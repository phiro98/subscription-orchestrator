from __future__ import annotations

import asyncio
from typing import AsyncGenerator
import pytest
import pytest_asyncio
from fakeredis.aioredis import FakeRedis
from httpx import AsyncClient, ASGITransport
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.api.dependencies import get_idempotency_manager
from app.config import get_settings
from app.db.base import Base
from app.db.session import get_db_session, get_redis_client
from app.main import create_app
from app.services.idempotency import IdempotencyManager

# Test SQLite in-memory database
TEST_DB_URL = "sqlite+aiosqlite:///:memory:"


@pytest.fixture(scope="session")
def event_loop():
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


@pytest_asyncio.fixture(scope="function")
async def test_db_engine():
    engine = create_async_engine(TEST_DB_URL, echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await engine.dispose()


@pytest_asyncio.fixture(scope="function")
async def db_session(test_db_engine) -> AsyncGenerator[AsyncSession, None]:
    session_factory = async_sessionmaker(
        bind=test_db_engine,
        class_=AsyncSession,
        expire_on_commit=False,
    )
    async with session_factory() as session:
        yield session


@pytest_asyncio.fixture(scope="function")
async def fake_redis() -> AsyncGenerator[FakeRedis, None]:
    redis = FakeRedis(decode_responses=True)
    yield redis
    await redis.flushall()
    await redis.aclose()


@pytest_asyncio.fixture(scope="function")
async def idempotency_manager(db_session: AsyncSession, fake_redis: FakeRedis) -> IdempotencyManager:
    return IdempotencyManager(db_session=db_session, redis_client=fake_redis)


@pytest_asyncio.fixture(scope="function")
async def test_client(
    db_session: AsyncSession, fake_redis: FakeRedis
) -> AsyncGenerator[AsyncClient, None]:
    app = create_app()

    # Override dependencies
    async def override_get_db():
        yield db_session

    async def override_get_redis():
        yield fake_redis

    async def override_idempotency_manager():
        return IdempotencyManager(db_session=db_session, redis_client=fake_redis)

    app.dependency_overrides[get_db_session] = override_get_db
    app.dependency_overrides[get_redis_client] = override_get_redis
    app.dependency_overrides[get_idempotency_manager] = override_idempotency_manager

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        yield client
