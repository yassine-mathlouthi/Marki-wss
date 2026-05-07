from __future__ import annotations

from abc import ABC, abstractmethod

from app.models.events import GameEvent
from app.models.room import Room


class RoomStore(ABC):
    @abstractmethod
    def generate_room_code(self) -> str:
        raise NotImplementedError

    @abstractmethod
    def save(self, room: Room) -> Room:
        raise NotImplementedError

    @abstractmethod
    def get(self, room_code: str) -> Room | None:
        raise NotImplementedError

    @abstractmethod
    def delete(self, room_code: str) -> None:
        raise NotImplementedError


class RoomEventBus(ABC):
    @abstractmethod
    async def publish(self, room_code: str, event: GameEvent) -> None:
        raise NotImplementedError
