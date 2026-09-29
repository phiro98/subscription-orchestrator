from __future__ import annotations

import hmac
import hashlib
import json
import time
from datetime import datetime, timezone, timedelta
import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.domain.enums import SubscriptionStatus
from app.domain.models import Subscription

settings = get_settings()


def make_stripe_signature(payload_bytes: bytes, timestamp: int) -> str:
    secret = settings.WEBHOOK_SECRETS["stripe"]
    signed_payload = f"{timestamp}.".encode("utf-8") + payload_bytes
    sig = hmac.new(secret.encode("utf-8"), signed_payload, hashlib.sha256).hexdigest()
    return f"t={timestamp},v1={sig}"


@pytest.mark.asyncio
async def test_charge_subscription_flow_and_idempotency_replay(
    test_client: AsyncClient, db_session: AsyncSession
):
    # 1. Create a subscription in DB
    now = datetime.now(timezone.utc)
    sub = Subscription(
        id="sub_charge_100",
        user_id="usr_100",
        plan_id="plan_gold",
        status=SubscriptionStatus.PENDING,
        current_period_start=now,
        current_period_end=now + timedelta(days=30),
        last_event_timestamp=0,
        version=1,
    )
    db_session.add(sub)
    await db_session.commit()

    charge_body = {
        "subscription_id": "sub_charge_100",
        "amount_in_cents": 4999,
        "currency": "USD",
    }
    headers = {"Idempotency-Key": "test_idem_key_100"}

    # 2. First charge attempt
    response1 = await test_client.post(
        "/v1/subscriptions/charge",
        json=charge_body,
        headers=headers,
    )
    assert response1.status_code == 200
    assert response1.headers["X-Cache-Lookup"] == "MISS"
    data1 = response1.json()
    assert data1["subscription_status"] == "ACTIVE"
    assert data1["amount_in_cents"] == 4999

    # 3. Repeated charge with identical Idempotency-Key
    response2 = await test_client.post(
        "/v1/subscriptions/charge",
        json=charge_body,
        headers=headers,
    )
    assert response2.status_code == 200
    assert response2.headers["X-Cache-Lookup"] == "HIT"
    data2 = response2.json()
    assert data2["transaction_id"] == data1["transaction_id"]


@pytest.mark.asyncio
async def test_charge_missing_idempotency_header_rejected(test_client: AsyncClient):
    charge_body = {
        "subscription_id": "sub_any",
        "amount_in_cents": 1000,
        "currency": "USD",
    }
    response = await test_client.post("/v1/subscriptions/charge", json=charge_body)
    assert response.status_code == 400
    problem = response.json()
    assert "Idempotency-Key" in problem["detail"]


@pytest.mark.asyncio
async def test_charge_canceled_subscription_raises_rfc7807_conflict(
    test_client: AsyncClient, db_session: AsyncSession
):
    now = datetime.now(timezone.utc)
    sub = Subscription(
        id="sub_canceled_200",
        user_id="usr_200",
        plan_id="plan_basic",
        status=SubscriptionStatus.CANCELED,
        current_period_start=now,
        current_period_end=now + timedelta(days=30),
        last_event_timestamp=1000,
        version=1,
    )
    db_session.add(sub)
    await db_session.commit()

    charge_body = {
        "subscription_id": "sub_canceled_200",
        "amount_in_cents": 1500,
        "currency": "USD",
    }
    headers = {"Idempotency-Key": "idem_key_canceled_test"}

    response = await test_client.post(
        "/v1/subscriptions/charge",
        json=charge_body,
        headers=headers,
    )
    assert response.status_code == 409
    assert response.headers["Content-Type"] == "application/problem+json"
    problem = response.json()
    assert problem["title"] == "Illegal State Transition"
    assert "CANCELED" in problem["detail"]
    assert problem["instance"] == "/v1/subscriptions/charge"


@pytest.mark.asyncio
async def test_webhook_api_success_and_invalid_signature(
    test_client: AsyncClient, db_session: AsyncSession
):
    now = datetime.now(timezone.utc)
    sub = Subscription(
        id="sub_wh_300",
        user_id="usr_300",
        plan_id="plan_basic",
        status=SubscriptionStatus.ACTIVE,
        current_period_start=now,
        current_period_end=now + timedelta(days=30),
        last_event_timestamp=100,
        version=1,
    )
    db_session.add(sub)
    await db_session.commit()

    event_payload = {
        "id": "evt_api_300",
        "type": "invoice.payment_failed",
        "created": int(time.time()),
        "data": {
            "subscription_id": "sub_wh_300",
            "amount_in_cents": 1200,
        },
    }
    raw_bytes = json.dumps(event_payload).encode("utf-8")
    sig = make_stripe_signature(raw_bytes, event_payload["created"])

    # Valid webhook request
    response = await test_client.post(
        "/v1/webhooks/stripe",
        content=raw_bytes,
        headers={"Stripe-Signature": sig},
    )
    assert response.status_code == 200
    res_data = response.json()
    assert res_data["status"] == "PROCESSED"

    # Verify state updated in DB
    await db_session.refresh(sub)
    assert sub.status == SubscriptionStatus.PAST_DUE

    # Invalid signature test
    now_ts = int(time.time())
    bad_response = await test_client.post(
        "/v1/webhooks/stripe",
        content=raw_bytes,
        headers={"Stripe-Signature": f"t={now_ts},v1=invalidsignature"},
    )
    assert bad_response.status_code == 401
    assert bad_response.headers["Content-Type"] == "application/problem+json"
