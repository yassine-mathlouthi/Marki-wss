from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field

from app.models.player import Player


class RoomStatus(str, Enum):
    WAITING = "waiting"
    PLAYING = "playing"
    FINISHED = "finished"


class Room(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    room_code: str = Field(min_length=1, max_length=6, alias="roomCode")
    host_player_id: str = Field(alias="hostPlayerId")
    players: list[Player] = Field(default_factory=list)
    status: RoomStatus = RoomStatus.WAITING
    max_players: int = Field(ge=2, le=8, alias="maxPlayers")
    current_turn_player_id: Optional[str] = Field(
        default=None,
        alias="currentTurnPlayerId",
    )
    scores: dict[str, int] = Field(default_factory=dict)
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        alias="createdAt",
    )
    updated_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        alias="updatedAt",
    )


class CreateRoomRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    host_name: str = Field(min_length=1, max_length=32, alias="hostName")
    max_players: int = Field(ge=2, le=8, alias="maxPlayers")


class JoinRoomRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    room_code: str = Field(min_length=1, max_length=6, alias="roomCode")
    player_name: str = Field(min_length=1, max_length=32, alias="playerName")


class LeaveRoomRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    player_id: str = Field(min_length=1, alias="playerId")


class CreateRoomResponse(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    room_code: str = Field(alias="roomCode")
    player_id: str = Field(alias="playerId")
    room: Room


class JoinRoomResponse(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    room_code: str = Field(alias="roomCode")
    player_id: str = Field(alias="playerId")
    room: Room
