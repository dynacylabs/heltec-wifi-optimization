import logging
from datetime import datetime, timedelta, timezone

import asyncpg

import halow_channel_plan
from config import (
    CHANNEL_COOLDOWN_MINUTES,
    DEFAULT_COMMAND_TTL_SECONDS,
    DEGRADED_SUSTAIN_MINUTES,
    RETRY_RATE_DEGRADED_THRESHOLD,
    WIFI24_CHANNELS,
)

logger = logging.getLogger("hobocams.optimizer")


async def run_optimizer_pass(pool: asyncpg.Pool):
    try:
        await _evaluate_halow_link(pool)
        await _evaluate_wifi24_link(pool)
    except Exception:
        logger.exception("optimizer pass failed")


async def _has_pending_command(conn, device_id, param: str) -> bool:
    return await conn.fetchval(
        "SELECT EXISTS(SELECT 1 FROM commands WHERE device_id = $1 AND param = $2 AND status IN ('pending', 'applied'))",
        device_id, param,
    )


async def _evaluate_halow_link(pool: asyncpg.Pool):
    async with pool.acquire() as conn:
        ap = await conn.fetchrow("SELECT id, mac FROM devices WHERE role = 'AP'")
        if not ap:
            return

        since = datetime.now(timezone.utc) - timedelta(minutes=DEGRADED_SUSTAIN_MINUTES)
        rows = await conn.fetch(
            """
            SELECT retries FROM telemetry
            WHERE device_id = $1 AND radio = 'halow' AND time >= $2
            ORDER BY time DESC
            """,
            ap["id"], since,
        )
        retry_values = [r["retries"] for r in rows if r["retries"] is not None]
        if not retry_values:
            return

        avg_retries = sum(retry_values) / len(retry_values)
        if avg_retries < RETRY_RATE_DEGRADED_THRESHOLD:
            return

        last_change = await conn.fetchval(
            """
            SELECT created_at FROM commands
            WHERE device_id = $1 AND param = 'halow_operating_freq'
            ORDER BY created_at DESC LIMIT 1
            """,
            ap["id"],
        )
        if last_change and datetime.now(timezone.utc) - last_change < timedelta(minutes=CHANNEL_COOLDOWN_MINUTES):
            logger.info("HaLow link degraded but still within cooldown since last change, skipping")
            return

        if await _has_pending_command(conn, ap["id"], "halow_operating_freq"):
            logger.info("HaLow link degraded but a command is already in flight, skipping")
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
            logger.warning(
                "HaLow link on AP %s degraded (avg retries=%.3f) but current channel/bandwidth "
                "unknown from telemetry - can't safely pick a target, skipping",
                ap["mac"], avg_retries,
            )
            return

        # Deliberately simple, not scan-informed: there's no real
        # channel-scan telemetry available on this HaLow driver (iwinfo
        # scan returns empty, confirmed live) - so rather than invent a
        # scoring heuristic on data we don't have, this just cycles to the
        # next valid channel at the SAME bandwidth in a fixed round-robin
        # order. It's a real, working decision, just not a smart one -
        # good enough to unblock the safe-apply loop and start
        # accumulating real before/after data, which is a prerequisite for
        # any better-informed strategy later anyway.
        valid = halow_channel_plan.valid_channels(current["bandwidth_mhz"])
        if not valid:
            logger.warning(
                "HaLow link on AP %s degraded but bandwidth %s MHz has no known valid channel "
                "list - skipping (see halow_channel_plan.py)",
                ap["mac"], current["bandwidth_mhz"],
            )
            return

        try:
            next_channel = valid[(valid.index(current["channel"]) + 1) % len(valid)]
        except ValueError:
            next_channel = valid[0]

        ttl_seconds = DEFAULT_COMMAND_TTL_SECONDS["halow_operating_freq"]
        await conn.execute(
            """
            INSERT INTO commands (device_id, param, target_value, previous_value, ttl_seconds)
            VALUES ($1, 'halow_operating_freq', $2, $3, $4)
            """,
            ap["id"], {"channel": next_channel},
            {"channel": current["channel"], "bandwidth_mhz": current["bandwidth_mhz"]}, ttl_seconds,
        )
        logger.warning(
            "HaLow link on AP %s degraded (avg retries=%.3f over last %d min) - "
            "cycling channel %s -> %s at %s MHz",
            ap["mac"], avg_retries, DEGRADED_SUSTAIN_MINUTES,
            current["channel"], next_channel, current["bandwidth_mhz"],
        )


async def _evaluate_wifi24_link(pool: asyncpg.Pool):
    # Same shape as HaLow, but the degradation signal lives in
    # radio_clients (per-client retries), not telemetry - the wifi24 radio
    # itself doesn't have a single "link quality" the way a P2P HaLow link
    # does; it's whatever its Blink/Shelly clients are experiencing.
    async with pool.acquire() as conn:
        sta = await conn.fetchrow("SELECT id, mac FROM devices WHERE role = 'STA'")
        if not sta:
            return

        since = datetime.now(timezone.utc) - timedelta(minutes=DEGRADED_SUSTAIN_MINUTES)
        rows = await conn.fetch(
            """
            SELECT retries FROM radio_clients
            WHERE device_id = $1 AND radio = 'wifi24' AND time >= $2
            """,
            sta["id"], since,
        )
        retry_values = [r["retries"] for r in rows if r["retries"] is not None]
        if not retry_values:
            return

        avg_retries = sum(retry_values) / len(retry_values)
        if avg_retries < RETRY_RATE_DEGRADED_THRESHOLD:
            return

        last_change = await conn.fetchval(
            """
            SELECT created_at FROM commands
            WHERE device_id = $1 AND param = 'wifi24_channel'
            ORDER BY created_at DESC LIMIT 1
            """,
            sta["id"],
        )
        if last_change and datetime.now(timezone.utc) - last_change < timedelta(minutes=CHANNEL_COOLDOWN_MINUTES):
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
        # heuristic.
        try:
            next_channel = WIFI24_CHANNELS[(WIFI24_CHANNELS.index(current_channel) + 1) % len(WIFI24_CHANNELS)]
        except ValueError:
            next_channel = WIFI24_CHANNELS[0]

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
            sta["mac"], avg_retries, DEGRADED_SUSTAIN_MINUTES, current_channel, next_channel,
        )
