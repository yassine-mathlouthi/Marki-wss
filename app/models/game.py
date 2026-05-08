from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any

from pydantic import AliasChoices, BaseModel, ConfigDict, Field


class WrongAnswerBehavior(str, Enum):
    RETURN_TO_HAND = "returnToHand"
    DISCARD_CARD = "discardCard"


class VoteChoice(str, Enum):
    CORRECT = "correct"
    WRONG = "wrong"


class GameCard(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    id: str = Field(validation_alias=AliasChoices("id", "_id"))
    type: str
    region_id: str = Field(alias="regionId")
    pack_id: str = Field(alias="packId")
    names: dict[str, str]
    icon: str = ""
    tags: list[str] = Field(default_factory=list)


class LobbySettings(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    region_id: str = Field(default="tunisia", alias="regionId")
    cards_per_player: int = Field(default=11, alias="cardsPerPlayer", ge=3, le=20)
    language: str = Field(default="en", min_length=2, max_length=8)
    wrong_answer_behavior: WrongAnswerBehavior = Field(
        default=WrongAnswerBehavior.DISCARD_CARD,
        alias="wrongAnswerBehavior",
    )


class Vote(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    player_id: str = Field(alias="playerId")
    choice: VoteChoice


class PendingRound(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    player_id: str = Field(alias="playerId")
    played_card: GameCard = Field(alias="playedCard")
    related_card: GameCard = Field(alias="relatedCard")
    answer_text: str = Field(alias="answerText")
    previous_accepted_answer: str | None = Field(
        default=None,
        alias="previousAcceptedAnswer",
    )
    votes: list[Vote] = Field(default_factory=list)


class RoundResult(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    player_id: str = Field(alias="playerId")
    played_card: GameCard = Field(alias="playedCard")
    related_card: GameCard = Field(alias="relatedCard")
    answer_text: str = Field(alias="answerText")
    votes: list[Vote] = Field(default_factory=list)
    accepted: bool
    correct_votes: int = Field(alias="correctVotes")
    wrong_votes: int = Field(alias="wrongVotes")
    previous_accepted_answer: str | None = Field(
        default=None,
        alias="previousAcceptedAnswer",
    )
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        alias="createdAt",
    )


class GameState(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    draw_pool: list[GameCard] = Field(default_factory=list, alias="drawPool")
    deck: list[GameCard] = Field(default_factory=list)
    table_cards: list[GameCard] = Field(default_factory=list, alias="tableCards")
    discard_pile: list[GameCard] = Field(default_factory=list, alias="discardPile")
    pending_round: PendingRound | None = Field(default=None, alias="pendingRound")
    last_round: RoundResult | None = Field(default=None, alias="lastRound")
    accepted_answers: dict[str, str] = Field(default_factory=dict, alias="acceptedAnswers")


class RoomSnapshotPlayer(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    player_id: str = Field(alias="playerId")
    name: str
    is_host: bool = Field(alias="isHost")
    is_ready: bool = Field(alias="isReady")
    is_connected: bool = Field(alias="isConnected")
    score: int = 0
    hand_count: int = Field(alias="handCount")
    hand: list[GameCard] | None = None


class RoomSnapshot(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    room_code: str = Field(alias="roomCode")
    host_player_id: str = Field(alias="hostPlayerId")
    status: str
    max_players: int = Field(alias="maxPlayers")
    current_turn_player_id: str | None = Field(default=None, alias="currentTurnPlayerId")
    settings: LobbySettings
    players: list[RoomSnapshotPlayer]
    game: dict[str, Any] | None = None
    created_at: datetime = Field(alias="createdAt")
    updated_at: datetime = Field(alias="updatedAt")

