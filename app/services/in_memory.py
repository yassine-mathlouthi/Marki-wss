from __future__ import annotations

import random
import string

from app.models.events import GameEvent
from app.models.room import Room
from app.services.connection_manager import ConnectionManager
from app.services.interfaces import RoomEventBus, RoomStore


class InMemoryRoomStore(RoomStore):
    def __init__(self) -> None:
        self._rooms: dict[str, Room] = {}

    def generate_room_code(self) -> str:
        for _ in range(100):
            room_code = "".join(random.choices(string.ascii_uppercase + string.digits, k=6))
            if room_code not in self._rooms:
                return room_code
        raise RuntimeError("Unable to create a unique room code.")

    def save(self, room: Room) -> Room:
        self._rooms[room.room_code] = room
        return room

    def get(self, room_code: str) -> Room | None:
        return self._rooms.get(room_code)

    def delete(self, room_code: str) -> None:
        self._rooms.pop(room_code, None)


class InMemoryRoomEventBus(RoomEventBus):
    def __init__(self, connection_manager: ConnectionManager) -> None:
        self._connection_manager = connection_manager

    async def publish(self, room_code: str, event: GameEvent) -> None:
        await self._connection_manager.broadcast(room_code, event)
