from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import List, Optional
from sqlalchemy import (
    String,
    Integer,
    DateTime,
    Enum as SQLEnum,
    ForeignKey,
    Index,
    UniqueConstraint,
    Text,
    BigInteger,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.domain.enums import (
    SubscriptionStatus,
    TransactionStatus,
    IdempotencyStatus,
    WebhookProcessingStatus,
)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class Subscription(Base):
    __tablename__ = "subscriptions"

    id: Mapped[str] = mapped_column(
        String(36),
        primary_key=True,
        default=lambda: str(uuid.uuid4()),
    )
    user_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    plan_id: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[SubscriptionStatus] = mapped_column(
        SQLEnum(SubscriptionStatus, native_enum=False, length=32),
        nullable=False,
        default=SubscriptionStatus.PENDING,
        index=True,
    )
    current_period_start: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utc_now,
    )
    current_period_end: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
    )
    # Event timestamp (in milliseconds or UTC datetime) of the last applied state-changing event
    last_event_timestamp: Mapped[int] = mapped_column(
        BigInteger,
        nullable=False,
        default=0,
        index=True,
    )
    # Optimistic locking version
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utc_now,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utc_now,
        onupdate=utc_now,
    )

    transactions: Mapped[List["Transaction"]] = relationship(
        "Transaction",
        back_populates="subscription",
        cascade="all, delete-orphan",
        order_by="desc(Transaction.created_at)",
    )

    __table_args__ = (Index("ix_subscriptions_user_status", "user_id", "status"),)


class Transaction(Base):
    __tablename__ = "transactions"

    id: Mapped[str] = mapped_column(
        String(36),
        primary_key=True,
        default=lambda: str(uuid.uuid4()),
    )
    subscription_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("subscriptions.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    gateway_transaction_id: Mapped[Optional[str]] = mapped_column(
        String(128),
        nullable=True,
        index=True,
    )
    amount_in_cents: Mapped[int] = mapped_column(Integer, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False, default="USD")
    status: Mapped[TransactionStatus] = mapped_column(
        SQLEnum(TransactionStatus, native_enum=False, length=32),
        nullable=False,
        default=TransactionStatus.PENDING,
        index=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utc_now,
    )

    subscription: Mapped["Subscription"] = relationship(
        "Subscription",
        back_populates="transactions",
    )


class IdempotencyRecord(Base):
    __tablename__ = "idempotency_records"

    idempotency_key: Mapped[str] = mapped_column(
        String(255),
        primary_key=True,
    )
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[IdempotencyStatus] = mapped_column(
        SQLEnum(IdempotencyStatus, native_enum=False, length=32),
        nullable=False,
        default=IdempotencyStatus.STARTED,
        index=True,
    )
    response_code: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    response_body: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utc_now,
    )
    locked_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utc_now,
    )
    expires_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )

    __table_args__ = (
        Index("ix_idempotency_key_hash", "idempotency_key", "request_hash"),
    )


class WebhookEvent(Base):
    __tablename__ = "webhook_events"

    id: Mapped[str] = mapped_column(
        String(36),
        primary_key=True,
        default=lambda: str(uuid.uuid4()),
    )
    gateway: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    event_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    event_timestamp: Mapped[int] = mapped_column(BigInteger, nullable=False)
    payload: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[WebhookProcessingStatus] = mapped_column(
        SQLEnum(WebhookProcessingStatus, native_enum=False, length=32),
        nullable=False,
        default=WebhookProcessingStatus.PROCESSED,
        index=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utc_now,
    )

    __table_args__ = (
        UniqueConstraint("gateway", "event_id", name="uq_gateway_event_id"),
    )
