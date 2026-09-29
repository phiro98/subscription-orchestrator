from __future__ import annotations

from typing import Any, Dict, Optional


class OrchestratorDomainException(Exception):
    """Base domain exception conforming to RFC 7807 (Problem Details)."""

    def __init__(
        self,
        title: str,
        detail: str,
        status_code: int = 400,
        type_uri: Optional[str] = None,
        extra: Optional[Dict[str, Any]] = None,
    ):
        super().__init__(detail)
        self.title = title
        self.detail = detail
        self.status_code = status_code
        self.type_uri = (
            type_uri
            or f"https://api.platform.internal/errors/{self.__class__.__name__}"
        )
        self.extra = extra or {}

    def to_problem_detail(self, instance: Optional[str] = None) -> Dict[str, Any]:
        doc: Dict[str, Any] = {
            "type": self.type_uri,
            "title": self.title,
            "status": self.status_code,
            "detail": self.detail,
        }
        if instance:
            doc["instance"] = instance
        if self.extra:
            doc.update(self.extra)
        return doc


class IllegalStateTransitionError(OrchestratorDomainException):
    """Raised when an FSM transition violates business rules."""

    def __init__(
        self,
        current_status: str,
        target_status: str,
        reason: Optional[str] = None,
        subscription_id: Optional[str] = None,
    ):
        detail = (
            f"Illegal state transition from '{current_status}' to '{target_status}'."
            + (f" Reason: {reason}" if reason else "")
        )
        extra = {
            "current_status": current_status,
            "target_status": target_status,
        }
        if subscription_id:
            extra["subscription_id"] = subscription_id

        super().__init__(
            title="Illegal State Transition",
            detail=detail,
            status_code=409,
            type_uri="https://api.platform.internal/errors/illegal-state-transition",
            extra=extra,
        )


class IdempotencyConflictError(OrchestratorDomainException):
    """Raised when an operation with the same Idempotency-Key is currently in flight."""

    def __init__(self, idempotency_key: str):
        super().__init__(
            title="Idempotency Conflict",
            detail=f"A mutation with idempotency key '{idempotency_key}' is currently in flight. Please retry later.",
            status_code=409,
            type_uri="https://api.platform.internal/errors/idempotency-conflict",
            extra={"idempotency_key": idempotency_key},
        )


class IdempotencyKeyPayloadMismatchError(OrchestratorDomainException):
    """Raised when an Idempotency-Key is reused with a different request payload/parameters."""

    def __init__(self, idempotency_key: str):
        super().__init__(
            title="Idempotency Key Payload Mismatch",
            detail=f"Idempotency key '{idempotency_key}' was previously used with a different request payload.",
            status_code=422,
            type_uri="https://api.platform.internal/errors/idempotency-payload-mismatch",
            extra={"idempotency_key": idempotency_key},
        )


class SubscriptionNotFoundError(OrchestratorDomainException):
    """Raised when a subscription identifier cannot be resolved."""

    def __init__(self, subscription_id: str):
        super().__init__(
            title="Subscription Not Found",
            detail=f"Subscription with ID '{subscription_id}' was not found.",
            status_code=404,
            type_uri="https://api.platform.internal/errors/subscription-not-found",
            extra={"subscription_id": subscription_id},
        )


class InvalidWebhookSignatureError(OrchestratorDomainException):
    """Raised when HMAC signature verification fails."""

    def __init__(self, gateway: str, detail: Optional[str] = None):
        super().__init__(
            title="Invalid Webhook Signature",
            detail=detail
            or f"HMAC signature verification failed for gateway '{gateway}'.",
            status_code=401,
            type_uri="https://api.platform.internal/errors/invalid-webhook-signature",
            extra={"gateway": gateway},
        )


class WebhookReplayAttackError(OrchestratorDomainException):
    """Raised when webhook timestamp falls outside acceptable clock drift window."""

    def __init__(self, timestamp: int, drift_seconds: float):
        super().__init__(
            title="Webhook Replay Detected",
            detail=f"Webhook event timestamp '{timestamp}' drifts by {drift_seconds:.1f}s, exceeding allowed tolerance.",
            status_code=400,
            type_uri="https://api.platform.internal/errors/webhook-replay-detected",
            extra={"timestamp": timestamp, "drift_seconds": drift_seconds},
        )


class DistributedLockTimeoutError(OrchestratorDomainException):
    """Raised when acquiring an atomic distributed lock exceeds acquisition timeout."""

    def __init__(self, resource_key: str):
        super().__init__(
            title="Distributed Lock Acquisition Timeout",
            detail=f"Timed out acquiring distributed lock for resource '{resource_key}'.",
            status_code=503,
            type_uri="https://api.platform.internal/errors/lock-acquisition-timeout",
            extra={"resource_key": resource_key},
        )
