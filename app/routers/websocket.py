from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from typing import Any
from uuid import uuid4

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
from app.services.operational_controls import OperationalControls

logger = logging.getLogger(__name__)

MUTATING_EVENT_TYPES = {
    "set_ready",
    "update_lobby_settings",
    "start_game",
    "replay_game",
    "submit_answer",
    "pass_turn",
    "continue_pass_result",
    "cast_vote",
    "continue_round_result",
    "leave_room",
}


async def run_serialized_room_command(
    room_code: str,
    command_id: str,
    room_service: RoomService,
    lock: asyncio.Lock,
    handler: Callable[[Any], Awaitable[None]],
) -> tuple[bool, Any]:
    async with lock:
        room = room_service.get_room(room_code)
        if command_id in room.processed_command_ids:
            return False, room
        room.processed_command_ids.append(command_id)
        room.processed_command_ids = room.processed_command_ids[-256:]
        try:
            await handler(room)
        except Exception:
            room.processed_command_ids = [
                item for item in room.processed_command_ids if item != command_id
            ]
            raise
        return True, room

def get_websocket_router(
    room_service: RoomService,
    connection_manager: ConnectionManager,
    game_service: GameService,
    operational_controls: OperationalControls | None = None,
) -> APIRouter:
    router = APIRouter(tags=["websocket"])
    room_locks: dict[str, asyncio.Lock] = {}
    grace_tasks: dict[tuple[str, str], asyncio.Task[None]] = {}

    def room_lock(room_code: str) -> asyncio.Lock:
        return room_locks.setdefault(room_code, asyncio.Lock())

    def cleanup_deleted_rooms(active_room_codes: set[str]) -> None:
        for room_code in list(room_locks):
            if room_code not in active_room_codes:
                room_locks.pop(room_code, None)

    def cancel_grace(room_code: str, player_id: str) -> None:
        task = grace_tasks.pop((room_code, player_id), None)
        if task is not None:
            task.cancel()

    async def expire_disconnected_player(
        room_code: str,
        player_id: str,
        connection_epoch: int,
    ) -> None:
        try:
            await asyncio.sleep(game_service.disconnect_grace_seconds)
            async with room_lock(room_code):
                room, result, expired, host_transferred = room_service.expire_disconnected_player(
                    room_code,
                    player_id,
                    connection_epoch,
                )
                if not expired:
                    return
            if host_transferred:
                await _broadcast_snapshot(
                    connection_manager,
                    room_service,
                    room,
                    "host_transferred",
                    room.host_player_id,
                    mark_disconnected,
                )
            elif result is not None:
                await _broadcast_round_resolved(
                    connection_manager,
                    room_service,
                    room,
                    player_id,
                    mark_disconnected,
                )
            else:
                await _broadcast_snapshot(
                    connection_manager,
                    room_service,
                    room,
                    "player_disconnect_expired",
                    player_id,
                    mark_disconnected,
                )
        except (asyncio.CancelledError, HTTPException):
            return
        finally:
            current = grace_tasks.get((room_code, player_id))
            if current is asyncio.current_task():
                grace_tasks.pop((room_code, player_id), None)

    async def mark_disconnected_locked(room_code: str, player_id: str) -> Any:
        room = room_service.mark_connected(room_code, player_id, False)
        player = room_service.get_player(room, player_id)
        cancel_grace(room_code, player_id)
        grace_tasks[(room_code, player_id)] = asyncio.create_task(
            expire_disconnected_player(
                room_code,
                player_id,
                player.connection_epoch,
            )
        )
        return room

    async def mark_disconnected(room_code: str, player_id: str) -> Any:
        async with room_lock(room_code):
            return await mark_disconnected_locked(room_code, player_id)

    @router.websocket("/ws/{room_code}/{player_id}")
    async def websocket_endpoint(
        websocket: WebSocket,
        room_code: str,
        player_id: str,
    ) -> None:
        try:
            room = room_service.get_room(room_code)
        except HTTPException:
            await websocket.close(code=status.WS_1008_POLICY_VIOLATION, reason="Invalid room or player session.")
            return

        await websocket.accept()
        try:
            raw_auth = await asyncio.wait_for(websocket.receive_text(), timeout=8)
            if operational_controls is not None and len(raw_auth.encode("utf-8")) > operational_controls.settings.ws_max_frame_bytes:
                await websocket.close(code=1009, reason="Frame too large.")
                return
            auth_message = json.loads(raw_auth)
            if not isinstance(auth_message, dict) or auth_message.get("type") != "authenticate":
                raise ValueError
            room_service.authenticate_player(
                room,
                player_id,
                str(auth_message.get("sessionToken", "")),
            )
        except (asyncio.TimeoutError, HTTPException, ValueError, WebSocketDisconnect):
            await websocket.close(code=status.WS_1008_POLICY_VIOLATION, reason="Invalid room or player session.")
            return

        registered = False
        admitted_socket = False
        client_ip = websocket.client.host if websocket.client else "unknown"
        try:
            if operational_controls is not None:
                operational_controls.admit_socket(
                    client_ip, room.room_code, player_id, id(websocket)
                )
                admitted_socket = True
            await connection_manager.connect(
                room.room_code,
                player_id,
                websocket,
                accept=False,
            )
            registered = True
            async with room_lock(room.room_code):
                cancel_grace(room.room_code, player_id)
                previous_host_id = room.host_player_id
                room = room_service.mark_connected(room.room_code, player_id, True)
            await _send_snapshot(connection_manager, room_service, room, player_id, "room_snapshot", player_id)
            await _broadcast_presence(
                connection_manager,
                room_service,
                room,
                "host_transferred" if room.host_player_id != previous_host_id else "player_connected",
                player_id,
                mark_disconnected,
            )

            while True:
                raw_text = await websocket.receive_text()
                if operational_controls is not None:
                    if len(raw_text.encode("utf-8")) > operational_controls.settings.ws_max_frame_bytes:
                        operational_controls.increment("socket_errors", "frame_too_large")
                        await websocket.close(code=1009, reason="Frame too large.")
                        return
                    try:
                        operational_controls.check_event_rate(room.room_code, player_id)
                    except HTTPException:
                        await _send_error(
                            websocket,
                            room.room_code,
                            player_id,
                            "Event rate limit exceeded.",
                            code="event_rate_limited",
                            correlation_id=str(uuid4()),
                        )
                        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
                        return
                try:
                    raw_message = json.loads(raw_text)
                except json.JSONDecodeError:
                    correlation_id = str(uuid4())
                    logger.warning(
                        "Malformed WebSocket JSON room=%s player=%s correlation=%s",
                        room.room_code,
                        player_id,
                        correlation_id,
                    )
                    if operational_controls is not None:
                        operational_controls.increment("socket_errors", "invalid_json")
                    await _send_error(
                        websocket,
                        room.room_code,
                        player_id,
                        "Message must be valid JSON.",
                        code="invalid_json",
                        correlation_id=correlation_id,
                    )
                    continue

                correlation_id = (
                    str(raw_message.get("commandId", "")).strip()
                    if isinstance(raw_message, dict)
                    else ""
                ) or str(uuid4())
                if not isinstance(raw_message, dict):
                    await _send_error(
                        websocket,
                        room.room_code,
                        player_id,
                        "Event payload must be a JSON object.",
                        code="invalid_event",
                        correlation_id=correlation_id,
                    )
                    continue

                if raw_message.get("roomCode") and raw_message.get("roomCode") != room.room_code:
                    await _send_error(websocket, room.room_code, player_id, "roomCode does not match the connected room.", code="room_mismatch", correlation_id=correlation_id)
                    continue

                if raw_message.get("playerId") and raw_message.get("playerId") != player_id:
                    await _send_error(websocket, room.room_code, player_id, "playerId does not match the connected player.", code="player_mismatch", correlation_id=correlation_id)
                    continue

                event_type = raw_message.get("type")
                payload = raw_message.get("payload", {})
                if not isinstance(payload, dict):
                    await _send_error(websocket, room.room_code, player_id, "Event payload must be an object.", code="invalid_payload", correlation_id=correlation_id)
                    continue

                try:
                    command_id = str(raw_message.get("commandId", "")).strip()
                    if event_type in MUTATING_EVENT_TYPES and not command_id:
                        if operational_controls is not None:
                            operational_controls.increment("command_rejections", "missing_command_id")
                        await _send_error(
                            websocket,
                            room.room_code,
                            player_id,
                            "Mutating commands require commandId.",
                            code="missing_command_id",
                            correlation_id=correlation_id,
                        )
                        continue

                    if event_type in MUTATING_EVENT_TYPES:
                        async def execute(locked_room) -> None:
                            await _handle_event(
                                websocket=websocket,
                                room=locked_room,
                                player_id=player_id,
                                event_type=event_type,
                                payload=payload,
                                room_service=room_service,
                                connection_manager=connection_manager,
                                game_service=game_service,
                                cancel_grace=cancel_grace,
                                mark_disconnected=mark_disconnected_locked,
                                correlation_id=correlation_id,
                            )

                        executed, room = await run_serialized_room_command(
                            room.room_code,
                            command_id,
                            room_service,
                            room_lock(room.room_code),
                            execute,
                        )
                        await _send_command_result(
                            connection_manager,
                            room_service,
                            room,
                            player_id,
                            command_id,
                            str(event_type),
                            replayed=not executed,
                        )
                        if event_type == "leave_room":
                            connection_manager.disconnect(room.room_code, player_id, websocket)
                            await websocket.close(code=status.WS_1000_NORMAL_CLOSURE, reason="Left room.")
                            return
                        if not executed:
                            continue
                    else:
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
                            cancel_grace=cancel_grace,
                            mark_disconnected=mark_disconnected,
                            correlation_id=correlation_id,
                        )
                except ValidationError as exc:
                    if operational_controls is not None:
                        operational_controls.increment("command_rejections", "invalid_payload")
                    details = []
                    for error in exc.errors():
                        location = ".".join(str(part) for part in error.get("loc", ()))
                        message = error.get("msg", "Invalid value.")
                        details.append(f"{location}: {message}" if location else message)
                    await _send_rejected_command_result(
                        websocket, room.room_code, player_id,
                        locals().get("command_id", correlation_id),
                        str(locals().get("event_type", "")), "invalid_payload",
                    )
                except HTTPException as exc:
                    code = exc.detail.get("code") if isinstance(exc.detail, dict) else "command_rejected"
                    logger.info("WebSocket command rejected room=%s player=%s correlation=%s code=%s", room.room_code, player_id, correlation_id, code)
                    if operational_controls is not None:
                        operational_controls.increment("command_rejections", str(code))
                    await _send_rejected_command_result(
                        websocket, room.room_code, player_id,
                        locals().get("command_id", correlation_id),
                        str(locals().get("event_type", "")), str(code),
                    )
                except ValueError as exc:
                    logger.info("WebSocket command invalid room=%s player=%s correlation=%s error=%s", room.room_code, player_id, correlation_id, exc)
                    if operational_controls is not None:
                        operational_controls.increment("command_rejections", "command_rejected")
                    await _send_rejected_command_result(
                        websocket, room.room_code, player_id,
                        locals().get("command_id", correlation_id),
                        str(locals().get("event_type", "")), "command_rejected",
                    )
        except WebSocketDisconnect:
            pass
        except Exception:
            if operational_controls is not None:
                operational_controls.increment("socket_errors", "unexpected")
            logger.exception(
                "Unexpected WebSocket failure room=%s player=%s correlation=%s",
                room.room_code,
                player_id,
                locals().get("correlation_id", "connection"),
            )
        finally:
            try:
                await websocket.close()
            except Exception:
                pass
            if operational_controls is not None and admitted_socket:
                operational_controls.release_socket(
                    room.room_code, player_id, id(websocket)
                )
            disconnected_active_socket = registered and connection_manager.disconnect(
                room.room_code, player_id, websocket
            )
            if disconnected_active_socket:
                try:
                    if room_service.player_in_room(room.room_code, player_id):
                        room = await mark_disconnected(room.room_code, player_id)
                        await _broadcast_presence(
                            connection_manager,
                            room_service,
                            room,
                            "player_disconnected",
                            player_id,
                            mark_disconnected,
                        )
                except HTTPException:
                    pass

    setattr(router, "cleanup_deleted_rooms", cleanup_deleted_rooms)
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
    cancel_grace: Callable[[str, str], None],
    mark_disconnected: Callable[[str, str], Awaitable[Any]],
    correlation_id: str,
) -> None:
    if event_type == "set_ready":
        ready = bool(payload.get("ready", False))
        room = room_service.set_ready(room.room_code, player_id, ready)
        await _broadcast_snapshot(
            connection_manager,
            room_service,
            room,
            "room_snapshot",
            player_id,
            mark_disconnected,
        )
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
        await _broadcast_snapshot(
            connection_manager,
            room_service,
            room,
            "room_snapshot",
            player_id,
            mark_disconnected,
        )
        return

    if event_type == "start_game":
        room_service.require_host(room, player_id)
        if room.status != RoomStatus.WAITING:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Game already started.")
        if len(room.players) < 2:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="At least two players are required.")
        if any(not player.is_ready for player in room.players):
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="All players must be ready.")
        room.status = RoomStatus.STARTING
        room = room_service.save(room)
        await _broadcast_snapshot(
            connection_manager,
            room_service,
            room,
            "game_starting",
            player_id,
            mark_disconnected,
        )
        try:
            room = await game_service.start_game(room)
        except Exception:
            room.status = RoomStatus.WAITING
            room = room_service.save(room)
            await _broadcast_snapshot(
                connection_manager,
                room_service,
                room,
                "room_snapshot",
                player_id,
                mark_disconnected,
            )
            raise
        room = room_service.save(room)
        await _broadcast_snapshot(
            connection_manager,
            room_service,
            room,
            "game_snapshot",
            player_id,
            mark_disconnected,
        )
        return

    if event_type == "replay_game":
        room_service.require_host(room, player_id)
        if room.status != RoomStatus.FINISHED:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Game has not finished.")
        room = game_service.reset_game(room)
        room = room_service.save(room)
        await _broadcast_snapshot(
            connection_manager,
            room_service,
            room,
            "room_snapshot",
            player_id,
            mark_disconnected,
        )
        return

    if event_type == "submit_answer":
        if room.status != RoomStatus.PLAYING:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Game is not active.")
        if room.game.last_round is not None:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Resolve the current result first.")
        if room.game.pending_round is not None:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Resolve the current round first.")
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
        await _broadcast_snapshot(
            connection_manager,
            room_service,
            room,
            "game_snapshot",
            player_id,
            mark_disconnected,
        )
        return

    if event_type == "pass_turn":
        if room.status != RoomStatus.PLAYING:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Game is not active.")
        if room.game.last_round is not None:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Resolve the current result first.")
        if room.game.pending_round is not None:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Resolve the current round first.")
        if room.current_turn_player_id != player_id:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="It is not your turn.")
        room = game_service.pass_turn(room, player_id)
        room = room_service.save(room)
        await _broadcast_snapshot(
            connection_manager,
            room_service,
            room,
            "game_snapshot",
            player_id,
            mark_disconnected,
        )
        return

    if event_type == "continue_pass_result":
        if room.status != RoomStatus.PLAYING:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Game is not active.")
        if room.game.last_pass is not None and room.game.last_pass.player_id != player_id:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Only the passing player can resolve this draw.")
        room = game_service.continue_pass_result(room)
        room = room_service.save(room)
        await _broadcast_snapshot(
            connection_manager,
            room_service,
            room,
            "game_snapshot",
            player_id,
            mark_disconnected,
        )
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
        if result is not None:
            await _broadcast_round_resolved(
                connection_manager,
                room_service,
                room,
                player_id,
                mark_disconnected,
            )
        else:
            await _broadcast_snapshot(
                connection_manager,
                room_service,
                room,
                "game_snapshot",
                player_id,
                mark_disconnected,
            )
        return

    if event_type == "continue_round_result":
        if room.status not in (RoomStatus.PLAYING, RoomStatus.FINISHED):
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Game is not active.")
        if room.game.last_round is None:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail={"code": "no_round_result"})
        if room.game.last_round.player_id != player_id:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail={"code": "not_result_owner"})
        room = game_service.continue_round_result(room)
        room = room_service.save(room)
        await _broadcast_snapshot(
            connection_manager,
            room_service,
            room,
            "game_snapshot",
            player_id,
            mark_disconnected,
        )
        return

    if event_type == "leave_room":
        cancel_grace(room.room_code, player_id)
        updated_room, result, _ = room_service.leave_room_idempotent(
            correlation_id, room.room_code, player_id
        )
        if updated_room is not None:
            if result is not None:
                await _broadcast_round_resolved(
                    connection_manager,
                    room_service,
                    updated_room,
                    player_id,
                    mark_disconnected,
                )
            else:
                await _broadcast_snapshot(
                    connection_manager,
                    room_service,
                    updated_room,
                    "player_left",
                    player_id,
                    mark_disconnected,
                )
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

    await _send_error(
        websocket,
        room.room_code,
        player_id,
        "Unsupported event type.",
        code="unsupported_event",
        correlation_id=correlation_id,
    )


async def _send_snapshot(
    connection_manager: ConnectionManager,
    room_service: RoomService,
    room,
    target_player_id: str,
    event_type: str,
    actor_player_id: str,
) -> bool:
    return await connection_manager.send_to_player(
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
    mark_disconnected: Callable[[str, str], Awaitable[Any]] | None = None,
) -> None:
    stale_player_ids: list[str] = []
    for player in room.players:
        sent = await _send_snapshot(connection_manager, room_service, room, player.player_id, event_type, actor_player_id)
        if not sent and player.is_connected:
            stale_player_ids.append(player.player_id)

    if not stale_player_ids:
        return

    for player_id in stale_player_ids:
        room = (
            await mark_disconnected(room.room_code, player_id)
            if mark_disconnected is not None
            else room_service.mark_connected(room.room_code, player_id, False)
        )

    for player in room.players:
        if player.player_id not in stale_player_ids:
            await _send_snapshot(
                connection_manager,
                room_service,
                room,
                player.player_id,
                "player_disconnected",
                actor_player_id,
            )


async def _broadcast_presence(
    connection_manager: ConnectionManager,
    room_service: RoomService,
    room,
    event_type: str,
    actor_player_id: str,
    mark_disconnected: Callable[[str, str], Awaitable[Any]] | None = None,
) -> None:
    await _broadcast_snapshot(
        connection_manager,
        room_service,
        room,
        event_type,
        actor_player_id,
        mark_disconnected,
    )


async def _send_command_result(
    connection_manager: ConnectionManager,
    room_service: RoomService,
    room,
    player_id: str,
    command_id: str,
    command_type: str,
    *,
    replayed: bool,
) -> bool:
    payload = {
        "commandId": command_id,
        "commandType": command_type,
        "status": "applied",
        "replayed": replayed,
    }
    if replayed and command_type != "leave_room":
        payload["snapshot"] = room_service.build_snapshot(room, player_id).model_dump(mode="json", by_alias=True)
    return await connection_manager.send_to_player(
        room.room_code,
        player_id,
        GameEvent(
            type="command_result",
            roomCode=room.room_code,
            playerId=player_id,
            payload=payload,
        ),
    )


async def _send_rejected_command_result(
    websocket: WebSocket,
    room_code: str,
    player_id: str,
    command_id: str,
    command_type: str,
    code: str,
) -> None:
    await websocket.send_json(
        GameEvent(
            type="command_result",
            roomCode=room_code,
            playerId=player_id,
            payload={
                "commandId": command_id,
                "commandType": command_type,
                "status": "rejected",
                "code": code,
            },
        ).model_dump(mode="json", by_alias=True)
    )


async def _broadcast_round_resolved(
    connection_manager: ConnectionManager,
    room_service: RoomService,
    room,
    actor_player_id: str,
    mark_disconnected: Callable[[str, str], Awaitable[Any]] | None = None,
) -> None:
    stale_player_ids: list[str] = []
    for player in room.players:
        sent = await connection_manager.send_to_player(
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
        if not sent and player.is_connected:
            stale_player_ids.append(player.player_id)

    if not stale_player_ids:
        return

    for player_id in stale_player_ids:
        room = (
            await mark_disconnected(room.room_code, player_id)
            if mark_disconnected is not None
            else room_service.mark_connected(room.room_code, player_id, False)
        )

    for player in room.players:
        if player.player_id not in stale_player_ids:
            await _send_snapshot(
                connection_manager,
                room_service,
                room,
                player.player_id,
                "player_disconnected",
                actor_player_id,
            )


async def _send_error(
    websocket: WebSocket,
    room_code: str,
    player_id: str,
    message: str,
    *,
    code: str,
    correlation_id: str,
) -> None:
    await websocket.send_json(
        GameEvent(
            type="error",
            roomCode=room_code,
            playerId=player_id,
            payload={
                "code": code,
                "message": message,
                "correlationId": correlation_id,
            },
        ).model_dump(mode="json", by_alias=True),
    )
