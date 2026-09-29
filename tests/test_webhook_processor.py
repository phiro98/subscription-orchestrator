from __future__ import annotations

import hmac
import hashlib
import json
import time
from datetime import datetime, timezone, timedelta
import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.domain.enums import (
    SubscriptionStatus,
    TransactionStatus,
    WebhookProcessingStatus,
)
from app.domain.exceptions import (
    InvalidWebhookSignatureError,
    WebhookReplayAttackError,
)
from app.domain.models import Subscription
from app.schemas.webhook import GatewayWebhookPayload, WebhookPayloadData
from app.services.webhook_processor import WebhookProcessor

settings = get_settings()


def generate_signature(payload_bytes: bytes, gateway: str, timestamp: int) -> str:
    secret = settings.WEBHOOK_SECRETS[gateway]
    signed_payload = f"{timestamp}.".encode("utf-8") + payload_bytes
    sig = hmac.new(secret.encode("utf-8"), signed_payload, hashlib.sha256).hexdigest()
    return f"t={timestamp},v1={sig}"


@pytest.mark.asyncio
async def test_webhook_hmac_signature_verification():
    body = b'{"hello": "world"}'
    ts = int(time.time())
    sig = generate_signature(body, "stripe", ts)

    # Valid signature
    verified_ts = WebhookProcessor.verify_signature(body, sig, "stripe")
    assert verified_ts == ts

    # Tampered body fails verification
    tampered_body = b'{"hello": "hacked"}'
    with pytest.raises(InvalidWebhookSignatureError):
        WebhookProcessor.verify_signature(tampered_body, sig, "stripe")


@pytest.mark.asyncio
async def test_webhook_replay_attack_rejected():
    body = b'{"hello": "world"}'
    # Timestamp 10 minutes in the past
    stale_ts = int(time.time()) - 600
    sig = generate_signature(body, "stripe", stale_ts)

    with pytest.raises(WebhookReplayAttackError):
        WebhookProcessor.verify_signature(body, sig, "stripe", tolerance_seconds=300)


@pytest.mark.asyncio
async def test_webhook_out_of_order_protection(db_session: AsyncSession):
    # Setup initial active subscription with last_event_timestamp = 2000
    now = datetime.now(timezone.utc)
    sub = Subscription(
        id="sub_test_ooo",
        user_id="usr_ooo",
        plan_id="plan_pro",
        status=SubscriptionStatus.ACTIVE,
        current_period_start=now,
        current_period_end=now + timedelta(days=30),
        last_event_timestamp=2000,
        version=1,
    )
    db_session.add(sub)
    await db_session.commit()

    processor = WebhookProcessor(db_session=db_session)

    # Stale webhook arrives: payment failed with timestamp 1500 (< 2000)
    stale_payload = GatewayWebhookPayload(
        id="evt_stale_001",
        type="invoice.payment_failed",
        created=1500,
        data=WebhookPayloadData(
            subscription_id="sub_test_ooo",
            amount_in_cents=2999,
        ),
    )

    result = await processor.process_webhook(
        gateway="stripe",
        event_payload=stale_payload,
        raw_body_str=stale_payload.model_dump_json(),
    )

    assert result.status == WebhookProcessingStatus.IGNORED_OUT_OF_ORDER
    # Ensure subscription remains ACTIVE and not regressed to PAST_DUE
    await db_session.refresh(sub)
    assert sub.status == SubscriptionStatus.ACTIVE
    assert sub.last_event_timestamp == 2000


@pytest.mark.asyncio
async def test_webhook_newer_event_advances_state_and_deduplicates(db_session: AsyncSession):
    now = datetime.now(timezone.utc)
    sub = Subscription(
        id="sub_test_flow",
        user_id="usr_flow",
        plan_id="plan_pro",
        status=SubscriptionStatus.ACTIVE,
        current_period_start=now,
        current_period_end=now + timedelta(days=30),
        last_event_timestamp=1000,
        version=1,
    )
    db_session.add(sub)
    await db_session.commit()

    processor = WebhookProcessor(db_session=db_session)

    # 1. Process newer failure event at timestamp 3000
    failure_payload = GatewayWebhookPayload(
        id="evt_fail_002",
        type="invoice.payment_failed",
        created=3000,
        data=WebhookPayloadData(
            subscription_id="sub_test_flow",
            amount_in_cents=2999,
        ),
    )

    result1 = await processor.process_webhook(
        gateway="stripe",
        event_payload=failure_payload,
        raw_body_str=failure_payload.model_dump_json(),
    )
    assert result1.status == WebhookProcessingStatus.PROCESSED

    await db_session.refresh(sub)
    assert sub.status == SubscriptionStatus.PAST_DUE
    assert sub.last_event_timestamp == 3000

    # 2. Duplicate event delivery of the same evt_fail_002
    result2 = await processor.process_webhook(
        gateway="stripe",
        event_payload=failure_payload,
        raw_body_str=failure_payload.model_dump_json(),
    )
    assert result2.status == WebhookProcessingStatus.DUPLICATE
