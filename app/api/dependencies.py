from __future__ import annotations

from typing import Annotated, Optional
from fastapi import Header, HTTPException, Depends, status
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_db_session, get_redis_client
from app.services.idempotency import IdempotencyManager


def get_required_idempotency_key(
    idempotency_key: Annotated[Optional[str], Header(alias="Idempotency-Key")] = None,
) -> str:
    """Extracts and validates mandatory Idempotency-Key header."""
    if not idempotency_key or not idempotency_key.strip():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Header 'Idempotency-Key' is required for this mutation endpoint.",
        )
    trimmed = idempotency_key.strip()
    if len(trimmed) > 255:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Header 'Idempotency-Key' must not exceed 255 characters.",
        )
    return trimmed


async def get_idempotency_manager(
    db: Annotated[AsyncSession, Depends(get_db_session)],
    redis: Annotated[Redis, Depends(get_redis_client)],
) -> IdempotencyManager:
    return IdempotencyManager(db_session=db, redis_client=redis)
