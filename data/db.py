import asyncpg
from core.config import config
from core.logging import get_logger

log = get_logger(__name__)

_pool: asyncpg.Pool | None = None


async def get_pool() -> asyncpg.Pool:
    global _pool
    if _pool is None:
        _pool = await asyncpg.create_pool(
            config.database_url,
            min_size=1,
            max_size=10,
            max_inactive_connection_lifetime=300,  # recycle idle connections every 5 min
            command_timeout=60,
            server_settings={"tcp_keepalives_idle": "60"},
        )
        log.info("Database pool established")
    return _pool


async def close_pool() -> None:
    global _pool
    if _pool:
        await _pool.close()
        _pool = None
        log.info("Database pool closed")
