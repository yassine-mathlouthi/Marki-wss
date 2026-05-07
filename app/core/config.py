from __future__ import annotations

import os
from functools import lru_cache


class Settings:
    def __init__(self) -> None:
        allowed_origins = os.getenv("CORS_ALLOW_ORIGINS", "*")
        self.app_name = os.getenv("APP_NAME", "MARKI Game Server")
        self.environment = os.getenv("ENVIRONMENT", "development")
        self.cors_allow_origins = [
            origin.strip() for origin in allowed_origins.split(",") if origin.strip()
        ] or ["*"]


@lru_cache
def get_settings() -> Settings:
    return Settings()
