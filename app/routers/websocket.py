from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, WebSocket, WebSocketDisconnect, status
from pydantic import ValidationError

from app.models.events import (
    CastVotePayload,
    GameEvent,
    SubmitAnswerPayload,
    UpdateLobbySettingsPayload,
)
from app.models.room import RoomStatus
from app.services.connection_manager import ConnectionManager
from app.services.game_service import GameService
from app.services.room_service import RoomService


def get_websocket_router(
    room_service: RoomService,
    connection_manager: ConnectionManager,
    game_service: GameService,
) -> APIRouter:
    router = APIRouter(tags=["websocket"])

    @router.websocket("/ws/{room_code}/{player_id}")
    async def websocket_endpoint(
        websocket: WebSocket,
        room_code: str,
        player_id: str,
    ) -> None:
        try:
            room = room_service.get_room(room_code)
            if not room_service.player_in_room(room.room_code, player_id):
                await websocket.close(code=status.WS_1008_POLICY_VIOLATION, reason="Player not in room.")
                return
        except HTTPException:
            await websocket.close(code=status.WS_1008_POLICY_VIOLATION, reason="Invalid room or player.")
            return

        await connection_manager.connect(room.room_code, player_id, websocket)
        room = room_service.mark_connected(room.room_code, player_id, True)
        await _send_snapshot(connection_manager, room_service, room, player_id, "room_snapshot", player_id)
        await _broadcast_presence(connection_manager, room_service, room, "player_connected", player_id)

        try:
            while True:
                raw_message = await websocket.receive_json()
                if not isinstance(raw_message, dict):
                    await _send_error(websocket, room.room_code, player_id, "Event payload must be a JSON object.")
                    continue

                if raw_message.get("roomCode") and raw_message.get("roomCode") != room.room_code:
                    await _send_error(websocket, room.room_code, player_id, "roomCode does not match the connected room.")
                    continue

                if raw_message.get("playerId") and raw_message.get("playerId") != player_id:
                    await _send_error(websocket, room.room_code, player_id, "playerId does not match the connected player.")
                    continue

                event_type = raw_message.get("type")
                payload = raw_message.get("payload", {})
                if not isinstance(payload, dict):
                    await _send_error(websocket, room.room_code, player_id, "Event payload must be an object.")
                    continue

                try:
                    room = room_service.get_room(room.room_code)
                    await _handle_event(
                        websocket=websocket,
                        room=room,
                        player_id=player_id,
                        event_type=event_type,
                        payload=payload,
                        room_service=room_service,
                        connection_manager=connection_manager,
                        game_service=game_service,
                    )
                except ValidationError:
                    await _send_error(websocket, room.room_code, player_id, "Payload validation failed.")
                except HTTPException as exc:
                    await _send_error(websocket, room.room_code, player_id, str(exc.detail))
                except ValueError as exc:
                    await _send_error(websocket, room.room_code, player_id, str(exc))
        except WebSocketDisconnect:
            room = room_service.mark_connected(room.room_code, player_id, False)
            connection_manager.disconnect(room.room_code, player_id)
            await _broadcast_presence(connection_manager, room_service, room, "player_disconnected", player_id)

    return router


async def _handle_event(
    *,
    websocket: WebSocket,
    room,
    player_id: str,
    event_type: Any,
    payload: dict[str, Any],
    room_service: RoomService,
    connection_manager: ConnectionManager,
    game_service: GameService,
) -> None:
    if event_type == "set_ready":
        ready = bool(payload.get("ready", False))
        room = room_service.set_ready(room.room_code, player_id, ready)
        await _broadcast_snapshot(connection_manager, room_service, room, "room_snapshot", player_id)
        return

    if event_type == "update_lobby_settings":
        settings_payload = UpdateLobbySettingsPayload.model_validate(payload)
        room = room_service.update_lobby_settings(
            room.room_code,
            player_id,
            region_id=settings_payload.region_id,
            cards_per_player=settings_payload.cards_per_player,
            language=settings_payload.language,
            wrong_answer_behavior=settings_payload.wrong_answer_behavior,
            max_players=settings_payload.max_players,
        )
        await _broadcast_snapshot(connection_manager, room_service, room, "room_snapshot", player_id)
        return

    if event_type == "start_game":
        room_service.require_host(room, player_id)
        if len(room.players) < 2:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="At least two players are required.")
        if any(not player.is_ready for player in room.players):
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="All players must be ready.")
        room = await game_service.start_game(room)
        room = room_service.save(room)
        await _broadcast_snapshot(connection_manager, room_service, room, "game_snapshot", player_id)
        return

    if event_type == "submit_answer":
        if room.status != RoomStatus.PLAYING:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Game is not active.")
        if room.current_turn_player_id != player_id:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="It is not your turn.")
        answer_payload = SubmitAnswerPayload.model_validate(payload)
        room = game_service.submit_answer(
            room=room,
            player_id=player_id,
            card_id=answer_payload.card_id or "",
            related_card_id=answer_payload.related_card_id,
            answer=answer_payload.answer,
        )
        room = room_service.save(room)
        await _broadcast_snapshot(connection_manager, room_service, room, "game_snapshot", player_id)
        return

    if event_type == "cast_vote":
        if room.status != RoomStatus.PLAYING:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Game is not active.")
        if room.game.pending_round is None:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="No pending round.")
        if room.game.pending_round.player_id == player_id:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Round owner cannot vote.")
        vote_payload = CastVotePayload.model_validate(payload)
        room, result = game_service.cast_vote(room, player_id, vote_payload.choice)
        room = room_service.save(room)
        await _broadcast_snapshot(connection_manager, room_service, room, "game_snapshot", player_id)
        if result is not None:
            await _broadcast_round_resolved(connection_manager, room_service, room, player_id)
        return

    if event_type == "leave_room":
        updated_room = room_service.leave_room(room.room_code, player_id)
        connection_manager.disconnect(room.room_code, player_id)
        await websocket.close(code=status.WS_1000_NORMAL_CLOSURE, reason="Left room.")
        if updated_room is not None:
            await _broadcast_snapshot(connection_manager, room_service, updated_room, "player_left", player_id)
        return

    if event_type == "ping":
        await websocket.send_json(
            GameEvent(
                type="pong",
                roomCode=room.room_code,
                playerId=player_id,
                payload={},
            ).model_dump(mode="json", by_alias=True),
        )
        return

    await _send_error(websocket, room.room_code, player_id, "Unsupported event type.")


async def _send_snapshot(
    connection_manager: ConnectionManager,
    room_service: RoomService,
    room,
    target_player_id: str,
    event_type: str,
    actor_player_id: str,
) -> None:
    await connection_manager.send_to_player(
        room.room_code,
        target_player_id,
        GameEvent(
            type=event_type,
            roomCode=room.room_code,
            playerId=actor_player_id,
            payload={"snapshot": room_service.build_snapshot(room, target_player_id).model_dump(mode="json", by_alias=True)},
        ),
    )


async def _broadcast_snapshot(
    connection_manager: ConnectionManager,
    room_service: RoomService,
    room,
    event_type: str,
    actor_player_id: str,
) -> None:
    for player in room.players:
        await _send_snapshot(connection_manager, room_service, room, player.player_id, event_type, actor_player_id)


async def _broadcast_presence(
    connection_manager: ConnectionManager,
    room_service: RoomService,
    room,
    event_type: str,
    actor_player_id: str,
) -> None:
    await _broadcast_snapshot(connection_manager, room_service, room, event_type, actor_player_id)


async def _broadcast_round_resolved(
    connection_manager: ConnectionManager,
    room_service: RoomService,
    room,
    actor_player_id: str,
) -> None:
    for player in room.players:
        await connection_manager.send_to_player(
            room.room_code,
            player.player_id,
            GameEvent(
                type="round_resolved",
                roomCode=room.room_code,
                playerId=actor_player_id,
                payload={
                    "snapshot": room_service.build_snapshot(room, player.player_id).model_dump(mode="json", by_alias=True),
                    "round": room.game.last_round.model_dump(mode="json", by_alias=True) if room.game and room.game.last_round else None,
                },
            ),
        )


async def _send_error(
    websocket: WebSocket,
    room_code: str,
    player_id: str,
    message: str,
) -> None:
    await websocket.send_json(
        GameEvent(
            type="error",
            roomCode=room_code,
            playerId=player_id,
            payload={"message": message},
        ).model_dump(mode="json", by_alias=True),
    )
