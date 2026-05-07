from __future__ import annotations

import os
from functools import lru_cache


class Settings:
    def __init__(self) -> None:
        allowed_origins = os.getenv("CORS_ALLOW_ORIGINS", "*")
        self.app_name = os.getenv("APP_NAME", "MARKI Game Server")
        self.environment = os.getenv("ENVIRONMENT", "development")
        self.redis_url = os.getenv("REDIS_URL", "").strip() or None
        self.cards_api_base_url = os.getenv("CARDS_API_BASE_URL", "").strip() or None
        self.cards_api_timeout = float(os.getenv("CARDS_API_TIMEOUT", "10"))
        self.cors_allow_origins = [
            origin.strip() for origin in allowed_origins.split(",") if origin.strip()
        ] or ["*"]


@lru_cache
def get_settings() -> Settings:
    return Settings()
