from __future__ import annotations

from fastapi import APIRouter, Header, HTTPException, status

from app.models.events import GameEvent
from app.models.room import (
    CreateRoomRequest,
    CreateRoomResponse,
    JoinRoomRequest,
    JoinRoomResponse,
    LeaveRoomRequest,
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
        room, host_player, session_token = room_service.create_room(
            host_name=payload.host_name,
            max_players=payload.max_players,
            settings=payload.settings,
        )
        return CreateRoomResponse(
            roomCode=room.room_code,
            playerId=host_player.player_id,
            sessionToken=session_token,
            room=room,
        )

    @router.post("/join", response_model=JoinRoomResponse)
    async def join_room(payload: JoinRoomRequest) -> JoinRoomResponse:
        room, player, session_token = room_service.join_room(
            room_code=payload.room_code,
            player_name=payload.player_name,
        )
        await _broadcast_snapshot(connection_manager, room_service, room, event_type="player_joined", actor_player_id=player.player_id)
        return JoinRoomResponse(
            roomCode=room.room_code,
            playerId=player.player_id,
            sessionToken=session_token,
            room=room,
        )

    @router.get("/{room_code}")
    async def get_room(room_code: str, authorization: str | None = Header(default=None)) -> dict:
        room = room_service.get_room(room_code)
        player = room_service.authenticate_session(room, _bearer_token(authorization))
        return room_service.build_snapshot(room, viewer_player_id=player.player_id).model_dump(mode="json", by_alias=True)

    @router.post("/{room_code}/leave")
    async def leave_room(
        room_code: str,
        payload: LeaveRoomRequest,
        authorization: str | None = Header(default=None),
    ) -> dict | None:
        existing_room = room_service.get_room(room_code)
        room_service.authenticate_player(
            existing_room,
            payload.player_id,
            _bearer_token(authorization),
        )
        room = room_service.leave_room(room_code, payload.player_id)
        connection_manager.disconnect(existing_room.room_code, payload.player_id)
        if room is not None:
            await _broadcast_snapshot(
                connection_manager,
                room_service,
                room,
                event_type="player_left",
                actor_player_id=payload.player_id,
            )
            return room_service.build_snapshot(room, viewer_player_id=None).model_dump(mode="json", by_alias=True)
        return None

    return router


def _bearer_token(authorization: str | None) -> str:
    scheme, _, token = (authorization or "").partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Player session token is required.",
        )
    return token


async def _broadcast_snapshot(
    connection_manager: ConnectionManager,
    room_service: RoomService,
    room,
    *,
    event_type: str,
    actor_player_id: str,
) -> None:
    for player in room.players:
        await connection_manager.send_to_player(
            room.room_code,
            player.player_id,
            GameEvent(
                type=event_type,
                roomCode=room.room_code,
                playerId=actor_player_id,
                payload={"snapshot": room_service.build_snapshot(room, player.player_id).model_dump(mode="json", by_alias=True)},
            ),
        )
