from __future__ import annotations

from enum import Enum


class SubscriptionStatus(str, Enum):
    PENDING = "PENDING"
    ACTIVE = "ACTIVE"
    PAST_DUE = "PAST_DUE"
    CANCELED = "CANCELED"  # Terminal state


class TransactionStatus(str, Enum):
    PENDING = "PENDING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    REFUNDED = "REFUNDED"


class IdempotencyStatus(str, Enum):
    STARTED = "STARTED"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


class WebhookProcessingStatus(str, Enum):
    PROCESSED = "PROCESSED"
    IGNORED_OUT_OF_ORDER = "IGNORED_OUT_OF_ORDER"
    DUPLICATE = "DUPLICATE"
    FAILED = "FAILED"
