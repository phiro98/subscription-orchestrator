from __future__ import annotations

import hashlib
import json
import logging
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone, timedelta
from typing import Any, AsyncGenerator, Dict, Optional, Tuple

from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.domain.enums import IdempotencyStatus
from app.domain.exceptions import (
    IdempotencyConflictError,
    IdempotencyKeyPayloadMismatchError,
)
from app.domain.models import IdempotencyRecord, utc_now

logger = logging.getLogger(__name__)
settings = get_settings()

# Lua script to release Redis lock atomically only if token matches
LUA_RELEASE_LOCK = """
if redis.call("get", KEYS[1]) == ARGV[1] then
    return redis.call("del", KEYS[1])
else
    return 0
end
"""


def normalize_utc(dt: Optional[datetime]) -> Optional[datetime]:
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


class DistributedLock:
    """Atomic Redis distributed lock with lease expiration and safe token verification."""

    def __init__(
        self,
        redis_client: Redis,
        resource_key: str,
        ttl_seconds: int = 15,
    ):
        self.redis = redis_client
        self.lock_key = f"lock:idempotency:{resource_key}"
        self.ttl_seconds = ttl_seconds
        self.token = str(uuid.uuid4())
        self._acquired = False

    async def acquire(self) -> bool:
        """Attempts to atomically acquire the lock via SET key val NX EX."""
        acquired = await self.redis.set(
            self.lock_key,
            self.token,
            nx=True,
            ex=self.ttl_seconds,
        )
        self._acquired = bool(acquired)
        return self._acquired

    async def release(self) -> bool:
        """Atomically releases lock using Lua script to verify token ownership."""
        if not self._acquired:
            return False
        try:
            res = await self.redis.eval(
                LUA_RELEASE_LOCK,
                1,
                self.lock_key,
                self.token,
            )
            return bool(res)
        except Exception:
            # Fallback for environments where Lua eval might be constrained
            try:
                current_val = await self.redis.get(self.lock_key)
                if current_val == self.token:
                    await self.redis.delete(self.lock_key)
                    return True
            except Exception as e:
                logger.warning(f"Error releasing Redis lock for {self.lock_key}: {e}")
            return False
        finally:
            self._acquired = False

    async def __aenter__(self) -> "DistributedLock":
        acquired = await self.acquire()
        if not acquired:
            raise IdempotencyConflictError(self.lock_key)
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.release()


class IdempotencyManager:
    """
    Coordinates Redis distributed locking and PostgreSQL durable storage to ensure
    strict exactly-once execution semantics for mutating API requests.
    """

    def __init__(self, db_session: AsyncSession, redis_client: Redis):
        self.db = db_session
        self.redis = redis_client

    @staticmethod
    def compute_request_hash(payload: Any) -> str:
        """Computes deterministic SHA-256 hash of canonicalized request payload."""
        if payload is None:
            raw = b""
        elif isinstance(payload, bytes):
            raw = payload
        elif isinstance(payload, str):
            raw = payload.encode("utf-8")
        else:
            raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode(
                "utf-8"
            )
        return hashlib.sha256(raw).hexdigest()

    async def get_existing_record(
        self, idempotency_key: str
    ) -> Optional[IdempotencyRecord]:
        """Fetches durable record from database."""
        stmt = select(IdempotencyRecord).where(
            IdempotencyRecord.idempotency_key == idempotency_key
        )
        result = await self.db.execute(stmt)
        return result.scalar_one_or_none()

    async def start_operation(
        self,
        idempotency_key: str,
        request_hash: str,
        ttl_seconds: int = 15,
    ) -> Tuple[bool, Optional[Tuple[int, Dict[str, Any]]], Optional[DistributedLock]]:
        """
        Initiates idempotency protocol:
        Returns:
            (is_cached, cached_response, lock_instance)
            - If is_cached is True, cached_response contains (status_code, body_dict).
            - If is_cached is False, operation proceeds; lock_instance must be released on finish.
        Raises:
            - IdempotencyKeyPayloadMismatchError (422) if key is reused with different payload.
            - IdempotencyConflictError (409) if an identical operation is currently in flight.
        """
        # Step 1: Check durable PostgreSQL store
        record = await self.get_existing_record(idempotency_key)
        if record is not None:
            # Validate hash consistency
            if record.request_hash != request_hash:
                logger.warning(
                    f"Idempotency payload mismatch for key '{idempotency_key}'. "
                    f"Stored: {record.request_hash}, Provided: {request_hash}"
                )
                raise IdempotencyKeyPayloadMismatchError(idempotency_key)

            if record.status == IdempotencyStatus.COMPLETED:
                body = json.loads(record.response_body) if record.response_body else {}
                logger.info(
                    f"Replaying cached response for idempotency key '{idempotency_key}'"
                )
                return True, (record.response_code or 200, body), None

            if record.status == IdempotencyStatus.STARTED:
                # If the lock lease is still valid, reject concurrent execution
                expires_at_utc = normalize_utc(record.expires_at)
                if expires_at_utc and expires_at_utc > utc_now():
                    logger.warning(
                        f"In-flight mutation detected for key '{idempotency_key}'"
                    )
                    raise IdempotencyConflictError(idempotency_key)

        # Step 2: Acquire Redis distributed lock
        lock = DistributedLock(self.redis, idempotency_key, ttl_seconds=ttl_seconds)
        acquired = await lock.acquire()
        if not acquired:
            logger.warning(
                f"Redis lock acquisition denied for key '{idempotency_key}' (in-flight)"
            )
            raise IdempotencyConflictError(idempotency_key)

        # Step 3: Record STARTED state in PostgreSQL
        now = utc_now()
        expires_at = now + timedelta(seconds=ttl_seconds)
        if record is None:
            new_record = IdempotencyRecord(
                idempotency_key=idempotency_key,
                request_hash=request_hash,
                status=IdempotencyStatus.STARTED,
                locked_at=now,
                expires_at=expires_at,
            )
            self.db.add(new_record)
        else:
            record.status = IdempotencyStatus.STARTED
            record.locked_at = now
            record.expires_at = expires_at

        await self.db.commit()
        return False, None, lock

    async def complete_operation(
        self,
        idempotency_key: str,
        status_code: int,
        response_body: Dict[str, Any],
        lock: Optional[DistributedLock],
    ) -> None:
        """Marks operation as COMPLETED with durable cached response, and releases Redis lock."""
        try:
            record = await self.get_existing_record(idempotency_key)
            if record:
                record.status = IdempotencyStatus.COMPLETED
                record.response_code = status_code
                record.response_body = json.dumps(response_body, default=str)
                record.expires_at = utc_now() + timedelta(
                    seconds=settings.IDEMPOTENCY_RETENTION_SECONDS
                )
                await self.db.commit()
        finally:
            if lock:
                await lock.release()

    async def abort_operation(
        self,
        idempotency_key: str,
        lock: Optional[DistributedLock],
    ) -> None:
        """Cleans up operation state on failure so client can retry safely."""
        try:
            record = await self.get_existing_record(idempotency_key)
            if record and record.status == IdempotencyStatus.STARTED:
                record.status = IdempotencyStatus.FAILED
                await self.db.commit()
        except Exception as e:
            logger.error(
                f"Failed to update failed idempotency record '{idempotency_key}': {e}"
            )
        finally:
            if lock:
                await lock.release()
