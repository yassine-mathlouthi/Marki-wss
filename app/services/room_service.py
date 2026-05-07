from __future__ import annotations

from datetime import datetime, timezone

from fastapi import HTTPException, status

from app.models.game import GameState, LobbySettings, RoomSnapshot, RoomSnapshotPlayer
from app.models.player import Player
from app.models.room import Room, RoomStatus
from app.services.interfaces import RoomStore


class RoomService:
    def __init__(self, store: RoomStore) -> None:
        self._store = store

    def create_room(self, host_name: str, max_players: int, settings: LobbySettings) -> tuple[Room, Player]:
        room_code = self._store.generate_room_code()
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
            settings=settings,
            game=GameState(),
            createdAt=now,
            updatedAt=now,
        )
        self._store.save(room)
        return room, host_player

    def join_room(self, room_code: str, player_name: str) -> tuple[Room, Player]:
        room = self.get_room(room_code)
        if room.status != RoomStatus.WAITING:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="This room has already started.")
        if len(room.players) >= room.max_players:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="This room is full.")
        player = Player(name=player_name.strip())
        room.players.append(player)
        room.scores[player.player_id] = 0
        self._touch(room)
        self._store.save(room)
        return room, player

    def get_room(self, room_code: str) -> Room:
        normalized_code = self.normalize_room_code(room_code)
        room = self._store.get(normalized_code)
        if room is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Room not found.")
        return room

    def leave_room(self, room_code: str, player_id: str) -> Room | None:
        room = self.get_room(room_code)
        player = self.get_player(room, player_id)
        room.players = [existing for existing in room.players if existing.player_id != player.player_id]
        room.scores.pop(player.player_id, None)
        if not room.players:
            self._store.delete(room.room_code)
            return None
        if room.host_player_id == player.player_id:
            room.host_player_id = room.players[0].player_id
            room.players[0].is_host = True
        room.current_turn_player_id = self._resolve_current_turn(room)
        self._touch(room)
        self._store.save(room)
        return room

    def mark_connected(self, room_code: str, player_id: str, connected: bool) -> Room:
        room = self.get_room(room_code)
        player = self.get_player(room, player_id)
        player.is_connected = connected
        self._touch(room)
        self._store.save(room)
        return room

    def set_ready(self, room_code: str, player_id: str, ready: bool) -> Room:
        room = self.get_room(room_code)
        if room.status != RoomStatus.WAITING:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Game already started.")
        player = self.get_player(room, player_id)
        player.is_ready = ready
        self._touch(room)
        self._store.save(room)
        return room

    def update_lobby_settings(
        self,
        room_code: str,
        player_id: str,
        *,
        region_id: str | None = None,
        cards_per_player: int | None = None,
        language: str | None = None,
        wrong_answer_behavior=None,
        max_players: int | None = None,
    ) -> Room:
        room = self.get_room(room_code)
        self.require_host(room, player_id)
        if room.status != RoomStatus.WAITING:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Cannot edit lobby after game start.")
        if region_id:
            room.settings.region_id = region_id
        if cards_per_player:
            room.settings.cards_per_player = cards_per_player
        if language:
            room.settings.language = language
        if wrong_answer_behavior:
            room.settings.wrong_answer_behavior = wrong_answer_behavior
        if max_players:
            if max_players < len(room.players):
                raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="maxPlayers is below joined players.")
            room.max_players = max_players
        self._touch(room)
        self._store.save(room)
        return room

    def player_in_room(self, room_code: str, player_id: str) -> bool:
        room = self.get_room(room_code)
        return any(player.player_id == player_id for player in room.players)

    def save(self, room: Room) -> Room:
        self._touch(room)
        return self._store.save(room)

    def build_snapshot(self, room: Room, viewer_player_id: str | None) -> RoomSnapshot:
        players = []
        for player in room.players:
            players.append(
                RoomSnapshotPlayer(
                    playerId=player.player_id,
                    name=player.name,
                    isHost=player.is_host,
                    isReady=player.is_ready,
                    isConnected=player.is_connected,
                    score=room.scores.get(player.player_id, 0),
                    handCount=len(player.hand),
                    hand=[card.model_copy(deep=True) for card in player.hand]
                    if viewer_player_id == player.player_id
                    else None,
                )
            )

        game_payload = None
        if room.game is not None:
            pending_round = room.game.pending_round.model_dump(mode="json", by_alias=True) if room.game.pending_round else None
            if pending_round is not None and viewer_player_id is not None:
                if pending_round["playerId"] != viewer_player_id:
                    pending_round["votes"] = [
                        vote for vote in pending_round["votes"] if vote["playerId"] == viewer_player_id
                    ]
            game_payload = {
                "tableCards": [card.model_dump(mode="json", by_alias=True) for card in room.game.table_cards],
                "discardPile": [card.model_dump(mode="json", by_alias=True) for card in room.game.discard_pile],
                "deckCount": len(room.game.deck),
                "drawPoolCount": len(room.game.draw_pool),
                "pendingRound": pending_round,
                "lastRound": room.game.last_round.model_dump(mode="json", by_alias=True)
                if room.game.last_round
                else None,
            }

        return RoomSnapshot(
            roomCode=room.room_code,
            hostPlayerId=room.host_player_id,
            status=room.status.value,
            maxPlayers=room.max_players,
            currentTurnPlayerId=room.current_turn_player_id,
            settings=room.settings,
            players=players,
            game=game_payload,
            createdAt=room.created_at,
            updatedAt=room.updated_at,
        )

    def require_host(self, room: Room, player_id: str) -> None:
        if room.host_player_id != player_id:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Only the host can perform this action.")

    def get_player(self, room: Room, player_id: str) -> Player:
        for player in room.players:
            if player.player_id == player_id:
                return player
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Player not found in this room.")

    @staticmethod
    def normalize_room_code(room_code: str) -> str:
        normalized = room_code.strip().upper()
        if not normalized or len(normalized) > 6:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Invalid room code.")
        return normalized

    @staticmethod
    def _resolve_current_turn(room: Room) -> str | None:
        if not room.players:
            return None
        player_ids = {player.player_id for player in room.players}
        if room.current_turn_player_id in player_ids:
            return room.current_turn_player_id
        return room.players[0].player_id

    @staticmethod
    def _touch(room: Room) -> None:
        room.updated_at = RoomService._now()

    @staticmethod
    def _now() -> datetime:
        return datetime.now(timezone.utc)
