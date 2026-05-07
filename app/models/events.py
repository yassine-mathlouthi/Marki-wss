from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.models.game import VoteChoice, WrongAnswerBehavior


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


class UpdateLobbySettingsPayload(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    region_id: str | None = Field(default=None, alias="regionId")
    cards_per_player: int | None = Field(default=None, alias="cardsPerPlayer")
    language: str | None = None
    wrong_answer_behavior: WrongAnswerBehavior | None = Field(
        default=None,
        alias="wrongAnswerBehavior",
    )
    max_players: int | None = Field(default=None, alias="maxPlayers")


class SubmitAnswerPayload(BaseModel):
    answer: str = Field(min_length=1, max_length=128)
    card_id: str | None = Field(default=None, alias="cardId")
    related_card_id: str = Field(alias="relatedCardId")

    model_config = ConfigDict(populate_by_name=True)


class CastVotePayload(BaseModel):
    choice: VoteChoice

    model_config = ConfigDict(populate_by_name=True)


IncomingEventType = Literal[
    "set_ready",
    "update_lobby_settings",
    "start_game",
    "submit_answer",
    "cast_vote",
    "leave_room",
    "ping",
]
