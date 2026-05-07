from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, WebSocket, WebSocketDisconnect, status
from pydantic import ValidationError

from app.models.events import (
    GameEvent,
    ReadyPayload,
    SubmitAnswerPayload,
    UpdateScorePayload,
)
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
                await websocket.close(
                    code=status.WS_1008_POLICY_VIOLATION,
                    reason="Player not in room.",
                )
                return
        except HTTPException:
            await websocket.close(code=status.WS_1008_POLICY_VIOLATION, reason="Invalid room or player.")
            return

        await connection_manager.connect(room.room_code, player_id, websocket)
        room = room_service.mark_connected(room.room_code, player_id, True)
        await connection_manager.broadcast(
            room.room_code,
            GameEvent(
                type="player_connected",
                roomCode=room.room_code,
                playerId=player_id,
                payload={"room": room.model_dump(mode="json", by_alias=True)},
            ),
        )

        try:
            while True:
                try:
                    raw_message = await websocket.receive_json()
                except ValueError:
                    await _send_error(
                        websocket,
                        room.room_code,
                        player_id,
                        "invalid_json",
                        "Malformed JSON message.",
                    )
                    continue

                if not isinstance(raw_message, dict):
                    await _send_error(
                        websocket,
                        room.room_code,
                        player_id,
                        "invalid_event",
                        "Event payload must be a JSON object.",
                    )
                    continue

                if raw_message.get("roomCode") and raw_message.get("roomCode") != room.room_code:
                    await _send_error(
                        websocket,
                        room.room_code,
                        player_id,
                        "invalid_event",
                        "roomCode does not match the connected room.",
                    )
                    continue

                if raw_message.get("playerId") and raw_message.get("playerId") != player_id:
                    await _send_error(
                        websocket,
                        room.room_code,
                        player_id,
                        "invalid_event",
                        "playerId does not match the connected player.",
                    )
                    continue

                event_type = raw_message.get("type")
                payload = raw_message.get("payload", {})
                if not isinstance(payload, dict):
                    await _send_error(
                        websocket,
                        room.room_code,
                        player_id,
                        "invalid_payload",
                        "Event payload must be an object.",
                    )
                    continue

                try:
                    await _handle_event(
                        websocket=websocket,
                        room_code=room.room_code,
                        player_id=player_id,
                        event_type=event_type,
                        payload=payload,
                        room_service=room_service,
                        connection_manager=connection_manager,
                        game_service=game_service,
                    )
                except ValidationError:
                    await _send_error(
                        websocket,
                        room.room_code,
                        player_id,
                        "invalid_payload",
                        "Payload validation failed.",
                    )
                except HTTPException as exc:
                    await _send_error(
                        websocket,
                        room.room_code,
                        player_id,
                        "request_error",
                        str(exc.detail),
                    )
        except WebSocketDisconnect:
            room = room_service.mark_connected(room.room_code, player_id, False)
            connection_manager.disconnect(room.room_code, player_id)
            await connection_manager.broadcast(
                room.room_code,
                GameEvent(
                    type="player_disconnected",
                    roomCode=room.room_code,
                    playerId=player_id,
                    payload={"room": room.model_dump(mode="json", by_alias=True)},
                ),
            )
        except Exception:
            room = room_service.mark_connected(room.room_code, player_id, False)
            connection_manager.disconnect(room.room_code, player_id)
            await connection_manager.broadcast(
                room.room_code,
                GameEvent(
                    type="player_disconnected",
                    roomCode=room.room_code,
                    playerId=player_id,
                    payload={"room": room.model_dump(mode="json", by_alias=True)},
                ),
            )

    return router


async def _handle_event(
    websocket: WebSocket,
    room_code: str,
    player_id: str,
    event_type: Any,
    payload: dict[str, Any],
    room_service: RoomService,
    connection_manager: ConnectionManager,
    game_service: GameService,
) -> None:
    if event_type == "ready":
        ready_payload = ReadyPayload.model_validate(payload)
        room = room_service.set_ready(room_code, player_id, ready_payload.ready)
        await connection_manager.broadcast(
            room.room_code,
            GameEvent(
                type="player_ready_updated",
                roomCode=room.room_code,
                playerId=player_id,
                payload={
                    "ready": ready_payload.ready,
                    "room": room.model_dump(mode="json", by_alias=True),
                },
            ),
        )
        return

    if event_type == "start_game":
        room = room_service.start_game(room_code, player_id)
        await connection_manager.broadcast(
            room.room_code,
            GameEvent(
                type="game_started",
                roomCode=room.room_code,
                playerId=player_id,
                payload={"room": room.model_dump(mode="json", by_alias=True)},
            ),
        )
        return

    if event_type == "submit_answer":
        answer_payload = SubmitAnswerPayload.model_validate(payload)
        await game_service.validate_answer(
            answer=answer_payload.answer,
            card_id=answer_payload.card_id,
        )
        await connection_manager.broadcast(
            room_code,
            GameEvent(
                type="answer_submitted",
                roomCode=room_code,
                playerId=player_id,
                payload=answer_payload.model_dump(mode="json", by_alias=True),
            ),
        )
        return

    if event_type == "update_score":
        room = room_service.get_room(room_code)
        if room.host_player_id != player_id:
            await _send_error(
                websocket,
                room.room_code,
                player_id,
                "forbidden",
                "Only the host can update scores.",
            )
            return

        score_payload = UpdateScorePayload.model_validate(payload)
        room = room_service.update_score(
            room_code=room_code,
            target_player_id=score_payload.target_player_id,
            points=score_payload.points,
        )
        await connection_manager.broadcast(
            room.room_code,
            GameEvent(
                type="score_updated",
                roomCode=room.room_code,
                playerId=player_id,
                payload={
                    "targetPlayerId": score_payload.target_player_id,
                    "points": score_payload.points,
                    "scores": room.scores,
                },
            ),
        )
        return

    if event_type == "next_turn":
        room = room_service.get_room(room_code)
        if room.host_player_id != player_id:
            await _send_error(
                websocket,
                room.room_code,
                player_id,
                "forbidden",
                "Only the host can change turns.",
            )
            return

        room = room_service.next_turn(room_code)
        await connection_manager.broadcast(
            room.room_code,
            GameEvent(
                type="turn_changed",
                roomCode=room.room_code,
                playerId=player_id,
                payload={
                    "currentTurnPlayerId": room.current_turn_player_id,
                    "room": room.model_dump(mode="json", by_alias=True),
                },
            ),
        )
        return

    if event_type == "ping":
        await websocket.send_json(
            GameEvent(
                type="pong",
                roomCode=room_code,
                playerId=player_id,
                payload={},
            ).model_dump(mode="json", by_alias=True),
        )
        return

    await _send_error(
        websocket,
        room_code,
        player_id,
        "unknown_event",
        "Unsupported event type.",
    )


async def _send_error(
    websocket: WebSocket,
    room_code: str,
    player_id: str,
    error_type: str,
    message: str,
) -> None:
    await websocket.send_json(
        GameEvent(
            type=error_type,
            roomCode=room_code,
            playerId=player_id,
            payload={"message": message},
        ).model_dump(mode="json", by_alias=True),
    )
