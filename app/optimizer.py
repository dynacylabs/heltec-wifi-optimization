import logging
from datetime import datetime, timedelta, timezone

import asyncpg

from config import (
    CHANNEL_COOLDOWN_MINUTES,
    DEGRADED_SUSTAIN_MINUTES,
    RETRY_RATE_DEGRADED_THRESHOLD,
)

logger = logging.getLogger("hobocams.optimizer")


async def run_optimizer_pass(pool: asyncpg.Pool):
    try:
        await _evaluate_halow_link(pool)
        await _evaluate_wifi24_link(pool)
    except Exception:
        logger.exception("optimizer pass failed")


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

        # We don't yet ingest channel-scan results from the device, so there's
        # no candidate channel to score here. Detecting and logging sustained
        # degradation is real and useful on its own - auto-selecting a new
        # channel needs a scan-telemetry endpoint built first.
        logger.warning(
            "HaLow link on AP %s degraded (avg retries=%.3f over last %d min) "
            "- no action taken, channel-scan ingestion not implemented yet",
            ap["mac"], avg_retries, DEGRADED_SUSTAIN_MINUTES,
        )


async def _evaluate_wifi24_link(pool: asyncpg.Pool):
    # Same shape as HaLow, targeting the STA's wifi24 radio. Stubbed for the
    # same reason: needs real scan telemetry before it can pick a channel.
    return
