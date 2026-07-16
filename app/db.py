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
    # if_not_exists=True makes this safe to call on every startup, but means
    # it won't pick up a *changed* TELEMETRY_RETENTION_DAYS on a deployment
    # that already has the policy - see config.py's comment and the README
    # for how to change it after the fact.
    async with pool.acquire() as conn:
        for table in ("telemetry", "radio_clients"):
            try:
                await conn.execute(
                    "SELECT add_retention_policy($1::regclass, make_interval(days => $2), if_not_exists => true)",
                    table, TELEMETRY_RETENTION_DAYS,
                )
            except Exception:
                logger.exception("failed to ensure retention policy on %s", table)
