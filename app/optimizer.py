import logging
from datetime import datetime, timedelta, timezone

import asyncpg

import halow_channel_plan
from config import DEFAULT_COMMAND_TTL_SECONDS, WIFI24_CHANNELS
from notify import notify

logger = logging.getLogger("wifi_optimizer.optimizer")

BANDWIDTH_TIERS = [1, 2, 4, 8]  # MHz, matches halow_channel_plan.py


async def run_optimizer_pass(pool: asyncpg.Pool):
    try:
        async with pool.acquire() as conn:
            # Thresholds live in the DB (optimizer_state, migration 007) so
            # they're tunable from the dashboard without a rebuild - see
            # models.OptimizerSettings. Fetched once per pass and threaded
            # through rather than re-queried per-link.
            settings = await conn.fetchrow(
                """
                SELECT enabled, retry_rate_degraded_threshold, degraded_sustain_minutes,
                       channel_cooldown_minutes, bandwidth_widen_utilization_threshold,
                       bandwidth_widen_sustain_minutes, bandwidth_narrow_utilization_threshold,
                       bandwidth_narrow_sustain_minutes
                FROM optimizer_state LIMIT 1
                """
            )
        if not settings["enabled"]:
            logger.info("Optimizer paused via kill switch, skipping this pass")
            return
        await _evaluate_halow_link(pool, settings)
        await _evaluate_wifi24_link(pool, settings)
    except Exception:
        logger.exception("optimizer pass failed")


async def _has_pending_command(conn, device_id, param: str) -> bool:
    return await conn.fetchval(
        "SELECT EXISTS(SELECT 1 FROM commands WHERE device_id = $1 AND param = $2 AND status IN ('pending', 'applied'))",
        device_id, param,
    )


async def _avg_over_window(conn, table: str, column: str, where_extra: str, device_id, minutes: int):
    since = datetime.now(timezone.utc) - timedelta(minutes=minutes)
    rows = await conn.fetch(
        f"SELECT {column} AS v FROM {table} WHERE device_id = $1 {where_extra} AND time >= $2",
        device_id, since,
    )
    values = [r["v"] for r in rows if r["v"] is not None]
    return (sum(values) / len(values)) if values else None


async def _in_cooldown(conn, device_id, param: str, cooldown_minutes: int) -> bool:
    last_change = await conn.fetchval(
        "SELECT created_at FROM commands WHERE device_id = $1 AND param = $2 ORDER BY created_at DESC LIMIT 1",
        device_id, param,
    )
    return bool(last_change and datetime.now(timezone.utc) - last_change < timedelta(minutes=cooldown_minutes))


async def _reverted_channels(conn, device_id, param: str) -> set:
    # A channel that's reverted before doesn't necessarily mean it's really
    # bad RF - it can also mean this specific channel/bandwidth combination
    # just doesn't apply on this hardware (confirmed live: some entries in
    # the theoretical channel-plan CSV silently fail to take). Either way,
    # picking the exact same target again is pointless - without this, a
    # channel pick that's deterministic (widen/narrow) or that wraps back
    # to the same "next" channel every time (degradation cycling, once
    # cur_channel stops advancing because the last attempt reverted) retries
    # forever on a target that will never succeed.
    rows = await conn.fetch(
        "SELECT DISTINCT (target_value->>'channel')::int AS channel FROM commands "
        "WHERE device_id = $1 AND param = $2 AND status = 'reverted'",
        device_id, param,
    )
    return {r["channel"] for r in rows if r["channel"] is not None}


async def _emit_halow_command(conn, device_id, mac, channel: int, bandwidth_mhz: int, current_channel, current_bw, reason: str):
    ttl_seconds = DEFAULT_COMMAND_TTL_SECONDS["halow_operating_freq"]
    await conn.execute(
        """
        INSERT INTO commands (device_id, param, target_value, previous_value, ttl_seconds)
        VALUES ($1, 'halow_operating_freq', $2, $3, $4)
        """,
        device_id, {"channel": channel},
        {"channel": current_channel, "bandwidth_mhz": current_bw}, ttl_seconds,
    )
    logger.warning(
        "HaLow AP %s: %s - channel %s (%sMHz) -> %s (%sMHz)",
        mac, reason, current_channel, current_bw, channel, bandwidth_mhz,
    )


async def _evaluate_halow_link(pool: asyncpg.Pool, settings):
    async with pool.acquire() as conn:
        ap = await conn.fetchrow("SELECT id, mac FROM devices WHERE role = 'AP'")
        if not ap:
            return

        if await _has_pending_command(conn, ap["id"], "halow_operating_freq"):
            return

        current = await conn.fetchrow(
            """
            SELECT channel, bandwidth_mhz FROM telemetry
            WHERE device_id = $1 AND radio = 'halow'
            ORDER BY time DESC LIMIT 1
            """,
            ap["id"],
        )
        if not current or current["channel"] is None or current["bandwidth_mhz"] is None:
            return
        cur_channel, cur_bw = current["channel"], current["bandwidth_mhz"]

        if await _in_cooldown(conn, ap["id"], "halow_operating_freq", settings["channel_cooldown_minutes"]):
            return

        # 1. Degradation takes priority over anything else - widening or
        # narrowing a link that's actively struggling is likely to make
        # things worse, not better.
        sustain_minutes = settings["degraded_sustain_minutes"]
        avg_retries = await _avg_over_window(conn, "telemetry", "retries", "AND radio = 'halow'", ap["id"], sustain_minutes)
        if avg_retries is not None and avg_retries >= settings["retry_rate_degraded_threshold"]:
            # Deliberately simple, not scan-informed: there's no real
            # channel-scan telemetry available on this HaLow driver
            # (iwinfo scan returns empty, confirmed live) - so rather than
            # invent a scoring heuristic on data we don't have, this just
            # cycles to the next valid channel at the SAME bandwidth in a
            # fixed round-robin order.
            valid = halow_channel_plan.valid_channels(cur_bw)
            if not valid:
                logger.warning("HaLow AP %s degraded but bandwidth %s MHz has no known valid channel list", ap["mac"], cur_bw)
                return
            failed = await _reverted_channels(conn, ap["id"], "halow_operating_freq")
            start_idx = valid.index(cur_channel) if cur_channel in valid else -1
            next_channel = next(
                (c for c in (valid[(start_idx + step) % len(valid)] for step in range(1, len(valid) + 1)) if c not in failed),
                None,
            )
            if next_channel is None:
                logger.warning(
                    "HaLow AP %s degraded but every channel at %sMHz has previously failed to apply - not cycling further",
                    ap["mac"], cur_bw,
                )
                return
            await _emit_halow_command(
                conn, ap["id"], ap["mac"], next_channel, cur_bw, cur_channel, cur_bw,
                f"degraded (avg retries={avg_retries:.3f} over {sustain_minutes}m), cycling channel",
            )
            await notify(
                "WiFi Optimizer: HaLow link degraded",
                f"AP {ap['mac']}: sustained avg retries {avg_retries:.1%} over {sustain_minutes}m - "
                f"cycling channel {cur_channel} -> {next_channel} ({cur_bw}MHz)",
                priority="high", tags="warning",
            )
            return

        # 2. Widen - only considered on an otherwise-healthy link (we
        # already returned above if it's degraded). Utilization is actual
        # throughput against the currently negotiated PHY rate, not a
        # fixed theoretical capacity table for the bandwidth.
        if cur_bw != BANDWIDTH_TIERS[-1]:
            widen_minutes = settings["bandwidth_widen_sustain_minutes"]
            util = await _throughput_utilization(conn, ap["id"], widen_minutes)
            if util is not None and util >= settings["bandwidth_widen_utilization_threshold"]:
                new_bw = BANDWIDTH_TIERS[BANDWIDTH_TIERS.index(cur_bw) + 1]
                new_channel = await _pick_channel_for_bandwidth(conn, ap["id"], new_bw)
                if new_channel is not None:
                    await _emit_halow_command(
                        conn, ap["id"], ap["mac"], new_channel, new_bw, cur_channel, cur_bw,
                        f"sustained high utilization ({util:.2f} over {widen_minutes}m), widening",
                    )
                    return
                logger.warning(
                    "HaLow AP %s: sustained high utilization would widen to %sMHz, but every channel "
                    "there has previously failed to apply - not widening",
                    ap["mac"], new_bw,
                )

        # 3. Narrow - the mirror case, much longer sustain window since
        # it's the more conservative direction (better range/robustness at
        # the cost of throughput headroom we apparently aren't using).
        if cur_bw != BANDWIDTH_TIERS[0]:
            narrow_minutes = settings["bandwidth_narrow_sustain_minutes"]
            util = await _throughput_utilization(conn, ap["id"], narrow_minutes)
            if util is not None and util <= settings["bandwidth_narrow_utilization_threshold"]:
                new_bw = BANDWIDTH_TIERS[BANDWIDTH_TIERS.index(cur_bw) - 1]
                new_channel = await _pick_channel_for_bandwidth(conn, ap["id"], new_bw)
                if new_channel is not None:
                    await _emit_halow_command(
                        conn, ap["id"], ap["mac"], new_channel, new_bw, cur_channel, cur_bw,
                        f"sustained low utilization ({util:.2f} over {narrow_minutes}m), narrowing",
                    )
                    return
                logger.warning(
                    "HaLow AP %s: sustained low utilization would narrow to %sMHz, but every channel "
                    "there has previously failed to apply - not narrowing",
                    ap["mac"], new_bw,
                )


async def _pick_channel_for_bandwidth(conn, device_id, bandwidth_mhz: int):
    # Simplest possible choice, not frequency-proximity-aware: the first
    # valid channel at the new bandwidth that hasn't already reverted for
    # this device. Same "honest v1" reasoning as the round-robin channel
    # cycling above - good enough to make widening and narrowing real and
    # working, not necessarily the smartest pick. Without the failed-channel
    # check this was confirmed live to retry the exact same (never-working)
    # channel every cooldown period forever, since the pick is otherwise
    # deterministic - see README/Gotchas.
    valid = halow_channel_plan.valid_channels(bandwidth_mhz)
    if not valid:
        return None
    failed = await _reverted_channels(conn, device_id, "halow_operating_freq")
    return next((c for c in valid if c not in failed), None)


async def _throughput_utilization(conn, device_id, minutes: int):
    since = datetime.now(timezone.utc) - timedelta(minutes=minutes)
    rows = await conn.fetch(
        """
        SELECT throughput_mbps, rate_mbps FROM telemetry
        WHERE device_id = $1 AND radio = 'halow' AND time >= $2
        """,
        device_id, since,
    )
    pairs = [
        (r["throughput_mbps"], r["rate_mbps"]) for r in rows
        if r["throughput_mbps"] is not None and r["rate_mbps"] not in (None, 0)
    ]
    if not pairs:
        return None
    ratios = [t / r for t, r in pairs]
    return sum(ratios) / len(ratios)


async def _evaluate_wifi24_link(pool: asyncpg.Pool, settings):
    # Same shape as HaLow, but the degradation signal lives in
    # radio_clients (per-client retries), not telemetry - the wifi24 radio
    # itself doesn't have a single "link quality" the way a P2P HaLow link
    # does; it's whatever its downstream 2.4GHz clients are experiencing. No
    # bandwidth lever here - standard Wi-Fi channels don't have HaLow's
    # bandwidth-dependent numbering, so there's nothing to widen/narrow.
    async with pool.acquire() as conn:
        sta = await conn.fetchrow("SELECT id, mac FROM devices WHERE role = 'STA'")
        if not sta:
            return

        sustain_minutes = settings["degraded_sustain_minutes"]
        avg_retries = await _avg_over_window(conn, "radio_clients", "retries", "AND radio = 'wifi24'", sta["id"], sustain_minutes)
        if avg_retries is None or avg_retries < settings["retry_rate_degraded_threshold"]:
            return

        if await _in_cooldown(conn, sta["id"], "wifi24_channel", settings["channel_cooldown_minutes"]):
            logger.info("2.4GHz link degraded but still within cooldown since last change, skipping")
            return

        if await _has_pending_command(conn, sta["id"], "wifi24_channel"):
            logger.info("2.4GHz link degraded but a command is already in flight, skipping")
            return

        current_channel = await conn.fetchval(
            """
            SELECT channel FROM telemetry
            WHERE device_id = $1 AND radio = 'wifi24'
            ORDER BY time DESC LIMIT 1
            """,
            sta["id"],
        )
        if current_channel is None:
            logger.warning(
                "2.4GHz link on STA %s degraded (avg retries=%.3f) but current channel "
                "unknown from telemetry - can't safely pick a target, skipping",
                sta["mac"], avg_retries,
            )
            return

        # Same reasoning as HaLow: no real scan telemetry to score
        # candidates against, so this cycles through the standard
        # non-overlapping channels round-robin rather than guessing at a
        # heuristic. Also skips any channel that's previously reverted for
        # this device/param, same as HaLow - otherwise a channel that never
        # actually applies gets retried forever every cooldown period.
        failed = await _reverted_channels(conn, sta["id"], "wifi24_channel")
        start_idx = WIFI24_CHANNELS.index(current_channel) if current_channel in WIFI24_CHANNELS else -1
        next_channel = next(
            (c for c in (WIFI24_CHANNELS[(start_idx + step) % len(WIFI24_CHANNELS)] for step in range(1, len(WIFI24_CHANNELS) + 1)) if c not in failed),
            None,
        )
        if next_channel is None:
            logger.warning(
                "2.4GHz link on STA %s degraded but every standard channel has previously failed to apply - not cycling further",
                sta["mac"],
            )
            return

        ttl_seconds = DEFAULT_COMMAND_TTL_SECONDS["wifi24_channel"]
        await conn.execute(
            """
            INSERT INTO commands (device_id, param, target_value, previous_value, ttl_seconds)
            VALUES ($1, 'wifi24_channel', $2, $3, $4)
            """,
            sta["id"], {"channel": next_channel}, {"channel": current_channel}, ttl_seconds,
        )
        logger.warning(
            "2.4GHz link on STA %s degraded (avg retries=%.3f over last %d min) - "
            "cycling channel %s -> %s",
            sta["mac"], avg_retries, sustain_minutes, current_channel, next_channel,
        )
        await notify(
            "WiFi Optimizer: 2.4GHz link degraded",
            f"STA {sta['mac']}: sustained avg client retries {avg_retries:.1%} over {sustain_minutes}m - "
            f"cycling channel {current_channel} -> {next_channel}",
            priority="high", tags="warning",
        )
