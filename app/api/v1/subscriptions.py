from __future__ import annotations

import logging
from typing import Annotated
from fastapi import APIRouter, Depends, status, Response
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import get_required_idempotency_key, get_idempotency_manager
from app.db.session import get_db_session
from app.domain.enums import SubscriptionStatus, TransactionStatus
from app.domain.exceptions import (
    IllegalStateTransitionError,
    SubscriptionNotFoundError,
)
from app.domain.models import Subscription, Transaction, utc_now
from app.schemas.subscription import (
    ChargeSubscriptionRequest,
    ChargeSubscriptionResponse,
    CreateSubscriptionRequest,
    SubscriptionResponse,
)
from app.services.fsm import SubscriptionFSM
from app.services.idempotency import IdempotencyManager

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/subscriptions", tags=["Subscriptions"])


@router.post(
    "",
    response_model=SubscriptionResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create a new subscription",
)
async def create_subscription(
    payload: CreateSubscriptionRequest,
    db: Annotated[AsyncSession, Depends(get_db_session)],
) -> SubscriptionResponse:
    subscription = Subscription(
        user_id=payload.user_id,
        plan_id=payload.plan_id,
        status=SubscriptionStatus.PENDING,
        current_period_start=utc_now(),
        current_period_end=payload.current_period_end,
    )
    db.add(subscription)
    await db.commit()
    await db.refresh(subscription)
    return SubscriptionResponse.model_validate(subscription)


@router.get(
    "/{subscription_id}",
    response_model=SubscriptionResponse,
    summary="Fetch subscription details",
)
async def get_subscription(
    subscription_id: str,
    db: Annotated[AsyncSession, Depends(get_db_session)],
) -> SubscriptionResponse:
    stmt = select(Subscription).where(Subscription.id == subscription_id)
    result = await db.execute(stmt)
    subscription = result.scalar_one_or_none()
    if not subscription:
        raise SubscriptionNotFoundError(subscription_id)
    return SubscriptionResponse.model_validate(subscription)


@router.post(
    "/charge",
    response_model=ChargeSubscriptionResponse,
    status_code=status.HTTP_200_OK,
    summary="Charge subscription with distributed idempotency guarantee",
    responses={
        200: {
            "description": "Subscription charged successfully or cached idempotent replay"
        },
        400: {"description": "Missing or malformed Idempotency-Key header"},
        404: {"description": "Subscription not found"},
        409: {
            "description": "Idempotency conflict (in flight) or illegal state transition"
        },
        422: {"description": "Idempotency key payload mismatch or validation error"},
    },
)
async def charge_subscription(
    payload: ChargeSubscriptionRequest,
    idempotency_key: Annotated[str, Depends(get_required_idempotency_key)],
    idempotency_mgr: Annotated[IdempotencyManager, Depends(get_idempotency_manager)],
    db: Annotated[AsyncSession, Depends(get_db_session)],
) -> Response:
    # 1. Compute deterministic request hash
    request_dict = payload.model_dump(mode="json")
    request_hash = idempotency_mgr.compute_request_hash(request_dict)

    # 2. Acquire lock / check cached response
    is_cached, cached, lock = await idempotency_mgr.start_operation(
        idempotency_key=idempotency_key,
        request_hash=request_hash,
    )

    if is_cached and cached is not None:
        cached_status, cached_body = cached
        return JSONResponse(
            status_code=cached_status,
            content=cached_body,
            headers={
                "X-Cache-Lookup": "HIT",
                "Idempotency-Key": idempotency_key,
            },
        )

    # 3. Execute mutation under transactional isolation
    try:
        # Acquire row-level lock on subscription
        stmt = (
            select(Subscription)
            .where(Subscription.id == payload.subscription_id)
            .with_for_update()
        )
        result = await db.execute(stmt)
        subscription = result.scalar_one_or_none()

        if not subscription:
            raise SubscriptionNotFoundError(payload.subscription_id)

        # Validate FSM state transition
        target_status = SubscriptionStatus.ACTIVE
        SubscriptionFSM.validate_transition(
            current_status=subscription.status,
            target_status=target_status,
            subscription_id=subscription.id,
        )

        # Record successful financial transaction
        transaction = Transaction(
            subscription_id=subscription.id,
            gateway_transaction_id=f"ch_{idempotency_key[:16]}",
            amount_in_cents=payload.amount_in_cents,
            currency=payload.currency,
            status=TransactionStatus.SUCCEEDED,
        )
        db.add(transaction)

        # Advance subscription state
        SubscriptionFSM.transition(
            subscription=subscription,
            target_status=target_status,
        )

        await db.commit()
        await db.refresh(transaction)
        await db.refresh(subscription)

        response_schema = ChargeSubscriptionResponse(
            transaction_id=transaction.id,
            subscription_id=subscription.id,
            amount_in_cents=transaction.amount_in_cents,
            currency=transaction.currency,
            status=transaction.status,
            subscription_status=subscription.status,
            created_at=transaction.created_at,
        )
        response_dict = response_schema.model_dump(mode="json")

        # Persist completed idempotency record and release Redis lock
        await idempotency_mgr.complete_operation(
            idempotency_key=idempotency_key,
            status_code=status.HTTP_200_OK,
            response_body=response_dict,
            lock=lock,
        )

        return JSONResponse(
            status_code=status.HTTP_200_OK,
            content=response_dict,
            headers={
                "X-Cache-Lookup": "MISS",
                "Idempotency-Key": idempotency_key,
            },
        )

    except Exception:
        # On failure, release distributed lock and reset idempotency state
        await idempotency_mgr.abort_operation(
            idempotency_key=idempotency_key, lock=lock
        )
        raise
