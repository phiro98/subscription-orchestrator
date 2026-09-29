from __future__ import annotations

from fastapi import APIRouter
from app.api.v1.subscriptions import router as subscriptions_router
from app.api.v1.webhooks import router as webhooks_router

api_v1_router = APIRouter(prefix="/v1")
api_v1_router.include_router(subscriptions_router)
api_v1_router.include_router(webhooks_router)
