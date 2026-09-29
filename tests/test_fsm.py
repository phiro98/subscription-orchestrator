from __future__ import annotations

from datetime import datetime, timezone, timedelta
import pytest

from app.domain.enums import SubscriptionStatus
from app.domain.exceptions import IllegalStateTransitionError
from app.domain.models import Subscription
from app.services.fsm import SubscriptionFSM


def create_sample_subscription(status: SubscriptionStatus) -> Subscription:
    now = datetime.now(timezone.utc)
    return Subscription(
        id="sub_test_12345",
        user_id="usr_001",
        plan_id="plan_standard",
        status=status,
        current_period_start=now,
        current_period_end=now + timedelta(days=30),
        last_event_timestamp=1000,
        version=1,
    )


def test_valid_transitions():
    sub = create_sample_subscription(SubscriptionStatus.PENDING)

    # PENDING -> ACTIVE
    changed = SubscriptionFSM.transition(
        sub, SubscriptionStatus.ACTIVE, event_timestamp=1100
    )
    assert changed is True
    assert sub.status == SubscriptionStatus.ACTIVE
    assert sub.version == 2
    assert sub.last_event_timestamp == 1100

    # ACTIVE -> PAST_DUE
    changed = SubscriptionFSM.transition(
        sub, SubscriptionStatus.PAST_DUE, event_timestamp=1200
    )
    assert changed is True
    assert sub.status == SubscriptionStatus.PAST_DUE
    assert sub.version == 3

    # PAST_DUE -> ACTIVE
    changed = SubscriptionFSM.transition(
        sub, SubscriptionStatus.ACTIVE, event_timestamp=1300
    )
    assert changed is True
    assert sub.status == SubscriptionStatus.ACTIVE
    assert sub.version == 4

    # ACTIVE -> CANCELED
    changed = SubscriptionFSM.transition(
        sub, SubscriptionStatus.CANCELED, event_timestamp=1400
    )
    assert changed is True
    assert sub.status == SubscriptionStatus.CANCELED
    assert sub.version == 5


def test_terminal_canceled_state_blocks_all_outbound_transitions():
    sub = create_sample_subscription(SubscriptionStatus.CANCELED)

    with pytest.raises(IllegalStateTransitionError) as exc_info:
        SubscriptionFSM.transition(sub, SubscriptionStatus.ACTIVE)
    assert exc_info.value.status_code == 409
    assert "CANCELED" in exc_info.value.detail
    assert "terminal" in exc_info.value.detail.lower()

    with pytest.raises(IllegalStateTransitionError):
        SubscriptionFSM.transition(sub, SubscriptionStatus.PAST_DUE)

    with pytest.raises(IllegalStateTransitionError):
        SubscriptionFSM.transition(sub, SubscriptionStatus.PENDING)


def test_illegal_jump_from_pending_to_past_due():
    sub = create_sample_subscription(SubscriptionStatus.PENDING)

    with pytest.raises(IllegalStateTransitionError) as exc_info:
        # A pending subscription cannot go directly to PAST_DUE without being active
        SubscriptionFSM.transition(sub, SubscriptionStatus.PAST_DUE)
    assert exc_info.value.status_code == 409
    assert exc_info.value.extra["current_status"] == "PENDING"
    assert exc_info.value.extra["target_status"] == "PAST_DUE"


def test_idempotent_self_transitions():
    sub = create_sample_subscription(SubscriptionStatus.ACTIVE)
    initial_version = sub.version

    # Transitioning ACTIVE -> ACTIVE should return False (no change) and not fail
    changed = SubscriptionFSM.transition(
        sub, SubscriptionStatus.ACTIVE, event_timestamp=2000
    )
    assert changed is False
    assert sub.status == SubscriptionStatus.ACTIVE
    assert sub.version == initial_version
