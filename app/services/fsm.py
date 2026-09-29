from __future__ import annotations

import logging
from typing import Dict, FrozenSet, Optional

from app.domain.enums import SubscriptionStatus
from app.domain.exceptions import IllegalStateTransitionError
from app.domain.models import Subscription

logger = logging.getLogger(__name__)


class SubscriptionFSM:
    """
    Deterministic Finite State Machine (FSM) governing subscription lifecycles.
    
    States:
        PENDING   -> Initial state awaiting first charge.
        ACTIVE    -> Valid recurring subscriber in good standing.
        PAST_DUE  -> Payment failure occurred; entering grace/dunning period.
        CANCELED  -> Terminal state; service stopped, no further transitions permitted.

    Transition Graph:
        PENDING   -> { ACTIVE, CANCELED, PENDING }
        ACTIVE    -> { PAST_DUE, CANCELED, ACTIVE }
        PAST_DUE  -> { ACTIVE, CANCELED, PAST_DUE }
        CANCELED  -> { CANCELED } (Terminal: no outbound transitions to other states)
    """

    # Strict transition graph mapping source state to set of allowable destination states
    _TRANSITION_RULES: Dict[SubscriptionStatus, FrozenSet[SubscriptionStatus]] = {
        SubscriptionStatus.PENDING: frozenset(
            {SubscriptionStatus.ACTIVE, SubscriptionStatus.CANCELED, SubscriptionStatus.PENDING}
        ),
        SubscriptionStatus.ACTIVE: frozenset(
            {SubscriptionStatus.PAST_DUE, SubscriptionStatus.CANCELED, SubscriptionStatus.ACTIVE}
        ),
        SubscriptionStatus.PAST_DUE: frozenset(
            {SubscriptionStatus.ACTIVE, SubscriptionStatus.CANCELED, SubscriptionStatus.PAST_DUE}
        ),
        SubscriptionStatus.CANCELED: frozenset(
            {SubscriptionStatus.CANCELED}
        ),
    }

    @classmethod
    def is_transition_allowed(
        cls, current_status: SubscriptionStatus, target_status: SubscriptionStatus
    ) -> bool:
        """Checks if a transition between two states is permitted."""
        allowed_targets = cls._TRANSITION_RULES.get(current_status, frozenset())
        return target_status in allowed_targets

    @classmethod
    def validate_transition(
        cls,
        current_status: SubscriptionStatus,
        target_status: SubscriptionStatus,
        subscription_id: Optional[str] = None,
    ) -> None:
        """
        Validates the proposed transition. Raises IllegalStateTransitionError if illegal.
        Self-transitions are allowed idempotently.
        """
        if current_status == SubscriptionStatus.CANCELED and target_status != SubscriptionStatus.CANCELED:
            raise IllegalStateTransitionError(
                current_status=current_status.value,
                target_status=target_status.value,
                reason="Subscription is in terminal 'CANCELED' state and cannot be reactivated or altered.",
                subscription_id=subscription_id,
            )

        if not cls.is_transition_allowed(current_status, target_status):
            raise IllegalStateTransitionError(
                current_status=current_status.value,
                target_status=target_status.value,
                reason=f"Transition from '{current_status.value}' to '{target_status.value}' is not permitted by lifecycle rules.",
                subscription_id=subscription_id,
            )

    @classmethod
    def transition(
        cls,
        subscription: Subscription,
        target_status: SubscriptionStatus,
        event_timestamp: Optional[int] = None,
    ) -> bool:
        """
        Applies state transition on a subscription model in-memory.
        Returns True if status changed, False if it was an idempotent self-transition.
        """
        cls.validate_transition(
            current_status=subscription.status,
            target_status=target_status,
            subscription_id=subscription.id,
        )

        if subscription.status == target_status:
            logger.info(
                f"[FSM] Idempotent self-transition for subscription '{subscription.id}': "
                f"already in state '{target_status.value}'"
            )
            return False

        old_status = subscription.status
        subscription.status = target_status
        subscription.version += 1
        if event_timestamp is not None and event_timestamp > subscription.last_event_timestamp:
            subscription.last_event_timestamp = event_timestamp

        logger.info(
            f"[FSM] Transition applied for subscription '{subscription.id}': "
            f"'{old_status.value}' -> '{target_status.value}' (version={subscription.version})"
        )
        return True

    @classmethod
    def map_event_to_target_status(cls, event_type: str) -> Optional[SubscriptionStatus]:
        """Maps normalized gateway event types to deterministic subscription target states."""
        normalized = event_type.lower().strip()
        if normalized in {
            "invoice.payment_succeeded",
            "payment.succeeded",
            "charge.succeeded",
            "subscription.payment_succeeded",
        }:
            return SubscriptionStatus.ACTIVE
        elif normalized in {
            "invoice.payment_failed",
            "payment.failed",
            "charge.failed",
            "subscription.payment_failed",
        }:
            return SubscriptionStatus.PAST_DUE
        elif normalized in {
            "customer.subscription.deleted",
            "subscription.canceled",
            "subscription.deleted",
        }:
            return SubscriptionStatus.CANCELED
        return None
