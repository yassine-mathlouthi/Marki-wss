from __future__ import annotations

import os
from functools import lru_cache


class Settings:
    def __init__(self) -> None:
        allowed_origins = os.getenv("CORS_ALLOW_ORIGINS", "*")
        self.app_name = os.getenv("APP_NAME", "MARKI Game Server")
        self.environment = os.getenv("ENVIRONMENT", "development")
        self.redis_url = os.getenv("REDIS_URL", "").strip() or None
        self.cards_api_base_url = (
            os.getenv("CARDS_API_BASE_URL", os.getenv("API_BASE_URL", "")).strip()
            or None
        )
        self.cards_api_timeout = float(os.getenv("CARDS_API_TIMEOUT", "10"))
        self.cards_cache_ttl_seconds = max(0.0, float(os.getenv("CARDS_CACHE_TTL_SECONDS", "300")))
        self.disconnect_grace_seconds = max(
            0.0,
            float(os.getenv("DISCONNECT_GRACE_SECONDS", "180")),
        )
        self.room_create_rate_limit = max(1, int(os.getenv("ROOM_CREATE_RATE_LIMIT", "5")))
        self.room_join_rate_limit = max(1, int(os.getenv("ROOM_JOIN_RATE_LIMIT", "20")))
        self.room_rate_window_seconds = max(1.0, float(os.getenv("ROOM_RATE_WINDOW_SECONDS", "60")))
        self.max_active_rooms_per_ip = max(1, int(os.getenv("MAX_ACTIVE_ROOMS_PER_IP", "5")))
        self.max_sockets_per_ip = max(1, int(os.getenv("MAX_SOCKETS_PER_IP", "10")))
        self.ws_max_frame_bytes = max(1024, int(os.getenv("WS_MAX_FRAME_BYTES", "65536")))
        self.ws_send_timeout_seconds = max(0.1, float(os.getenv("WS_SEND_TIMEOUT_SECONDS", "2")))
        self.ws_event_rate_limit = max(1, int(os.getenv("WS_EVENT_RATE_LIMIT", "30")))
        self.ws_event_rate_window_seconds = max(1.0, float(os.getenv("WS_EVENT_RATE_WINDOW_SECONDS", "10")))
        self.abandoned_room_ttl_seconds = max(1.0, float(os.getenv("ABANDONED_ROOM_TTL_SECONDS", "1800")))
        self.waiting_room_ttl_seconds = max(1.0, float(os.getenv("WAITING_ROOM_TTL_SECONDS", "900")))
        self.playing_room_ttl_seconds = max(1.0, float(os.getenv("PLAYING_ROOM_TTL_SECONDS", "3600")))
        self.finished_room_ttl_seconds = max(1.0, float(os.getenv("FINISHED_ROOM_TTL_SECONDS", "900")))
        self.room_cleanup_interval_seconds = max(1.0, float(os.getenv("ROOM_CLEANUP_INTERVAL_SECONDS", "30")))
        self.idempotency_ttl_seconds = max(
            600.0,
            float(os.getenv("IDEMPOTENCY_TTL_SECONDS", "86400")),
        )
        self.cors_allow_origins = [
            origin.strip() for origin in allowed_origins.split(",") if origin.strip()
        ] or ["*"]


@lru_cache
def get_settings() -> Settings:
    return Settings()
