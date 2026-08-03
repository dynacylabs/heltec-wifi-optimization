import hashlib
import json
import logging
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import BaseModel, ValidationError

import device_client
import ssh_client
from config import API_TOKEN, DEFAULT_COMMAND_TTL_SECONDS, SSH_KEY_PATH
from db import close_pool, ensure_retention_policies, get_pool
from models import (
    AppSettings,
    AppSettingsUpdate,
    BackupHistoryEntry,
    CollectResult,
    CommandHistoryEntry,
    DeviceStatus,
    DeviceTarget,
    DeviceTargetUpdate,
    OptimizerSettings,
    OptimizerState,
    ProvisionRequest,
    RadioClientPoint,
    RadioSnapshot,
    TelemetryPoint,
)
from notify import notify, send_test_notification
from optimizer import run_optimizer_pass

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("wifi_optimizer")

scheduler = AsyncIOScheduler()


async def _get_app_settings(pool) -> dict:
    # Loaded fresh from the DB (app_settings table, migration 011) rather
    # than cached in a module-level config constant - poll intervals,
    # alert/retention thresholds, ntfy config. Reloading it wherever it's
    # needed means an edit from the dashboard's System Settings section
    # takes effect immediately, same reasoning as _get_device_targets and
    # optimizer_state.
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM app_settings LIMIT 1")
    return dict(row)


@asynccontextmanager
async def lifespan(app: FastAPI):
    pool = await get_pool()
    settings = await _get_app_settings(pool)
    await ensure_retention_policies(pool, settings["telemetry_retention_days"])
    # Every job below is given an explicit id so update_app_settings can
    # reschedule it live (scheduler.reschedule_job) when its interval
    # changes from the dashboard, without needing a container restart.
    scheduler.add_job(
        run_optimizer_pass, "interval", seconds=settings["optimizer_interval_seconds"],
        args=[pool], id="run_optimizer_pass",
    )
    scheduler.add_job(
        check_device_liveness, "interval", seconds=settings["liveness_check_interval_seconds"],
        args=[pool], id="check_device_liveness",
    )
    # Everything below reaches out to the devices over SSH rather than
    # waiting for them to call in - which host/port/user to dial for each
    # role is loaded from the device_targets DB table on every tick (see
    # _get_device_targets), not fixed at startup - and README's
    # "Reaching the devices over SSH".
    scheduler.add_job(
        poll_telemetry, "interval", seconds=settings["ssh_poll_interval_seconds"],
        args=[pool], id="poll_telemetry",
    )
    scheduler.add_job(
        apply_pending_commands, "interval", seconds=settings["command_poll_interval_seconds"],
        args=[pool], id="apply_pending_commands",
    )
    scheduler.add_job(
        check_in_flight_commands, "interval", seconds=settings["command_poll_interval_seconds"],
        args=[pool], id="check_in_flight_commands",
    )
    scheduler.add_job(
        poll_backups, "interval", seconds=settings["backup_poll_interval_seconds"],
        args=[pool], id="poll_backups",
    )
    scheduler.start()
    yield
    scheduler.shutdown()
    await ssh_client.close_all()
    await close_pool()


app = FastAPI(title="heltec-wifi-optimizer", lifespan=lifespan)


async def require_token(token: str):
    # ?token= query param, not a header - see config.API_TOKEN for why.
    if token != API_TOKEN:
        raise HTTPException(401, "invalid token")


async def parse_body(request: Request, model: type[BaseModel]):
    # The dashboard's fetch() calls (set_optimizer_state, set_optimizer_settings)
    # don't set an explicit Content-Type on their JSON string bodies, so the
    # browser defaults to text/plain - which FastAPI's automatic
    # content-type-sensitive body parsing rejects for a JSON model. Parsing
    # the raw bytes ourselves sidesteps that. Raises the same 422 shape
    # FastAPI would have produced automatically, so callers get a normal
    # error body instead of an unhandled 500 on malformed input.
    try:
        return model.model_validate_json(await request.body())
    except ValidationError as e:
        raise HTTPException(422, e.errors())


async def _get_or_create_device(pool, mac: str, role: str | None, hostname: str | None):
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT id, role, offline_alerted FROM devices WHERE mac = $1", mac)
        if row:
            await conn.execute(
                "UPDATE devices SET last_seen = now(), hostname = COALESCE($2, hostname) WHERE id = $1",
                row["id"], hostname,
            )
            if row["offline_alerted"]:
                # Telemetry resumed after we'd already alerted on the
                # outage - clear the flag so a future outage alerts again,
                # and let whoever got paged know it's over.
                await conn.execute("UPDATE devices SET offline_alerted = false WHERE id = $1", row["id"])
                await notify(
                    conn,
                    f"WiFi Optimizer: {row['role']} {hostname or mac} back online",
                    "Telemetry resumed after an outage.",
                    tags="white_check_mark",
                )
            return row["id"]
        if role is None:
            raise HTTPException(400, "unknown device and no role provided to register it")
        row = await conn.fetchrow(
            "INSERT INTO devices (role, mac, hostname, last_seen) VALUES ($1, $2, $3, now()) RETURNING id",
            role, mac, hostname or "",
        )
        return row["id"]


async def check_device_liveness(pool):
    # Runs independently of the optimizer pass - a device being offline has
    # nothing to do with rule evaluation, and we want this checked on its
    # own (usually much shorter) interval.
    settings = await _get_app_settings(pool)
    offline_alert_seconds = settings["offline_alert_seconds"]
    async with pool.acquire() as conn:
        stale = await conn.fetch(
            """
            SELECT id, mac, hostname, role FROM devices
            WHERE offline_alerted = false
              AND (last_seen IS NULL OR last_seen < now() - make_interval(secs => $1))
            """,
            offline_alert_seconds,
        )
        for d in stale:
            await conn.execute("UPDATE devices SET offline_alerted = true WHERE id = $1", d["id"])
            await notify(
                conn,
                f"WiFi Optimizer: {d['role']} {d['hostname'] or d['mac']} offline",
                f"No telemetry received in over {offline_alert_seconds}s.",
                priority="high",
                tags="warning",
            )


async def _notify_command_outcome(conn, command_id: int, title_suffix: str, reason: str | None, tags: str):
    info = await conn.fetchrow(
        """
        SELECT d.mac, d.role, c.param, c.target_value FROM commands c
        JOIN devices d ON d.id = c.device_id WHERE c.id = $1
        """,
        command_id,
    )
    if info:
        await notify(
            conn,
            f"WiFi Optimizer: {info['role']} command {title_suffix}",
            f"{info['param']} -> {info['target_value']}: {reason or 'no reason given'}",
            priority="high",
            tags=tags,
        )


async def _notify_reverted(conn, command_id: int, reason: str | None):
    await _notify_command_outcome(conn, command_id, "reverted", reason, "rotating_light")


async def _notify_unknown(conn, command_id: int, reason: str | None):
    await _notify_command_outcome(conn, command_id, "outcome unknown", reason, "grey_question")


async def _upsert_radio_counters(conn, device_id, radio: str, retries_cum, packets_cum, tx_bytes_cum, rx_bytes_cum, now):
    # Returns the computed (retries_rate, throughput_mbps) for this poll by
    # comparing against whatever was stored on the *previous* poll, then
    # overwrites the stored row with this poll's raw counters. Cumulative
    # counters resetting (reboot, counter wrap) or this being the first
    # poll ever are treated as "no rate yet" (None) rather than a nonsense
    # negative/huge value - same reasoning the old on-device delta_rate/
    # delta_throughput_mbps had, just relocated here now that there's no
    # persistent on-device process to keep that state between one-shot SSH
    # polls (see migration 009).
    prev = await conn.fetchrow(
        "SELECT retries_cum, packets_cum, tx_bytes_cum, rx_bytes_cum, updated_at FROM device_radio_counters WHERE device_id = $1 AND radio = $2",
        device_id, radio,
    )
    await conn.execute(
        """
        INSERT INTO device_radio_counters (device_id, radio, retries_cum, packets_cum, tx_bytes_cum, rx_bytes_cum, updated_at)
        VALUES ($1, $2, $3, $4, $5, $6, $7)
        ON CONFLICT (device_id, radio) DO UPDATE SET
            retries_cum = $3, packets_cum = $4, tx_bytes_cum = $5, rx_bytes_cum = $6, updated_at = $7
        """,
        device_id, radio, retries_cum, packets_cum, tx_bytes_cum, rx_bytes_cum, now,
    )
    retries_rate = None
    if prev and retries_cum is not None and packets_cum is not None and prev["packets_cum"] is not None:
        d_retries = retries_cum - prev["retries_cum"]
        d_packets = packets_cum - prev["packets_cum"]
        retries_rate = (d_retries / d_packets) if (d_retries >= 0 and d_packets > 0) else 0.0
    throughput_mbps = None
    if prev and tx_bytes_cum is not None and rx_bytes_cum is not None and prev["tx_bytes_cum"] is not None:
        d_bytes = (tx_bytes_cum + rx_bytes_cum) - (prev["tx_bytes_cum"] + prev["rx_bytes_cum"])
        d_secs = (now - prev["updated_at"]).total_seconds()
        throughput_mbps = ((d_bytes * 8) / (d_secs * 1_000_000)) if (d_bytes >= 0 and d_secs > 0) else 0.0
    return retries_rate, throughput_mbps


async def _upsert_client_counters(conn, device_id, radio: str, client_mac: str, retries_cum, packets_cum, now):
    prev = await conn.fetchrow(
        "SELECT retries_cum, packets_cum FROM device_radio_client_counters WHERE device_id = $1 AND radio = $2 AND client_mac = $3",
        device_id, radio, client_mac,
    )
    await conn.execute(
        """
        INSERT INTO device_radio_client_counters (device_id, radio, client_mac, retries_cum, packets_cum, updated_at)
        VALUES ($1, $2, $3, $4, $5, $6)
        ON CONFLICT (device_id, radio, client_mac) DO UPDATE SET
            retries_cum = $4, packets_cum = $5, updated_at = $6
        """,
        device_id, radio, client_mac, retries_cum, packets_cum, now,
    )
    if not prev or retries_cum is None or packets_cum is None or prev["packets_cum"] is None:
        return None
    d_retries = retries_cum - prev["retries_cum"]
    d_packets = packets_cum - prev["packets_cum"]
    return (d_retries / d_packets) if (d_retries >= 0 and d_packets > 0) else 0.0


async def _ingest_collect(pool, role: str, raw: dict):
    result = CollectResult.model_validate(raw)
    device_id = await _get_or_create_device(pool, result.device_mac, role, result.hostname)
    now = datetime.now(timezone.utc)
    async with pool.acquire() as conn:
        async with conn.transaction():
            for radio in result.radios:
                retries_rate, throughput_mbps = await _upsert_radio_counters(
                    conn, device_id, radio.radio, radio.retries_cum, radio.packets_cum,
                    radio.tx_bytes_cum, radio.rx_bytes_cum, now,
                )
                await conn.execute(
                    """
                    INSERT INTO telemetry (time, device_id, radio, rssi, noise, mcs, rate_mbps, retries, channel, bandwidth_mhz, throughput_mbps)
                    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)
                    """,
                    now, device_id, radio.radio, radio.rssi, radio.noise, radio.mcs,
                    radio.rate_mbps, retries_rate, radio.channel, radio.bandwidth_mhz, throughput_mbps,
                )
                for client in radio.clients:
                    client_retries = await _upsert_client_counters(
                        conn, device_id, radio.radio, client.mac, client.retries_cum, client.packets_cum, now,
                    )
                    await conn.execute(
                        """
                        INSERT INTO radio_clients (time, device_id, radio, client_mac, host, rssi, rate_mbps, retries)
                        VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
                        """,
                        now, device_id, radio.radio, client.mac, None, client.rssi, client.rate_mbps, client_retries,
                    )


async def _get_device_targets(pool) -> dict[str, tuple[str, int, str]]:
    # Loaded fresh from the DB every scheduler tick rather than cached -
    # this table is tiny and rarely written to, and reloading it means an
    # edit made from the dashboard's Device Setup section takes effect on
    # the very next poll, with no restart required.
    async with pool.acquire() as conn:
        rows = await conn.fetch("SELECT role, ssh_host, ssh_port, ssh_user FROM device_targets")
    return {r["role"]: (r["ssh_host"], r["ssh_port"], r["ssh_user"]) for r in rows}


AUTO_REBOOT_COOLDOWN_SECONDS = 15 * 60
MAX_CONSECUTIVE_AUTO_REBOOTS = 3


async def _maybe_auto_recover_radio(pool, role: str, mac: str, host: str, port: int, user: str):
    # The on-device recover-radio cron (wifi-agent-boot.init) already tries
    # a chip reset + reboot escalation on its own 15-minute schedule,
    # independent of whether we can reach the device at all - but that's a
    # slower, self-contained backstop. This is a faster, server-driven path
    # for exactly the case that caused the 2026-08-01 outage: the AP stayed
    # SSH-reachable the entire time (so nothing was waiting on the on-device
    # cron - the device was never "unreachable"), but its HaLow radio was
    # wedged and nothing was watching for that between boots.
    async with pool.acquire() as conn:
        device = await conn.fetchrow(
            "SELECT id, last_auto_reboot_at, consecutive_auto_reboots FROM devices WHERE mac = $1", mac,
        )
        if device is None:
            return
        if device["last_auto_reboot_at"] is not None:
            elapsed = (datetime.now(timezone.utc) - device["last_auto_reboot_at"]).total_seconds()
            if elapsed < AUTO_REBOOT_COOLDOWN_SECONDS:
                return
        if device["consecutive_auto_reboots"] >= MAX_CONSECUTIVE_AUTO_REBOOTS:
            # Rebooting hasn't helped the last few times in a row - this
            # looks like a persistent hardware fault, not something more of
            # the same will fix. Stop retrying and rely on the existing
            # offline-liveness alerting instead of reboot-looping a device
            # that isn't recovering.
            return
        try:
            await device_client.reboot(host, port, user, SSH_KEY_PATH)
        except Exception:
            logger.warning("auto-recovery reboot failed for %s (%s)", role, host, exc_info=True)
            return
        attempt = device["consecutive_auto_reboots"] + 1
        await conn.execute(
            "UPDATE devices SET last_auto_reboot_at = now(), consecutive_auto_reboots = $2 WHERE id = $1",
            device["id"], attempt,
        )
        await notify(
            conn,
            f"WiFi Optimizer: {role} auto-recovery reboot",
            f"HaLow radio reported down on {role} ({mac}) - issuing an automatic reboot (attempt {attempt}/{MAX_CONSECUTIVE_AUTO_REBOOTS}).",
            priority="high",
            tags="arrows_counterclockwise",
        )


async def _reset_auto_recovery(pool, mac: str):
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE devices SET consecutive_auto_reboots = 0 WHERE mac = $1 AND consecutive_auto_reboots != 0", mac,
        )


async def poll_telemetry(pool):
    # Server-initiated telemetry pull, one SSH round-trip per device per
    # tick (see device_client.collect) - replaces the old design where each
    # device's agent pushed this out on its own loop.
    for role, (host, port, user) in (await _get_device_targets(pool)).items():
        try:
            raw = await device_client.collect(host, port, user, SSH_KEY_PATH)
        except Exception:
            logger.warning("telemetry collect failed for %s (%s)", role, host, exc_info=True)
            continue
        try:
            await _ingest_collect(pool, role, raw)
        except Exception:
            logger.exception("failed to ingest telemetry for %s (%s)", role, host)

        halow = next((r for r in raw.get("radios", []) if r.get("radio") == "halow"), None)
        mac = raw.get("device_mac")
        if halow is not None and mac is not None:
            if halow.get("radio_up") is False:
                await _maybe_auto_recover_radio(pool, role, mac, host, port, user)
            elif halow.get("radio_up") is True:
                await _reset_auto_recovery(pool, mac)


async def apply_pending_commands(pool):
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT c.id, c.param, c.target_value, d.mac, d.role
            FROM commands c JOIN devices d ON d.id = c.device_id
            WHERE c.status = 'pending'
            ORDER BY c.created_at ASC
            """
        )
    if not rows:
        return
    targets = await _get_device_targets(pool)
    for c in rows:
        target = targets.get(c["role"])
        if target is None:
            logger.warning("no device_targets row for role %s, skipping command %s", c["role"], c["id"])
            continue
        host, port, user = target
        if c["param"] == "reboot":
            # No uci apply/rollback to do, and no live state to verify
            # afterward - the device is about to disappear. Ack as soon as
            # the reboot command has actually been sent over SSH, same
            # "ack first" ordering the old agent used, just server-driven.
            try:
                await device_client.reboot(host, port, user, SSH_KEY_PATH)
            except Exception:
                logger.warning("reboot SSH failed for %s (%s), will retry next tick", c["mac"], host, exc_info=True)
                continue
            async with pool.acquire() as conn:
                await conn.execute(
                    "UPDATE commands SET status = 'acked', applied_at = now(), acked_at = now() WHERE id = $1",
                    c["id"],
                )
            logger.warning("Reboot acked for %s (%s)", c["mac"], host)
            continue

        try:
            await device_client.apply_command(
                host, port, user, SSH_KEY_PATH, c["param"], c["target_value"],
                DEFAULT_COMMAND_TTL_SECONDS.get(c["param"], 120),
            )
        except Exception:
            logger.warning(
                "apply SSH failed for command %s on %s (%s), will retry next tick",
                c["id"], c["mac"], host, exc_info=True,
            )
            continue
        async with pool.acquire() as conn:
            await conn.execute(
                "UPDATE commands SET status = 'applied', applied_at = now() WHERE id = $1", c["id"],
            )


async def check_in_flight_commands(pool):
    # Runs the same cadence as apply_pending_commands. A command sits here
    # (status='applied') until either verify-confirm gives a definitive
    # answer or its ttl_seconds + grace period lapses - the on-device
    # `uci apply --rollback` timer is what actually guarantees the link
    # can't be left in a bad state either way; this loop only decides what
    # the dashboard/history should say happened.
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT c.id, c.param, c.target_value, c.ttl_seconds, c.applied_at, d.mac, d.role
            FROM commands c JOIN devices d ON d.id = c.device_id
            WHERE c.status = 'applied'
            """
        )
    now = datetime.now(timezone.utc)
    if not rows:
        return
    targets = await _get_device_targets(pool)
    command_verify_delay_seconds = (await _get_app_settings(pool))["command_verify_delay_seconds"]
    for c in rows:
        elapsed = (now - c["applied_at"]).total_seconds()
        if elapsed < command_verify_delay_seconds:
            continue
        target = targets.get(c["role"])
        if target is None:
            logger.warning("no device_targets row for role %s, skipping command %s", c["role"], c["id"])
            continue
        host, port, user = target
        confirmed = None
        try:
            confirmed = await device_client.verify_and_confirm(
                host, port, user, SSH_KEY_PATH, c["param"], c["target_value"],
            )
        except Exception:
            logger.warning(
                "verify-confirm SSH failed for command %s on %s (%s)", c["id"], c["mac"], host, exc_info=True,
            )

        async with pool.acquire() as conn:
            if confirmed is True:
                await conn.execute("UPDATE commands SET status = 'acked', acked_at = now() WHERE id = $1", c["id"])
            elif confirmed is False:
                reason = "target value not reached after apply"
                await conn.execute("UPDATE commands SET status = 'reverted', reason = $2 WHERE id = $1", c["id"], reason)
                await _notify_reverted(conn, c["id"], reason)
            elif elapsed >= c["ttl_seconds"] + 15:
                # Couldn't reach the device to check even after retrying
                # every tick up to ttl_seconds + 15s. The on-device
                # rollback timer *should* have reverted the change on its
                # own by now, but that's not guaranteed - confirmed live
                # (2026-08-01): a HaLow chip that wedges at the SDIO level
                # mid-apply can leave the on-device rollback unable to
                # actually take effect, silently leaving the bad config in
                # place. Report this as genuinely unknown rather than
                # asserting a revert that was never actually verified.
                reason = "could not reach device to confirm - outcome unknown, on-device rollback may not have taken effect"
                await conn.execute("UPDATE commands SET status = 'unknown', reason = $2 WHERE id = $1", c["id"], reason)
                await _notify_unknown(conn, c["id"], reason)
            # else: still within the retry window - leave as 'applied', try again next tick.


async def poll_backups(pool):
    backup_retention_count = (await _get_app_settings(pool))["backup_retention_count"]
    for role, (host, port, user) in (await _get_device_targets(pool)).items():
        try:
            archive = await device_client.fetch_backup(host, port, user, SSH_KEY_PATH)
        except Exception:
            logger.warning("backup fetch failed for %s (%s)", role, host, exc_info=True)
            continue
        if not archive:
            continue
        sha256 = hashlib.sha256(archive).hexdigest()
        async with pool.acquire() as conn:
            device = await conn.fetchrow("SELECT id FROM devices WHERE role = $1", role)
            if not device:
                continue  # hasn't shown up via telemetry yet
            latest_sha256 = await conn.fetchval(
                "SELECT sha256 FROM device_backups WHERE device_id = $1 ORDER BY created_at DESC LIMIT 1",
                device["id"],
            )
            if latest_sha256 == sha256:
                continue  # unchanged since the last stored version
            await conn.execute(
                "INSERT INTO device_backups (device_id, sha256, size_bytes, archive) VALUES ($1, $2, $3, $4)",
                device["id"], sha256, len(archive), archive,
            )
            if backup_retention_count > 0:
                await conn.execute(
                    """
                    DELETE FROM device_backups WHERE device_id = $1 AND id NOT IN (
                        SELECT id FROM device_backups WHERE device_id = $1
                        ORDER BY created_at DESC LIMIT $2
                    )
                    """,
                    device["id"], backup_retention_count,
                )


# Devices are polled roughly every ssh_poll_interval_seconds (default 30s,
# app_settings table); treat gaps beyond this as real downtime rather than
# normal jitter between polls. Deliberately a fixed constant, not read
# from the DB - a generous fixed margin over the default poll interval is
# simpler than threading a dynamic value through every uptime query, and
# this only affects the dashboard's uptime-% display, not anything that
# gates a real alert.
EXPECTED_POLL_INTERVAL_SECONDS = 60


async def _uptime_pct(conn, device_id: int, hours: float) -> float:
    # Gap analysis over the halow stream as a heartbeat proxy (both radios
    # get inserted together each cycle, so this represents "device was
    # alive and posting" regardless of which radio you'd otherwise care
    # about). Bookends the window with virtual readings at window start/now
    # so an outage at the very start or still ongoing at "now" both count,
    # not just gaps strictly between two real rows.
    downtime_seconds = await conn.fetchval(
        """
        WITH bounds AS (
            SELECT now() - ($2 || ' hours')::interval AS window_start, now() AS window_end
        ),
        readings AS (
            SELECT time FROM telemetry
            WHERE device_id = $1 AND radio = 'halow' AND time > (SELECT window_start FROM bounds)
        ),
        with_bounds AS (
            SELECT window_start AS time FROM bounds
            UNION ALL
            SELECT time FROM readings
            UNION ALL
            SELECT window_end AS time FROM bounds
        ),
        gaps AS (
            SELECT time, LAG(time) OVER (ORDER BY time) AS prev_time FROM with_bounds
        )
        SELECT COALESCE(SUM(GREATEST(EXTRACT(EPOCH FROM (time - prev_time)) - $3, 0)), 0)
        FROM gaps WHERE prev_time IS NOT NULL
        """,
        device_id, str(hours), EXPECTED_POLL_INTERVAL_SECONDS,
    )
    window_seconds = hours * 3600
    return max(0.0, min(100.0, 100.0 * (1 - float(downtime_seconds) / window_seconds)))


# Longest selectable range is 12mo; the extra headroom past exactly 365
# days is just slop for callers computing "12mo" as 366d/leap years/etc.
MAX_HOURS = 24 * 400


@app.get("/api/status", response_model=list[DeviceStatus], dependencies=[Depends(require_token)])
async def get_status(hours: float = Query(default=24, gt=0, le=MAX_HOURS)):
    pool = await get_pool()
    async with pool.acquire() as conn:
        devices = await conn.fetch("SELECT id, mac, role, hostname, last_seen FROM devices ORDER BY role")
        result = []
        for d in devices:
            halow = await conn.fetchrow(
                """
                SELECT time, rssi, noise, mcs, rate_mbps, retries, channel, bandwidth_mhz, throughput_mbps
                FROM telemetry WHERE device_id = $1 AND radio = 'halow'
                ORDER BY time DESC LIMIT 1
                """,
                d["id"],
            )
            wifi24 = await conn.fetchrow(
                """
                SELECT time, rssi, noise, mcs, rate_mbps, retries, channel, bandwidth_mhz, throughput_mbps
                FROM telemetry WHERE device_id = $1 AND radio = 'wifi24'
                ORDER BY time DESC LIMIT 1
                """,
                d["id"],
            )
            client_count = await conn.fetchval(
                """
                SELECT count(DISTINCT client_mac) FROM radio_clients
                WHERE device_id = $1 AND radio = 'wifi24' AND time > now() - interval '2 minutes'
                """,
                d["id"],
            )
            uptime_pct = await _uptime_pct(conn, d["id"], hours)
            result.append(DeviceStatus(
                mac=d["mac"],
                role=d["role"],
                hostname=d["hostname"],
                last_seen=d["last_seen"],
                latest_halow=RadioSnapshot(**dict(halow)) if halow else None,
                latest_wifi24=RadioSnapshot(**dict(wifi24)) if wifi24 else None,
                wifi24_client_count=client_count or 0,
                uptime_pct=uptime_pct,
            ))
        return result


# Every chart-data endpoint downsamples to roughly this many points
# regardless of the selected range, via TimescaleDB's time_bucket(). Without
# this, a 12mo range at one telemetry row per ~30s is 1M+ rows per
# radio/device - far too much to query, ship to the browser, or render in
# Chart.js. At short ranges the computed bucket width collapses back below
# the real ~30s poll interval, so bucketing is a no-op there (every bucket
# holds at most one real row) - one code path handles both cases.
TARGET_CHART_POINTS = 600


def _bucket_seconds(hours: float) -> int:
    return max(30, int((hours * 3600) / TARGET_CHART_POINTS))


@app.get("/api/telemetry/{device_mac}", response_model=list[TelemetryPoint], dependencies=[Depends(require_token)])
async def get_telemetry_history(
    device_mac: str,
    radio: str = Query(pattern="^(halow|wifi24)$"),
    hours: float = Query(default=6, gt=0, le=MAX_HOURS),
):
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT
                time_bucket(make_interval(secs => $4), t.time) AS time,
                avg(t.rssi) AS rssi,
                avg(t.noise) AS noise,
                avg(t.mcs) AS mcs,
                avg(t.rate_mbps) AS rate_mbps,
                avg(t.retries) AS retries,
                last(t.channel, t.time) AS channel,
                last(t.bandwidth_mhz, t.time) AS bandwidth_mhz,
                avg(t.throughput_mbps) AS throughput_mbps
            FROM telemetry t JOIN devices d ON d.id = t.device_id
            WHERE d.mac = $1 AND t.radio = $2 AND t.time > now() - make_interval(secs => $3)
            GROUP BY 1
            ORDER BY 1 ASC
            """,
            device_mac, radio, hours * 3600, _bucket_seconds(hours),
        )
        return [TelemetryPoint(**dict(r)) for r in rows]


@app.get("/api/radio-clients/{device_mac}", response_model=list[RadioClientPoint], dependencies=[Depends(require_token)])
async def get_radio_client_history(
    device_mac: str,
    radio: str = Query(default="wifi24", pattern="^(halow|wifi24)$"),
    hours: float = Query(default=6, gt=0, le=MAX_HOURS),
):
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT
                time_bucket(make_interval(secs => $4), rc.time) AS time,
                rc.client_mac,
                max(rc.host) AS host,
                avg(rc.rssi) AS rssi,
                avg(rc.rate_mbps) AS rate_mbps,
                avg(rc.retries) AS retries
            FROM radio_clients rc JOIN devices d ON d.id = rc.device_id
            WHERE d.mac = $1 AND rc.radio = $2 AND rc.time > now() - make_interval(secs => $3)
            GROUP BY 1, rc.client_mac
            ORDER BY 1 ASC
            """,
            device_mac, radio, hours * 3600, _bucket_seconds(hours),
        )
        return [RadioClientPoint(**dict(r)) for r in rows]


@app.get("/api/commands", response_model=list[CommandHistoryEntry], dependencies=[Depends(require_token)])
async def get_command_history(limit: int = Query(default=50, gt=0, le=500)):
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT c.id, d.mac AS device_mac, d.role AS device_role, c.param, c.target_value,
                   c.previous_value, c.created_at, c.ttl_seconds, c.status, c.applied_at,
                   c.acked_at, c.reason
            FROM commands c JOIN devices d ON d.id = c.device_id
            ORDER BY c.created_at DESC LIMIT $1
            """,
            limit,
        )
        return [CommandHistoryEntry(**dict(r)) for r in rows]


@app.get("/api/optimizer", response_model=OptimizerState, dependencies=[Depends(require_token)])
async def get_optimizer_state():
    pool = await get_pool()
    async with pool.acquire() as conn:
        enabled = await conn.fetchval("SELECT enabled FROM optimizer_state LIMIT 1")
        return OptimizerState(enabled=bool(enabled))


@app.post("/api/optimizer", status_code=204, dependencies=[Depends(require_token)])
async def set_optimizer_state(request: Request):
    # The dashboard's kill switch. Pausing stops the optimizer from
    # issuing any NEW commands - it does not touch anything already in
    # flight, which still goes through its own rollback safety net.
    state = await parse_body(request, OptimizerState)
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("UPDATE optimizer_state SET enabled = $1", state.enabled)
    logger.warning("Optimizer %s via dashboard kill switch", "ENABLED" if state.enabled else "DISABLED")


@app.post("/api/devices/{device_mac}/reboot", status_code=202, dependencies=[Depends(require_token)])
async def reboot_device(device_mac: str):
    # Queued through the same commands table as everything else, but
    # apply_pending_commands special-cases 'reboot' - there's no uci apply/
    # rollback to do, and no live state to verify afterward (the device
    # disappears mid-SSH-session). Meant for "the device is up but wedged
    # in a bad radio/network state", which otherwise has no remote
    # recovery path on hardware sitting somewhere hard to physically reach.
    pool = await get_pool()
    async with pool.acquire() as conn:
        device = await conn.fetchrow("SELECT id, role FROM devices WHERE mac = $1", device_mac)
        if not device:
            raise HTTPException(404, "unknown device")
        await conn.execute(
            "INSERT INTO commands (device_id, param, target_value, ttl_seconds) VALUES ($1, 'reboot', '{}'::jsonb, $2)",
            device["id"], DEFAULT_COMMAND_TTL_SECONDS["reboot"],
        )
    logger.warning("Reboot command queued for %s (%s) via dashboard", device_mac, device["role"])


@app.get("/api/settings", response_model=OptimizerSettings, dependencies=[Depends(require_token)])
async def get_optimizer_settings():
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT retry_rate_degraded_threshold, degraded_sustain_minutes, channel_cooldown_minutes,
                   bandwidth_widen_utilization_threshold, bandwidth_widen_sustain_minutes,
                   bandwidth_narrow_utilization_threshold, bandwidth_narrow_sustain_minutes,
                   halow_channel_cycling_enabled, halow_bandwidth_changes_enabled,
                   wifi24_channel_cycling_enabled
            FROM optimizer_state LIMIT 1
            """
        )
        return OptimizerSettings(**dict(row))


@app.post("/api/settings", status_code=204, dependencies=[Depends(require_token)])
async def set_optimizer_settings(request: Request):
    settings = await parse_body(request, OptimizerSettings)
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            UPDATE optimizer_state SET
                retry_rate_degraded_threshold = $1,
                degraded_sustain_minutes = $2,
                channel_cooldown_minutes = $3,
                bandwidth_widen_utilization_threshold = $4,
                bandwidth_widen_sustain_minutes = $5,
                bandwidth_narrow_utilization_threshold = $6,
                bandwidth_narrow_sustain_minutes = $7,
                halow_channel_cycling_enabled = $8,
                halow_bandwidth_changes_enabled = $9,
                wifi24_channel_cycling_enabled = $10
            """,
            settings.retry_rate_degraded_threshold, settings.degraded_sustain_minutes,
            settings.channel_cooldown_minutes, settings.bandwidth_widen_utilization_threshold,
            settings.bandwidth_widen_sustain_minutes, settings.bandwidth_narrow_utilization_threshold,
            settings.bandwidth_narrow_sustain_minutes, settings.halow_channel_cycling_enabled,
            settings.halow_bandwidth_changes_enabled, settings.wifi24_channel_cycling_enabled,
        )
    logger.warning("Optimizer settings updated via dashboard: %s", settings)


@app.get("/api/app-settings", response_model=AppSettings, dependencies=[Depends(require_token)])
async def get_app_settings():
    row = await _get_app_settings(await get_pool())
    return AppSettings(
        ssh_poll_interval_seconds=row["ssh_poll_interval_seconds"],
        command_poll_interval_seconds=row["command_poll_interval_seconds"],
        command_verify_delay_seconds=row["command_verify_delay_seconds"],
        backup_poll_interval_seconds=row["backup_poll_interval_seconds"],
        optimizer_interval_seconds=row["optimizer_interval_seconds"],
        liveness_check_interval_seconds=row["liveness_check_interval_seconds"],
        offline_alert_seconds=row["offline_alert_seconds"],
        telemetry_retention_days=row["telemetry_retention_days"],
        backup_retention_count=row["backup_retention_count"],
        ntfy_url=row["ntfy_url"],
        ntfy_topic=row["ntfy_topic"],
        ntfy_token_set=bool(row["ntfy_token"]),
    )


@app.post("/api/app-settings", status_code=204, dependencies=[Depends(require_token)])
async def update_app_settings(request: Request):
    # Everything here used to be a docker-compose.yml env var - see
    # config.py and README's "Configuring app settings". Rescheduling the
    # live APScheduler jobs (by the fixed ids assigned in lifespan()) and
    # re-running ensure_retention_policies means a change here takes
    # effect immediately, no container restart required.
    update = await parse_body(request, AppSettingsUpdate)
    pool = await get_pool()
    async with pool.acquire() as conn:
        if update.ntfy_token:
            await conn.execute(
                """
                UPDATE app_settings SET
                    ssh_poll_interval_seconds = $1, command_poll_interval_seconds = $2,
                    command_verify_delay_seconds = $3, backup_poll_interval_seconds = $4,
                    optimizer_interval_seconds = $5, liveness_check_interval_seconds = $6,
                    offline_alert_seconds = $7, telemetry_retention_days = $8,
                    backup_retention_count = $9, ntfy_url = $10, ntfy_topic = $11, ntfy_token = $12
                """,
                update.ssh_poll_interval_seconds, update.command_poll_interval_seconds,
                update.command_verify_delay_seconds, update.backup_poll_interval_seconds,
                update.optimizer_interval_seconds, update.liveness_check_interval_seconds,
                update.offline_alert_seconds, update.telemetry_retention_days,
                update.backup_retention_count, update.ntfy_url, update.ntfy_topic, update.ntfy_token,
            )
        else:
            # Blank/omitted ntfy_token leaves whatever's already stored
            # untouched, rather than clearing it - see models.AppSettingsUpdate.
            await conn.execute(
                """
                UPDATE app_settings SET
                    ssh_poll_interval_seconds = $1, command_poll_interval_seconds = $2,
                    command_verify_delay_seconds = $3, backup_poll_interval_seconds = $4,
                    optimizer_interval_seconds = $5, liveness_check_interval_seconds = $6,
                    offline_alert_seconds = $7, telemetry_retention_days = $8,
                    backup_retention_count = $9, ntfy_url = $10, ntfy_topic = $11
                """,
                update.ssh_poll_interval_seconds, update.command_poll_interval_seconds,
                update.command_verify_delay_seconds, update.backup_poll_interval_seconds,
                update.optimizer_interval_seconds, update.liveness_check_interval_seconds,
                update.offline_alert_seconds, update.telemetry_retention_days,
                update.backup_retention_count, update.ntfy_url, update.ntfy_topic,
            )

    scheduler.reschedule_job("poll_telemetry", trigger="interval", seconds=update.ssh_poll_interval_seconds)
    scheduler.reschedule_job("apply_pending_commands", trigger="interval", seconds=update.command_poll_interval_seconds)
    scheduler.reschedule_job("check_in_flight_commands", trigger="interval", seconds=update.command_poll_interval_seconds)
    scheduler.reschedule_job("poll_backups", trigger="interval", seconds=update.backup_poll_interval_seconds)
    scheduler.reschedule_job("run_optimizer_pass", trigger="interval", seconds=update.optimizer_interval_seconds)
    scheduler.reschedule_job("check_device_liveness", trigger="interval", seconds=update.liveness_check_interval_seconds)
    await ensure_retention_policies(pool, update.telemetry_retention_days)
    logger.warning("App settings updated via dashboard")


@app.post("/api/app-settings/test-notification", status_code=200, dependencies=[Depends(require_token)])
async def test_app_notification():
    pool = await get_pool()
    async with pool.acquire() as conn:
        try:
            sent = await send_test_notification(conn)
        except Exception as exc:
            raise HTTPException(400, f"failed to send test notification: {exc}")
    if not sent:
        raise HTTPException(400, "ntfy_url/ntfy_topic not set - nothing to test")
    return {"status": "sent"}


@app.get("/api/backups", response_model=list[BackupHistoryEntry], dependencies=[Depends(require_token)])
async def get_backup_history(limit: int = Query(default=100, gt=0, le=1000)):
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT b.id, d.mac AS device_mac, d.role AS device_role, b.created_at, b.sha256, b.size_bytes
            FROM device_backups b JOIN devices d ON d.id = b.device_id
            ORDER BY b.created_at DESC LIMIT $1
            """,
            limit,
        )
        return [BackupHistoryEntry(**dict(r)) for r in rows]


@app.get("/api/backups/{backup_id}/download", dependencies=[Depends(require_token)])
async def download_backup(backup_id: int):
    # Not response_model'd (raw binary, not JSON) - a human pulls this down
    # to manually restore via scp/ssh onto the device, the same last-mile
    # step hobo_cams' restore.sh does, just sourced from here instead of a
    # local dated folder.
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT b.archive, b.created_at, d.mac, d.role FROM device_backups b
            JOIN devices d ON d.id = b.device_id WHERE b.id = $1
            """,
            backup_id,
        )
        if not row:
            raise HTTPException(404, "unknown backup")
        filename = f"{row['role']}-{row['mac']}-{row['created_at']:%Y%m%dT%H%M%SZ}.tar.gz"
        return Response(
            content=row["archive"],
            media_type="application/gzip",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )


@app.post("/api/backups/{backup_id}/restore", dependencies=[Depends(require_token)])
async def restore_backup(backup_id: int):
    # Usable any time the target device is reachable over the existing
    # key-based SSH connection - not just during initial provisioning.
    # e.g. "this device got reset/swapped, put the last known-good config
    # back on it."
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT b.archive, d.role FROM device_backups b
            JOIN devices d ON d.id = b.device_id WHERE b.id = $1
            """,
            backup_id,
        )
        if not row:
            raise HTTPException(404, "unknown backup")
        target = await conn.fetchrow(
            "SELECT ssh_host, ssh_port, ssh_user FROM device_targets WHERE role = $1", row["role"],
        )
        if not target:
            raise HTTPException(404, f"no device_targets entry configured for role {row['role']}")
    try:
        await device_client.restore_backup(
            target["ssh_host"], target["ssh_port"], target["ssh_user"], SSH_KEY_PATH, row["archive"],
        )
    except Exception as exc:
        raise HTTPException(502, f"restore failed: {exc}")
    logger.warning("Backup %d restored to %s (%s)", backup_id, row["role"], target["ssh_host"])
    return {"status": "ok"}


@app.get("/api/device-targets", response_model=list[DeviceTarget], dependencies=[Depends(require_token)])
async def get_device_targets():
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT role, label, ssh_host, ssh_port, ssh_user,
                   provisioned_at, last_provision_status, last_provision_error
            FROM device_targets ORDER BY role
            """
        )
        return [DeviceTarget(**dict(r)) for r in rows]


@app.post("/api/device-targets/{role}", status_code=204, dependencies=[Depends(require_token)])
async def update_device_target(role: str, request: Request):
    update = await parse_body(request, DeviceTargetUpdate)
    pool = await get_pool()
    async with pool.acquire() as conn:
        result = await conn.execute(
            """
            UPDATE device_targets SET
                ssh_host = $2, ssh_port = $3, ssh_user = $4,
                label = COALESCE($5, label), updated_at = now()
            WHERE role = $1
            """,
            role, update.ssh_host, update.ssh_port, update.ssh_user, update.label,
        )
    if result == "UPDATE 0":
        raise HTTPException(404, f"unknown device role {role}")
    logger.warning("Device target updated via dashboard: %s -> %s:%s", role, update.ssh_host, update.ssh_port)


@app.post("/api/device-targets/{role}/provision", dependencies=[Depends(require_token)])
async def provision_device(role: str, request: Request):
    # Full zero-touch bootstrap of a brand-new device: installs our SSH
    # key (password-authenticated, one time only), pushes wifi-agent.sh +
    # the boot-init script, optionally restores a prior backup, then
    # reboots. The password is used only for this one call and is never
    # stored, logged, or persisted - see device_client.provision.
    body = await parse_body(request, ProvisionRequest)
    pool = await get_pool()
    async with pool.acquire() as conn:
        target = await conn.fetchrow(
            "SELECT ssh_host, ssh_port, ssh_user FROM device_targets WHERE role = $1", role,
        )
        if not target:
            raise HTTPException(404, f"unknown device role {role}")
        restore_archive = None
        if body.restore_backup_id is not None:
            restore_archive = await conn.fetchval(
                "SELECT archive FROM device_backups WHERE id = $1", body.restore_backup_id,
            )
            if restore_archive is None:
                raise HTTPException(404, f"unknown backup {body.restore_backup_id}")

    try:
        await device_client.provision(
            target["ssh_host"], target["ssh_port"], target["ssh_user"], body.password, restore_archive,
        )
    except Exception as exc:
        error = str(exc)[:500]
        async with pool.acquire() as conn:
            await conn.execute(
                "UPDATE device_targets SET last_provision_status = 'failed', last_provision_error = $2 WHERE role = $1",
                role, error,
            )
        raise HTTPException(502, f"provisioning failed: {error}")

    async with pool.acquire() as conn:
        await conn.execute(
            """
            UPDATE device_targets SET
                provisioned_at = now(), last_provision_status = 'ok', last_provision_error = NULL
            WHERE role = $1
            """,
            role,
        )
    logger.warning("Device %s provisioned via dashboard (%s)", role, target["ssh_host"])
    return {"status": "ok"}


@app.get("/")
async def root():
    return RedirectResponse("/dashboard")


def _render_app_page() -> HTMLResponse:
    # Injects the shared token server-side so the browser never has to
    # prompt for or store it - access control for a human is expected to
    # happen at the reverse proxy (Authelia), not here. The token itself
    # stays required on the API routes regardless, as defense in depth
    # against this same container also being reachable via the open,
    # un-authelia'd device API domain.
    #
    # /dashboard and /settings both serve this same page - it's a single
    # HTML document with both views built in, and switches between them
    # client-side (see dashboard.html's showTab()) instead of doing a full
    # page navigation on every tab click.
    with open("static/dashboard.html", encoding="utf-8") as f:
        html = f.read()
    html = html.replace("__API_TOKEN__", json.dumps(API_TOKEN))
    return HTMLResponse(html)


@app.get("/dashboard")
async def dashboard_page():
    return _render_app_page()


@app.get("/settings")
async def settings_page():
    return _render_app_page()


@app.get("/health")
async def health():
    return {"status": "ok"}
