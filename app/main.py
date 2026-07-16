from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager, suppress
from datetime import datetime, timezone

from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import PlainTextResponse

from app.core.config import get_settings
from app.routers.rooms import get_rooms_router
from app.routers.websocket import get_websocket_router
from app.services.connection_manager import ConnectionManager
from app.services.game_service import GameService
from app.services.in_memory import InMemoryRoomEventBus, InMemoryRoomStore
from app.services.room_service import RoomService
from app.services.operational_controls import OperationalControls

load_dotenv()

settings = get_settings()
operational_controls = OperationalControls(settings)
connection_manager = ConnectionManager(
    settings.ws_send_timeout_seconds,
    operational_controls=operational_controls,
)
room_store = InMemoryRoomStore()
room_event_bus = InMemoryRoomEventBus(connection_manager)
game_service = GameService(settings, operational_controls=operational_controls)
room_service = RoomService(room_store, game_service)

logger = logging.getLogger(__name__)


async def _cleanup_rooms() -> None:
    while True:
        await asyncio.sleep(settings.room_cleanup_interval_seconds)
        try:
            expired = room_service.expire_abandoned_rooms(
                now=datetime.now(timezone.utc),
                ttl_seconds=settings.abandoned_room_ttl_seconds,
            )
            for room_code in expired:
                operational_controls.remove_room(room_code)
                operational_controls.increment("rooms_expired")
            operational_controls.prune_expired_buckets()
            cleanup_deleted_rooms = getattr(websocket_router, "cleanup_deleted_rooms", None)
            if cleanup_deleted_rooms is not None:
                cleanup_deleted_rooms(
                    {room.room_code for room in room_store.list_rooms()}
                )
        except Exception:
            logger.exception("Room cleanup failed")


@asynccontextmanager
async def lifespan(_app: FastAPI):
    cleanup_task = asyncio.create_task(_cleanup_rooms())
    try:
        yield
    finally:
        cleanup_task.cancel()
        with suppress(asyncio.CancelledError):
            await cleanup_task

app = FastAPI(title=settings.app_name, lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_allow_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/")
async def health_check() -> dict[str, str]:
    return {"status": "ok", "service": settings.app_name}


@app.get("/metrics", response_class=PlainTextResponse)
async def metrics() -> str:
    return operational_controls.render_metrics(room_service.active_room_count())


app.include_router(
    get_rooms_router(room_service, connection_manager, operational_controls)
)
websocket_router = get_websocket_router(
    room_service,
    connection_manager,
    game_service,
    operational_controls,
)
app.include_router(websocket_router)
