from __future__ import annotations

from typing import Any, Dict, Optional
from pydantic import BaseModel, Field

from app.domain.enums import WebhookProcessingStatus


class WebhookPayloadData(BaseModel):
    subscription_id: str = Field(..., description="Target subscription ID")
    gateway_transaction_id: Optional[str] = Field(None, description="External transaction reference ID")
    amount_in_cents: Optional[int] = Field(None, gt=0, description="Transaction amount")
    currency: Optional[str] = Field("USD", min_length=3, max_length=3)
    failure_reason: Optional[str] = Field(None, description="Reason code if payment failed")


class GatewayWebhookPayload(BaseModel):
    id: str = Field(..., description="Unique event identifier from gateway", examples=["evt_3MjjL22eZvKYlo2C1"])
    type: str = Field(
        ...,
        description="Event type name",
        examples=[
            "invoice.payment_succeeded",
            "invoice.payment_failed",
            "customer.subscription.deleted",
        ],
    )
    created: int = Field(..., description="Event generation epoch timestamp (seconds)", examples=[1700000000])
    data: WebhookPayloadData


class WebhookProcessingResult(BaseModel):
    status: WebhookProcessingStatus
    event_id: str
    message: str
    subscription_id: Optional[str] = None
