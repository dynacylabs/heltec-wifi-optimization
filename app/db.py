import json

import asyncpg

from config import DATABASE_URL

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
