from __future__ import annotations

from fastapi import APIRouter

from app.models.events import GameEvent
from app.models.room import (
    CreateRoomRequest,
    CreateRoomResponse,
    JoinRoomRequest,
    JoinRoomResponse,
    LeaveRoomRequest,
    Room,
)
from app.services.connection_manager import ConnectionManager
from app.services.room_service import RoomService


def get_rooms_router(
    room_service: RoomService,
    connection_manager: ConnectionManager,
) -> APIRouter:
    router = APIRouter(prefix="/rooms", tags=["rooms"])

    @router.post("/create", response_model=CreateRoomResponse, status_code=201)
    async def create_room(payload: CreateRoomRequest) -> CreateRoomResponse:
        room, host_player = room_service.create_room(
            host_name=payload.host_name,
            max_players=payload.max_players,
        )
        return CreateRoomResponse(
            roomCode=room.room_code,
            playerId=host_player.player_id,
            room=room,
        )

    @router.post("/join", response_model=JoinRoomResponse)
    async def join_room(payload: JoinRoomRequest) -> JoinRoomResponse:
        room, player = room_service.join_room(
            room_code=payload.room_code,
            player_name=payload.player_name,
        )
        event = GameEvent(
            type="player_joined",
            roomCode=room.room_code,
            playerId=player.player_id,
            payload={"player": player.model_dump(mode="json", by_alias=True), "room": room.model_dump(mode="json", by_alias=True)},
        )
        await connection_manager.broadcast(room.room_code, event)
        return JoinRoomResponse(
            roomCode=room.room_code,
            playerId=player.player_id,
            room=room,
        )

    @router.get("/{room_code}", response_model=Room)
    async def get_room(room_code: str) -> Room:
        return room_service.get_room(room_code)

    @router.post("/{room_code}/leave", response_model=Room | None)
    async def leave_room(room_code: str, payload: LeaveRoomRequest) -> Room | None:
        existing_room = room_service.get_room(room_code)
        room = room_service.leave_room(room_code, payload.player_id)
        event = GameEvent(
            type="player_left",
            roomCode=existing_room.room_code,
            playerId=payload.player_id,
            payload={"room": room.model_dump(mode="json", by_alias=True) if room else None},
        )
        await connection_manager.broadcast(existing_room.room_code, event)
        connection_manager.disconnect(existing_room.room_code, payload.player_id)
        return room

    return router
