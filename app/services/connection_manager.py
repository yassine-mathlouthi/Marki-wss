from __future__ import annotations

from collections import defaultdict
from typing import DefaultDict

from fastapi import WebSocket

from app.models.events import GameEvent


class ConnectionManager:
    def __init__(self) -> None:
        # TODO: For multi-instance production, move active connection metadata to Redis
        # and broadcast room events through Redis pub/sub so all instances stay in sync.
        self._connections: DefaultDict[str, dict[str, WebSocket]] = defaultdict(dict)

    async def connect(
        self,
        room_code: str,
        player_id: str,
        websocket: WebSocket,
        *,
        accept: bool = True,
    ) -> None:
        if accept:
            await websocket.accept()
        previous_websocket = self._connections[room_code].get(player_id)
        self._connections[room_code][player_id] = websocket
        if previous_websocket is not None and previous_websocket is not websocket:
            try:
                await previous_websocket.close()
            except Exception:
                pass

    def disconnect(
        self,
        room_code: str,
        player_id: str,
        websocket: WebSocket | None = None,
    ) -> bool:
        room_connections = self._connections.get(room_code)
        if not room_connections:
            return False

        if websocket is not None and room_connections.get(player_id) is not websocket:
            return False

        room_connections.pop(player_id, None)
        if not room_connections:
            self._connections.pop(room_code, None)
        return True

    async def send_to_player(
        self,
        room_code: str,
        player_id: str,
        event: GameEvent,
    ) -> bool:
        websocket = self._connections.get(room_code, {}).get(player_id)
        if websocket is None:
            return False

        sent = await self._safe_send(websocket, event)
        if not sent:
            self.disconnect(room_code, player_id, websocket)
        return sent

    async def broadcast(self, room_code: str, event: GameEvent) -> None:
        room_connections = list(self._connections.get(room_code, {}).items())
        stale_connections: list[tuple[str, WebSocket]] = []

        for player_id, websocket in room_connections:
            sent = await self._safe_send(websocket, event)
            if not sent:
                stale_connections.append((player_id, websocket))

        for player_id, websocket in stale_connections:
            self.disconnect(room_code, player_id, websocket)

    async def broadcast_except(
        self,
        room_code: str,
        excluded_player_id: str,
        event: GameEvent,
    ) -> None:
        room_connections = list(self._connections.get(room_code, {}).items())
        stale_connections: list[tuple[str, WebSocket]] = []

        for player_id, websocket in room_connections:
            if player_id == excluded_player_id:
                continue

            sent = await self._safe_send(websocket, event)
            if not sent:
                stale_connections.append((player_id, websocket))

        for player_id, websocket in stale_connections:
            self.disconnect(room_code, player_id, websocket)

    async def _safe_send(self, websocket: WebSocket, event: GameEvent) -> bool:
        try:
            await websocket.send_json(event.model_dump(mode="json", by_alias=True))
            return True
        except Exception:
            return False
