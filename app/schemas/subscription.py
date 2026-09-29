from __future__ import annotations

from datetime import datetime
from typing import Optional
from pydantic import BaseModel, ConfigDict, Field

from app.domain.enums import SubscriptionStatus, TransactionStatus


class CreateSubscriptionRequest(BaseModel):
    user_id: str = Field(..., min_length=1, max_length=64, examples=["usr_987654321"])
    plan_id: str = Field(
        ..., min_length=1, max_length=64, examples=["plan_premium_tier"]
    )
    current_period_end: datetime = Field(
        ..., description="End date for the initial billing period"
    )


class ChargeSubscriptionRequest(BaseModel):
    subscription_id: str = Field(
        ...,
        min_length=1,
        max_length=36,
        examples=["018e47d1-0000-7000-8000-000000000001"],
    )
    amount_in_cents: int = Field(
        ..., gt=0, examples=[1999], description="Amount in cents (must be > 0)"
    )
    currency: str = Field(default="USD", min_length=3, max_length=3, examples=["USD"])
    payment_method_id: Optional[str] = Field(None, examples=["pm_card_visa_4242"])


class SubscriptionResponse(BaseModel):
    id: str
    user_id: str
    plan_id: str
    status: SubscriptionStatus
    current_period_start: datetime
    current_period_end: datetime
    last_event_timestamp: int
    version: int
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class ChargeSubscriptionResponse(BaseModel):
    transaction_id: str
    subscription_id: str
    amount_in_cents: int
    currency: str
    status: TransactionStatus
    subscription_status: SubscriptionStatus
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)
