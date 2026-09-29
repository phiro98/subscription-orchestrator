"""Initial schema for subscription orchestrator

Revision ID: 001_initial_schema
Revises:
Create Date: 2026-09-24 23:30:00.000000

"""

from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa

revision: str = "001_initial_schema"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1. Subscriptions table
    op.create_table(
        "subscriptions",
        sa.Column("id", sa.String(length=36), primary_key=True, nullable=False),
        sa.Column("user_id", sa.String(length=64), nullable=False),
        sa.Column("plan_id", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False, default="PENDING"),
        sa.Column("current_period_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("current_period_end", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_event_timestamp", sa.BigInteger(), nullable=False, default=0),
        sa.Column("version", sa.Integer(), nullable=False, default=1),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_subscriptions_user_id", "subscriptions", ["user_id"])
    op.create_index("ix_subscriptions_status", "subscriptions", ["status"])
    op.create_index(
        "ix_subscriptions_last_event_timestamp",
        "subscriptions",
        ["last_event_timestamp"],
    )
    op.create_index(
        "ix_subscriptions_user_status", "subscriptions", ["user_id", "status"]
    )

    # 2. Transactions table
    op.create_table(
        "transactions",
        sa.Column("id", sa.String(length=36), primary_key=True, nullable=False),
        sa.Column(
            "subscription_id",
            sa.String(length=36),
            sa.ForeignKey("subscriptions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("gateway_transaction_id", sa.String(length=128), nullable=True),
        sa.Column("amount_in_cents", sa.Integer(), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False, default="USD"),
        sa.Column("status", sa.String(length=32), nullable=False, default="PENDING"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_transactions_subscription_id", "transactions", ["subscription_id"]
    )
    op.create_index(
        "ix_transactions_gateway_transaction_id",
        "transactions",
        ["gateway_transaction_id"],
    )
    op.create_index("ix_transactions_status", "transactions", ["status"])

    # 3. Idempotency Records table
    op.create_table(
        "idempotency_records",
        sa.Column(
            "idempotency_key", sa.String(length=255), primary_key=True, nullable=False
        ),
        sa.Column("request_hash", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False, default="STARTED"),
        sa.Column("response_code", sa.Integer(), nullable=True),
        sa.Column("response_body", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("locked_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_idempotency_records_status", "idempotency_records", ["status"])
    op.create_index(
        "ix_idempotency_key_hash",
        "idempotency_records",
        ["idempotency_key", "request_hash"],
    )

    # 4. Webhook Events table
    op.create_table(
        "webhook_events",
        sa.Column("id", sa.String(length=36), primary_key=True, nullable=False),
        sa.Column("gateway", sa.String(length=32), nullable=False),
        sa.Column("event_id", sa.String(length=128), nullable=False),
        sa.Column("event_type", sa.String(length=64), nullable=False),
        sa.Column("event_timestamp", sa.BigInteger(), nullable=False),
        sa.Column("payload", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False, default="PROCESSED"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("gateway", "event_id", name="uq_gateway_event_id"),
    )
    op.create_index("ix_webhook_events_gateway", "webhook_events", ["gateway"])
    op.create_index("ix_webhook_events_event_id", "webhook_events", ["event_id"])
    op.create_index("ix_webhook_events_status", "webhook_events", ["status"])


def downgrade() -> None:
    op.drop_table("webhook_events")
    op.drop_table("idempotency_records")
    op.drop_table("transactions")
    op.drop_table("subscriptions")
