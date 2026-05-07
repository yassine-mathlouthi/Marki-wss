from __future__ import annotations

import random
import string
from datetime import datetime, timezone

from fastapi import HTTPException, status

from app.models.player import Player
from app.models.room import Room, RoomStatus


class RoomService:
    def __init__(self) -> None:
        # TODO: Replace this in-memory storage with Redis for multi-instance production.
        # TODO: Add Redis TTL/expiration per room for automatic cleanup of abandoned rooms.
        self._rooms: dict[str, Room] = {}

    def create_room(self, host_name: str, max_players: int) -> tuple[Room, Player]:
        room_code = self._generate_unique_room_code()
        host_player = Player(name=host_name.strip(), is_host=True, is_connected=False)
        now = self._now()
        room = Room(
            roomCode=room_code,
            hostPlayerId=host_player.player_id,
            players=[host_player],
            status=RoomStatus.WAITING,
            maxPlayers=max_players,
            currentTurnPlayerId=host_player.player_id,
            scores={host_player.player_id: 0},
            createdAt=now,
            updatedAt=now,
        )
        self._rooms[room_code] = room
        return room, host_player

    def join_room(self, room_code: str, player_name: str) -> tuple[Room, Player]:
        room = self._get_room_or_raise(room_code)

        if room.status != RoomStatus.WAITING:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="This room has already started.",
            )
        if len(room.players) >= room.max_players:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="This room is full.",
            )

        player = Player(name=player_name.strip())
        room.players.append(player)
        room.scores[player.player_id] = 0
        room.updated_at = self._now()
        return room, player

    def get_room(self, room_code: str) -> Room:
        return self._get_room_or_raise(room_code)

    def leave_room(self, room_code: str, player_id: str) -> Room | None:
        room = self._get_room_or_raise(room_code)
        player = self._get_player_or_raise(room, player_id)

        room.players = [existing for existing in room.players if existing.player_id != player.player_id]
        room.scores.pop(player.player_id, None)

        if not room.players:
            self.delete_room(room.room_code)
            return None

        if room.host_player_id == player.player_id:
            new_host = room.players[0]
            new_host.is_host = True
            room.host_player_id = new_host.player_id

        room.current_turn_player_id = self._resolve_current_turn(room)
        room.updated_at = self._now()
        return room

    def delete_room(self, room_code: str) -> None:
        normalized_code = self._normalize_room_code(room_code)
        self._rooms.pop(normalized_code, None)

    def mark_connected(self, room_code: str, player_id: str, connected: bool) -> Room:
        room = self._get_room_or_raise(room_code)
        player = self._get_player_or_raise(room, player_id)
        player.is_connected = connected
        room.updated_at = self._now()
        return room

    def set_ready(self, room_code: str, player_id: str, ready: bool) -> Room:
        room = self._get_room_or_raise(room_code)
        player = self._get_player_or_raise(room, player_id)
        player.is_ready = ready
        room.updated_at = self._now()
        return room

    def start_game(self, room_code: str, player_id: str) -> Room:
        room = self._get_room_or_raise(room_code)
        if room.host_player_id != player_id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Only the host can start the game.",
            )
        if len(room.players) < 2:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="At least two players are required to start the game.",
            )

        room.status = RoomStatus.PLAYING
        room.current_turn_player_id = room.current_turn_player_id or room.players[0].player_id
        room.updated_at = self._now()
        return room

    def next_turn(self, room_code: str) -> Room:
        room = self._get_room_or_raise(room_code)
        if not room.players:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="There are no players in this room.",
            )

        current_id = room.current_turn_player_id
        player_ids = [player.player_id for player in room.players]
        if current_id not in player_ids:
            room.current_turn_player_id = player_ids[0]
        else:
            current_index = player_ids.index(current_id)
            next_index = (current_index + 1) % len(player_ids)
            room.current_turn_player_id = player_ids[next_index]

        room.updated_at = self._now()
        return room

    def update_score(self, room_code: str, target_player_id: str, points: int) -> Room:
        room = self._get_room_or_raise(room_code)
        self._get_player_or_raise(room, target_player_id)
        room.scores[target_player_id] = room.scores.get(target_player_id, 0) + points
        room.updated_at = self._now()
        return room

    def player_in_room(self, room_code: str, player_id: str) -> bool:
        room = self._get_room_or_raise(room_code)
        return any(player.player_id == player_id for player in room.players)

    def _generate_unique_room_code(self) -> str:
        for _ in range(100):
            room_code = "".join(random.choices(string.ascii_uppercase + string.digits, k=6))
            if room_code not in self._rooms:
                return room_code

        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Unable to create a room right now.",
        )

    def _get_room_or_raise(self, room_code: str) -> Room:
        normalized_code = self._normalize_room_code(room_code)
        room = self._rooms.get(normalized_code)
        if room is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Room not found.",
            )
        return room

    @staticmethod
    def _get_player_or_raise(room: Room, player_id: str) -> Player:
        for player in room.players:
            if player.player_id == player_id:
                return player
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Player not found in this room.",
        )

    @staticmethod
    def _normalize_room_code(room_code: str) -> str:
        normalized = room_code.strip().upper()
        if not normalized or len(normalized) > 6:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Invalid room code.",
            )
        return normalized

    @staticmethod
    def _resolve_current_turn(room: Room) -> str | None:
        if not room.players:
            return None
        if room.current_turn_player_id in {player.player_id for player in room.players}:
            return room.current_turn_player_id
        return room.players[0].player_id

    @staticmethod
    def _now() -> datetime:
        return datetime.now(timezone.utc)


room_service = RoomService()
