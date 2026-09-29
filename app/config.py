from __future__ import annotations

import os
from functools import lru_cache
from typing import Dict
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    PROJECT_NAME: str = "Distributed Subscription & Payment Orchestrator"
    VERSION: str = "1.0.0"
    ENVIRONMENT: str = "development"
    DEBUG: bool = False

    # Database
    DATABASE_URL: str = "postgresql+asyncpg://postgres:postgres@localhost:5432/subscriptions_db"
    DATABASE_ECHO: bool = False
    DB_POOL_SIZE: int = 20
    DB_MAX_OVERFLOW: int = 10
    DB_POOL_TIMEOUT: int = 30

    # Redis
    REDIS_URL: str = "redis://localhost:6379/0"
    REDIS_LOCK_TTL_SECONDS: int = 15  # Max lease time before auto-release
    REDIS_RETRY_INTERVAL_SECONDS: float = 0.1
    REDIS_ACQUISITION_TIMEOUT_SECONDS: float = 2.0

    # Idempotency
    IDEMPOTENCY_RETENTION_SECONDS: int = 86400 * 7  # 7 days

    # Webhook HMAC secrets per gateway
    WEBHOOK_SECRETS: Dict[str, str] = {
        "stripe": "whsec_stripe_test_secret_key_998877",
        "adyen": "whsec_adyen_test_secret_key_112233",
    }
    WEBHOOK_MAX_DRIFT_SECONDS: int = 300  # 5 minutes replay protection window

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )


@lru_cache()
def get_settings() -> Settings:
    return Settings()
