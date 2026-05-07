from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from app.models.game import GameCard

class Player(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    player_id: str = Field(default_factory=lambda: str(uuid4()), alias="playerId")
    name: str = Field(min_length=1, max_length=32)
    is_host: bool = Field(default=False, alias="isHost")
    is_ready: bool = Field(default=False, alias="isReady")
    is_connected: bool = Field(default=False, alias="isConnected")
    hand: list[GameCard] = Field(default_factory=list)
    joined_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        alias="joinedAt",
    )
