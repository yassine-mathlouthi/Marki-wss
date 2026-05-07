from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class GameEvent(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    type: str
    room_code: str = Field(alias="roomCode")
    player_id: str = Field(alias="playerId")
    payload: dict[str, Any] = Field(default_factory=dict)
    timestamp: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
    )


class ReadyPayload(BaseModel):
    ready: bool


class SubmitAnswerPayload(BaseModel):
    answer: str = Field(min_length=1, max_length=128)
    card_id: str | None = Field(default=None, alias="cardId")

    model_config = ConfigDict(populate_by_name=True)


class UpdateScorePayload(BaseModel):
    target_player_id: str = Field(alias="targetPlayerId")
    points: int

    model_config = ConfigDict(populate_by_name=True)


IncomingEventType = Literal[
    "ready",
    "start_game",
    "submit_answer",
    "update_score",
    "next_turn",
    "ping",
]
