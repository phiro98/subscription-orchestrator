from __future__ import annotations

import hmac
import hashlib
import json
import logging
import time
from typing import Any, Dict, Optional, Tuple
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.domain.enums import (
    SubscriptionStatus,
    TransactionStatus,
    WebhookProcessingStatus,
)
from app.domain.exceptions import (
    InvalidWebhookSignatureError,
    SubscriptionNotFoundError,
    WebhookReplayAttackError,
)
from app.domain.models import Subscription, Transaction, WebhookEvent, utc_now
from app.schemas.webhook import GatewayWebhookPayload, WebhookProcessingResult
from app.services.fsm import SubscriptionFSM

logger = logging.getLogger(__name__)
settings = get_settings()


class WebhookProcessor:
    """
    Robust payment gateway webhook processor ensuring:
    1. HMAC cryptographic signature verification with timing-attack prevention.
    2. Clock drift replay attack protection.
    3. Exactly-once idempotency deduplication.
    4. Row-level pessimistic locking (SELECT FOR UPDATE) to eliminate race conditions.
    5. Deterministic event timestamp sequencing to reject out-of-order deliveries.
    """

    def __init__(self, db_session: AsyncSession):
        self.db = db_session

    @staticmethod
    def verify_signature(
        raw_payload: bytes,
        signature_header: str,
        gateway: str,
        tolerance_seconds: int = 300,
    ) -> int:
        """
        Verifies HMAC SHA-256 signature against gateway secret.
        Expected header format:
            t=<timestamp>,v1=<hex_signature> (Stripe/Industry standard)
            or raw hex signature.
        Returns the verified event timestamp.
        """
        secret = settings.WEBHOOK_SECRETS.get(gateway.lower())
        if not secret:
            raise InvalidWebhookSignatureError(
                gateway,
                f"No webhook verification secret configured for gateway '{gateway}'.",
            )

        timestamp: Optional[int] = None
        extracted_sig: Optional[str] = None

        if "t=" in signature_header and "v1=" in signature_header:
            parts = signature_header.split(",")
            for part in parts:
                key, _, val = part.partition("=")
                key = key.strip()
                val = val.strip()
                if key == "t":
                    try:
                        timestamp = int(val)
                    except ValueError:
                        pass
                elif key == "v1":
                    extracted_sig = val
        else:
            extracted_sig = signature_header.strip()

        if not extracted_sig:
            raise InvalidWebhookSignatureError(
                gateway, "Missing or malformed signature header."
            )

        # Replay attack prevention check
        now = int(time.time())
        if timestamp is not None:
            drift = abs(now - timestamp)
            if drift > tolerance_seconds:
                raise WebhookReplayAttackError(timestamp=timestamp, drift_seconds=drift)
            signed_payload = f"{timestamp}.".encode("utf-8") + raw_payload
        else:
            signed_payload = raw_payload
            timestamp = now

        expected_sig = hmac.new(
            secret.encode("utf-8"),
            signed_payload,
            hashlib.sha256,
        ).hexdigest()

        if not hmac.compare_digest(expected_sig, extracted_sig):
            raise InvalidWebhookSignatureError(gateway, "HMAC signature mismatch.")

        return timestamp

    async def get_existing_event(
        self, gateway: str, event_id: str
    ) -> Optional[WebhookEvent]:
        """Queries database for previously ingested webhook event."""
        stmt = select(WebhookEvent).where(
            WebhookEvent.gateway == gateway,
            WebhookEvent.event_id == event_id,
        )
        result = await self.db.execute(stmt)
        return result.scalar_one_or_none()

    async def process_webhook(
        self,
        gateway: str,
        event_payload: GatewayWebhookPayload,
        raw_body_str: str,
    ) -> WebhookProcessingResult:
        """
        Processes an incoming webhook event transactionally.
        """
        event_id = event_payload.id
        event_type = event_payload.type
        event_timestamp = event_payload.created
        sub_id = event_payload.data.subscription_id

        # 1. Deduplication Check
        existing_event = await self.get_existing_event(gateway, event_id)
        if existing_event is not None:
            logger.info(
                f"Duplicate webhook received: gateway={gateway}, event_id={event_id}. Skipping."
            )
            return WebhookProcessingResult(
                status=WebhookProcessingStatus.DUPLICATE,
                event_id=event_id,
                message=f"Event '{event_id}' has already been processed previously.",
                subscription_id=sub_id,
            )

        # 2. Acquire transactional row-level lock on Subscription (SELECT ... FOR UPDATE)
        # Guarantees no concurrent transactions can interleave or modify the subscription
        stmt = select(Subscription).where(Subscription.id == sub_id).with_for_update()
        result = await self.db.execute(stmt)
        subscription = result.scalar_one_or_none()

        if subscription is None:
            raise SubscriptionNotFoundError(sub_id)

        # 3. Deterministic Out-of-Order Delivery Detection
        # If incoming event timestamp is older than the subscription's latest applied event,
        # we reject this state change to prevent regressing state.
        if event_timestamp < subscription.last_event_timestamp:
            logger.warning(
                f"[WebhookProcessor] Out-of-order event detected for subscription {sub_id}! "
                f"Event {event_id} ({event_type}) has timestamp {event_timestamp}, but "
                f"subscription last_event_timestamp is {subscription.last_event_timestamp}. Ignoring state change."
            )
            audit_event = WebhookEvent(
                gateway=gateway,
                event_id=event_id,
                event_type=event_type,
                event_timestamp=event_timestamp,
                payload=raw_body_str,
                status=WebhookProcessingStatus.IGNORED_OUT_OF_ORDER,
            )
            self.db.add(audit_event)
            await self.db.commit()

            return WebhookProcessingResult(
                status=WebhookProcessingStatus.IGNORED_OUT_OF_ORDER,
                event_id=event_id,
                message=(
                    f"Event timestamp {event_timestamp} is older than subscription's "
                    f"last applied event {subscription.last_event_timestamp}. State transition ignored."
                ),
                subscription_id=sub_id,
            )

        # 4. Map Event to State Transition and advance FSM
        target_status = SubscriptionFSM.map_event_to_target_status(event_type)
        if target_status is not None:
            SubscriptionFSM.transition(
                subscription=subscription,
                target_status=target_status,
                event_timestamp=event_timestamp,
            )

        # 5. Record Transaction record if financial mutation
        if event_payload.data.amount_in_cents is not None:
            tx_status = (
                TransactionStatus.SUCCEEDED
                if target_status == SubscriptionStatus.ACTIVE
                else TransactionStatus.FAILED
            )
            tx = Transaction(
                subscription_id=subscription.id,
                gateway_transaction_id=event_payload.data.gateway_transaction_id
                or f"gtx_{event_id}",
                amount_in_cents=event_payload.data.amount_in_cents,
                currency=event_payload.data.currency or "USD",
                status=tx_status,
            )
            self.db.add(tx)

        # 6. Record WebhookEvent audit record
        audit_event = WebhookEvent(
            gateway=gateway,
            event_id=event_id,
            event_type=event_type,
            event_timestamp=event_timestamp,
            payload=raw_body_str,
            status=WebhookProcessingStatus.PROCESSED,
        )
        self.db.add(audit_event)

        # 7. Commit atomic transaction (releases row-level lock)
        await self.db.commit()
        await self.db.refresh(subscription)

        logger.info(
            f"Successfully processed webhook: gateway={gateway}, event={event_id}, "
            f"subscription={sub_id}, new_status={subscription.status.value}"
        )

        return WebhookProcessingResult(
            status=WebhookProcessingStatus.PROCESSED,
            event_id=event_id,
            message=f"Event applied successfully. Subscription is now {subscription.status.value}.",
            subscription_id=sub_id,
        )
