from __future__ import annotations

from fastapi import APIRouter, Header, HTTPException, Request, status

from app.models.events import GameEvent
from app.models.room import (
    CreateRoomRequest,
    CreateRoomResponse,
    JoinRoomRequest,
    JoinRoomResponse,
    LeaveRoomRequest,
)
from app.services.connection_manager import ConnectionManager
from app.services.operational_controls import OperationalControls
from app.services.room_service import RoomService


def get_rooms_router(
    room_service: RoomService,
    connection_manager: ConnectionManager,
    operational_controls: OperationalControls | None = None,
) -> APIRouter:
    router = APIRouter(prefix="/rooms", tags=["rooms"])

    @router.post("/create", response_model=CreateRoomResponse, status_code=201)
    async def create_room(
        request: Request,
        payload: CreateRoomRequest,
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    ) -> CreateRoomResponse:
        key = _idempotency_key(idempotency_key)
        fingerprint = payload.model_dump_json(by_alias=True)
        client_ip = request.client.host if request.client else "unknown"
        if operational_controls is not None:
            operational_controls.check_http_rate("create", client_ip)
            operational_controls.check_room_capacity(client_ip)
        room, host_player, session_token, replayed = room_service.create_room_idempotent(
            idempotency_key=key,
            fingerprint=fingerprint,
            host_name=payload.host_name,
            max_players=payload.max_players,
            settings=payload.settings,
        )
        if operational_controls is not None and not replayed:
            operational_controls.register_room(client_ip, room.room_code)
            operational_controls.increment("rooms_created")
        return CreateRoomResponse(
            roomCode=room.room_code,
            playerId=host_player.player_id,
            sessionToken=session_token,
            room=room,
        )

    @router.post("/join", response_model=JoinRoomResponse)
    async def join_room(
        request: Request,
        payload: JoinRoomRequest,
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    ) -> JoinRoomResponse:
        key = _idempotency_key(idempotency_key)
        fingerprint = payload.model_dump_json(by_alias=True)
        client_ip = request.client.host if request.client else "unknown"
        if operational_controls is not None:
            operational_controls.check_http_rate("join", client_ip)
            operational_controls.check_room_capacity(
                client_ip, payload.room_code.upper()
            )
        room, player, session_token, replayed = room_service.join_room_idempotent(
            idempotency_key=key,
            fingerprint=fingerprint,
            room_code=payload.room_code,
            player_name=payload.player_name,
        )
        if operational_controls is not None and not replayed:
            operational_controls.register_room(client_ip, room.room_code)
            operational_controls.increment("room_joins")
        if not replayed:
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
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    ) -> dict:
        key = _idempotency_key(idempotency_key)
        try:
            existing_room = room_service.get_room(room_code)
            room_service.authenticate_player(existing_room, payload.player_id, _bearer_token(authorization))
        except HTTPException:
            room, result, replayed = room_service.leave_room_idempotent(
                key, room_code, payload.player_id, replay_only=True
            )
            if not replayed:
                raise
            return {"status": "applied", "replayed": True}
        room, result, replayed = room_service.leave_room_idempotent(key, room_code, payload.player_id)
        connection_manager.disconnect(existing_room.room_code, payload.player_id)
        if room is not None:
            await _broadcast_snapshot(
                connection_manager,
                room_service,
                room,
                event_type="round_resolved" if result is not None else "player_left",
                actor_player_id=payload.player_id,
            )
        return {"status": "applied", "replayed": replayed}

    return router


def _bearer_token(authorization: str | None) -> str:
    scheme, _, token = (authorization or "").partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"code": "authorization_required"},
        )
    return token


def _idempotency_key(value: str | None) -> str:
    key = (value or "").strip()
    if len(key) < 16 or len(key) > 128:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"code": "invalid_idempotency_key"},
        )
    return key


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
