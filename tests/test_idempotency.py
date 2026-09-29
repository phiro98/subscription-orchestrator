from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import AsyncSession
from fakeredis.aioredis import FakeRedis

from app.domain.enums import IdempotencyStatus
from app.domain.exceptions import (
    IdempotencyConflictError,
    IdempotencyKeyPayloadMismatchError,
)
from app.services.idempotency import IdempotencyManager, DistributedLock


@pytest.mark.asyncio
async def test_distributed_lock_mutual_exclusion(fake_redis: FakeRedis):
    lock1 = DistributedLock(fake_redis, "res_42", ttl_seconds=5)
    lock2 = DistributedLock(fake_redis, "res_42", ttl_seconds=5)

    assert await lock1.acquire() is True
    # Second acquisition must fail while lock1 holds it
    assert await lock2.acquire() is False

    # Release lock1
    assert await lock1.release() is True

    # Now lock2 should successfully acquire
    assert await lock2.acquire() is True
    await lock2.release()


@pytest.mark.asyncio
async def test_idempotency_workflow_first_run_then_replay(
    db_session: AsyncSession, fake_redis: FakeRedis
):
    mgr = IdempotencyManager(db_session=db_session, redis_client=fake_redis)
    key = "idem_key_001"
    payload = {"subscription_id": "sub_1", "amount": 2000}
    payload_hash = mgr.compute_request_hash(payload)

    # 1. Start operation (First time)
    is_cached, cached, lock = await mgr.start_operation(key, payload_hash)
    assert is_cached is False
    assert cached is None
    assert lock is not None

    # Simulate completed operation
    response_data = {"transaction_id": "tx_999", "status": "SUCCEEDED"}
    await mgr.complete_operation(key, 200, response_data, lock)

    # 2. Replay with identical key and payload
    is_cached, cached, lock = await mgr.start_operation(key, payload_hash)
    assert is_cached is True
    assert cached is not None
    assert lock is None
    status_code, body = cached
    assert status_code == 200
    assert body["transaction_id"] == "tx_999"


@pytest.mark.asyncio
async def test_idempotency_in_flight_conflict_raises_409(
    db_session: AsyncSession, fake_redis: FakeRedis
):
    mgr = IdempotencyManager(db_session=db_session, redis_client=fake_redis)
    key = "idem_key_conflict"
    payload = {"subscription_id": "sub_1", "amount": 2000}
    payload_hash = mgr.compute_request_hash(payload)

    # First request starts
    _, _, lock1 = await mgr.start_operation(key, payload_hash)

    # Concurrent second request attempts with same key while first is in flight
    with pytest.raises(IdempotencyConflictError) as exc_info:
        await mgr.start_operation(key, payload_hash)
    assert exc_info.value.status_code == 409

    # Clean up lock
    if lock1:
        await lock1.release()


@pytest.mark.asyncio
async def test_idempotency_payload_mismatch_raises_422(
    db_session: AsyncSession, fake_redis: FakeRedis
):
    mgr = IdempotencyManager(db_session=db_session, redis_client=fake_redis)
    key = "idem_key_mismatch"
    payload1 = {"subscription_id": "sub_1", "amount": 2000}
    payload2 = {"subscription_id": "sub_1", "amount": 5000}  # Different amount!

    hash1 = mgr.compute_request_hash(payload1)
    hash2 = mgr.compute_request_hash(payload2)

    # Complete request 1
    _, _, lock = await mgr.start_operation(key, hash1)
    await mgr.complete_operation(key, 200, {"ok": True}, lock)

    # Request with same key but different payload must be rejected
    with pytest.raises(IdempotencyKeyPayloadMismatchError) as exc_info:
        await mgr.start_operation(key, hash2)
    assert exc_info.value.status_code == 422
