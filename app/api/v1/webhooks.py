from __future__ import annotations

import json
import logging
from typing import Annotated, Optional
from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_db_session
from app.domain.exceptions import (
    InvalidWebhookSignatureError,
    OrchestratorDomainException,
)
from app.schemas.webhook import GatewayWebhookPayload, WebhookProcessingResult
from app.services.webhook_processor import WebhookProcessor

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/webhooks", tags=["Webhooks"])


@router.post(
    "/{gateway}",
    response_model=WebhookProcessingResult,
    status_code=status.HTTP_200_OK,
    summary="Ingest external payment gateway webhook",
    responses={
        200: {
            "description": "Webhook received and processed (or duplicate/out-of-order safely handled)"
        },
        400: {"description": "Replay attack detected or timestamp drift"},
        401: {"description": "HMAC signature verification failed"},
        404: {"description": "Subscription target not found"},
        422: {"description": "Malformed JSON payload"},
    },
)
async def handle_gateway_webhook(
    gateway: str,
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db_session)],
    stripe_signature: Annotated[Optional[str], Header(alias="Stripe-Signature")] = None,
    x_signature: Annotated[Optional[str], Header(alias="X-Signature")] = None,
) -> WebhookProcessingResult:
    sig_header = stripe_signature or x_signature
    if not sig_header:
        raise InvalidWebhookSignatureError(
            gateway,
            "Missing signature header (Stripe-Signature or X-Signature required).",
        )

    # 1. Read raw body bytes for cryptographic HMAC verification
    raw_body = await request.body()
    if not raw_body:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Empty webhook request body received.",
        )

    # 2. Cryptographic signature and replay window verification
    WebhookProcessor.verify_signature(
        raw_payload=raw_body,
        signature_header=sig_header,
        gateway=gateway,
    )

    # 3. Parse JSON payload
    try:
        data_json = json.loads(raw_body.decode("utf-8"))
        payload = GatewayWebhookPayload.model_validate(data_json)
    except Exception as e:
        logger.warning(f"Malformed webhook payload for gateway '{gateway}': {e}")
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Malformed webhook JSON payload: {str(e)}",
        )

    # 4. Transactional processing with row lock and out-of-order event sequencing
    processor = WebhookProcessor(db_session=db)
    result = await processor.process_webhook(
        gateway=gateway,
        event_payload=payload,
        raw_body_str=raw_body.decode("utf-8"),
    )

    return result
