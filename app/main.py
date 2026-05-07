from __future__ import annotations

from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.core.config import get_settings
from app.routers.rooms import get_rooms_router
from app.routers.websocket import get_websocket_router
from app.services.connection_manager import ConnectionManager
from app.services.game_service import GameService
from app.services.in_memory import InMemoryRoomEventBus, InMemoryRoomStore
from app.services.room_service import RoomService

load_dotenv()

settings = get_settings()
connection_manager = ConnectionManager()
room_store = InMemoryRoomStore()
room_event_bus = InMemoryRoomEventBus(connection_manager)
room_service = RoomService(room_store)
game_service = GameService(settings)

app = FastAPI(title=settings.app_name)

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


app.include_router(get_rooms_router(room_service, connection_manager))
app.include_router(get_websocket_router(room_service, connection_manager, game_service))
