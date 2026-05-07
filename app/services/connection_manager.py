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

    async def connect(self, room_code: str, player_id: str, websocket: WebSocket) -> None:
        await websocket.accept()
        self._connections[room_code][player_id] = websocket

    def disconnect(self, room_code: str, player_id: str) -> None:
        room_connections = self._connections.get(room_code)
        if not room_connections:
            return

        room_connections.pop(player_id, None)
        if not room_connections:
            self._connections.pop(room_code, None)

    async def send_to_player(
        self,
        room_code: str,
        player_id: str,
        event: GameEvent,
    ) -> None:
        websocket = self._connections.get(room_code, {}).get(player_id)
        if websocket is None:
            return

        await self._safe_send(websocket, event)

    async def broadcast(self, room_code: str, event: GameEvent) -> None:
        room_connections = list(self._connections.get(room_code, {}).items())
        stale_player_ids: list[str] = []

        for player_id, websocket in room_connections:
            sent = await self._safe_send(websocket, event)
            if not sent:
                stale_player_ids.append(player_id)

        for player_id in stale_player_ids:
            self.disconnect(room_code, player_id)

    async def broadcast_except(
        self,
        room_code: str,
        excluded_player_id: str,
        event: GameEvent,
    ) -> None:
        room_connections = list(self._connections.get(room_code, {}).items())
        stale_player_ids: list[str] = []

        for player_id, websocket in room_connections:
            if player_id == excluded_player_id:
                continue

            sent = await self._safe_send(websocket, event)
            if not sent:
                stale_player_ids.append(player_id)

        for player_id in stale_player_ids:
            self.disconnect(room_code, player_id)

    async def _safe_send(self, websocket: WebSocket, event: GameEvent) -> bool:
        try:
            await websocket.send_json(event.model_dump(mode="json", by_alias=True))
            return True
        except Exception:
            return False
