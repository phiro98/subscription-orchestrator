from __future__ import annotations

import logging
from typing import AsyncGenerator
from redis.asyncio import Redis, ConnectionPool
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.config import get_settings

logger = logging.getLogger(__name__)

settings = get_settings()

# Engine setup
engine_kwargs = {
    "echo": settings.DATABASE_ECHO,
    "future": True,
}

# Add pool parameters only for postgresql (sqlite in tests doesn't use queue pool)
if "postgresql" in settings.DATABASE_URL:
    engine_kwargs.update(
        {
            "pool_size": settings.DB_POOL_SIZE,
            "max_overflow": settings.DB_MAX_OVERFLOW,
            "pool_timeout": settings.DB_POOL_TIMEOUT,
            "pool_pre_ping": True,
        }
    )

engine = create_async_engine(settings.DATABASE_URL, **engine_kwargs)

async_session_maker = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autoflush=False,
)

# Redis connection pool
redis_pool: ConnectionPool | None = None


def get_redis_pool() -> ConnectionPool:
    global redis_pool
    if redis_pool is None:
        redis_pool = ConnectionPool.from_url(
            settings.REDIS_URL,
            decode_responses=True,
            max_connections=50,
        )
    return redis_pool


async def get_redis_client() -> AsyncGenerator[Redis, None]:
    pool = get_redis_pool()
    client = Redis(connection_pool=pool)
    try:
        yield client
    finally:
        await client.aclose()


async def get_db_session() -> AsyncGenerator[AsyncSession, None]:
    async with async_session_maker() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
