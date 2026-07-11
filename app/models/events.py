from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.models.game import ALLOWED_CARDS_PER_PLAYER, VoteChoice, WrongAnswerBehavior


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
    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    region_id: str | None = Field(
        default=None,
        min_length=1,
        max_length=64,
        strict=True,
        alias="regionId",
    )
    cards_per_player: int | None = Field(
        default=None,
        strict=True,
        alias="cardsPerPlayer",
    )
    language: str | None = Field(default=None, min_length=2, max_length=8, strict=True)
    wrong_answer_behavior: WrongAnswerBehavior | None = Field(
        default=None,
        alias="wrongAnswerBehavior",
    )
    max_players: int | None = Field(
        default=None,
        ge=2,
        le=8,
        strict=True,
        alias="maxPlayers",
    )

    @field_validator("cards_per_player")
    @classmethod
    def validate_cards_per_player(cls, value: int | None) -> int | None:
        if value is not None and value not in ALLOWED_CARDS_PER_PLAYER:
            raise ValueError("cardsPerPlayer must be one of 4, 6, 8, 11, or 14.")
        return value


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
    "pass_turn",
    "continue_pass_result",
    "cast_vote",
    "continue_round_result",
    "leave_room",
    "ping",
]
