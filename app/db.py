import json
import logging

import asyncpg

from config import DATABASE_URL, TELEMETRY_RETENTION_DAYS

logger = logging.getLogger("hobocams.db")

_pool: asyncpg.Pool | None = None


async def _init_connection(conn: asyncpg.Connection):
    # Transparent dict <-> jsonb conversion so callers never touch json.dumps/loads.
    await conn.set_type_codec(
        "jsonb", encoder=json.dumps, decoder=json.loads, schema="pg_catalog",
    )


async def get_pool() -> asyncpg.Pool:
    global _pool
    if _pool is None:
        _pool = await asyncpg.create_pool(
            DATABASE_URL, min_size=1, max_size=5, init=_init_connection,
        )
    return _pool


async def close_pool():
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None


async def ensure_retention_policies(pool: asyncpg.Pool):
    # TELEMETRY_RETENTION_DAYS <= 0 means "keep everything forever" - remove
    # any existing policy rather than just skipping adding one, so flipping
    # this back to 0 on a deployment that already has a real policy actually
    # takes effect on the next restart instead of silently doing nothing.
    # For a nonzero value, if_not_exists=True makes this safe to call every
    # startup, but won't pick up a *changed* value once the policy already
    # exists - see config.py's comment and the README for how to change it.
    async with pool.acquire() as conn:
        for table in ("telemetry", "radio_clients"):
            try:
                if TELEMETRY_RETENTION_DAYS > 0:
                    await conn.execute(
                        "SELECT add_retention_policy($1::regclass, make_interval(days => $2), if_not_exists => true)",
                        table, TELEMETRY_RETENTION_DAYS,
                    )
                else:
                    await conn.execute(
                        "SELECT remove_retention_policy($1::regclass, if_exists => true)", table,
                    )
            except Exception:
                logger.exception("failed to ensure retention policy on %s", table)
