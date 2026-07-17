from __future__ import annotations

import hashlib
import secrets
import time
from hmac import compare_digest
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Callable

from fastapi import HTTPException, status

from app.models.game import (
    GameState,
    LobbySettings,
    RoomSnapshot,
    RoomSnapshotPlayer,
    WrongAnswerBehavior,
)
from app.models.player import Player
from app.models.room import Room, RoomStatus
from app.services.interfaces import RoomStore

if TYPE_CHECKING:
    from app.services.game_service import GameService


class RoomService:
    def __init__(
        self,
        store: RoomStore,
        game_service: GameService | None = None,
        *,
        idempotency_ttl_seconds: float = 86400.0,
        waiting_room_ttl_seconds: float = 900.0,
        playing_room_ttl_seconds: float = 3600.0,
        finished_room_ttl_seconds: float = 900.0,
    ) -> None:
        self._store = store
        self._game_service = game_service
        self._idempotency_records: dict[str, tuple[str, str, str, str, str, float]] = {}
        self._leave_records: dict[str, tuple[str, str, float]] = {}
        self._idempotency_ttl_seconds = idempotency_ttl_seconds
        self._room_ttl_seconds = {
            RoomStatus.WAITING: waiting_room_ttl_seconds,
            RoomStatus.STARTING: playing_room_ttl_seconds,
            RoomStatus.PLAYING: playing_room_ttl_seconds,
            RoomStatus.FINISHED: finished_room_ttl_seconds,
        }
        self._room_deleted_listeners: list[Callable[[str], None]] = []
        self._room_deletion_observers: list[
            Callable[[str, str, RoomStatus], None]
        ] = []
        self._closed_rooms: dict[str, tuple[str, float]] = {}

    def add_room_deleted_listener(self, listener: Callable[[str], None]) -> None:
        self._room_deleted_listeners.append(listener)

    def add_room_deletion_observer(
        self, observer: Callable[[str, str, RoomStatus], None]
    ) -> None:
        self._room_deletion_observers.append(observer)

    def leave_room_idempotent(
        self, operation_id: str, room_code: str, player_id: str, *, replay_only: bool = False
    ):
        now = time.monotonic()
        self._leave_records = {
            key: value for key, value in self._leave_records.items()
            if now - value[2] <= self._idempotency_ttl_seconds
        }
        normalized = room_code.upper()
        existing = self._leave_records.get(operation_id)
        if existing is not None:
            if existing[:2] != (normalized, player_id):
                raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail={"code": "idempotency_key_reused"})
            return None, None, True
        if replay_only:
            return None, None, False
        room, result = self.leave_room_with_result(normalized, player_id)
        self._leave_records[operation_id] = (normalized, player_id, now)
        return room, result, False

    def create_room_idempotent(
        self,
        idempotency_key: str,
        fingerprint: str,
        host_name: str,
        max_players: int,
        settings: LobbySettings,
    ):
        replay = self._idempotency_replay(idempotency_key, "create", fingerprint)
        if replay is not None:
            room_code, player_id, session_token = replay
            room = self.get_room(room_code)
            return room, self.get_player(room, player_id), session_token, True
        room, player, session_token = self.create_room(host_name, max_players, settings)
        self._store_idempotency(
            idempotency_key, "create", fingerprint, room.room_code, player.player_id, session_token
        )
        return room, player, session_token, False

    def join_room_idempotent(
        self,
        idempotency_key: str,
        fingerprint: str,
        room_code: str,
        player_name: str,
    ):
        replay = self._idempotency_replay(idempotency_key, "join", fingerprint)
        if replay is not None:
            stored_room_code, player_id, session_token = replay
            room = self.get_room(stored_room_code)
            return room, self.get_player(room, player_id), session_token, True
        room, player, session_token = self.join_room(room_code, player_name)
        self._store_idempotency(
            idempotency_key, "join", fingerprint, room.room_code, player.player_id, session_token
        )
        return room, player, session_token, False

    def create_room(self, host_name: str, max_players: int, settings: LobbySettings) -> tuple[Room, Player, str]:
        room_code = self._store.generate_room_code()
        session_token = self._generate_session_token()
        host_player = Player(
            name=host_name.strip(),
            is_host=True,
            is_connected=False,
            session_token_hash=self._hash_session_token(session_token),
        )
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
            allDisconnectedAt=now,
            expiresAt=now + timedelta(seconds=self._room_ttl_seconds[RoomStatus.WAITING]),
        )
        self._store.save(room)
        return room, host_player, session_token

    def join_room(self, room_code: str, player_name: str) -> tuple[Room, Player, str]:
        room = self.get_room(room_code)
        if room.status != RoomStatus.WAITING:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail={"code": "room_already_started"})
        if len(room.players) >= room.max_players:
            self._evict_expired_lobby_player(room)
        if len(room.players) >= room.max_players:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail={"code": "room_full"})
        session_token = self._generate_session_token()
        player = Player(
            name=player_name.strip(),
            session_token_hash=self._hash_session_token(session_token),
        )
        room.players.append(player)
        room.scores[player.player_id] = 0
        self._refresh_lifecycle(room)
        self._touch(room)
        self._store.save(room)
        return room, player, session_token

    def get_room(self, room_code: str) -> Room:
        normalized_code = self.normalize_room_code(room_code)
        room = self._store.get(normalized_code)
        if room is None:
            self._prune_closed_rooms()
            closed = self._closed_rooms.get(normalized_code)
            code = closed[0] if closed is not None else "room_not_found"
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail={"code": code})
        return room

    def leave_room(self, room_code: str, player_id: str) -> Room | None:
        room, _ = self.leave_room_with_result(room_code, player_id)
        return room

    def active_room_count(self) -> int:
        return len(self._store.list_rooms())

    def presence_counts(self) -> tuple[int, int]:
        connected = 0
        disconnected_retained = 0
        for room in self._store.list_rooms():
            for player in room.players:
                if player.is_connected:
                    connected += 1
                else:
                    disconnected_retained += 1
        return connected, disconnected_retained

    def expire_abandoned_rooms(
        self,
        *,
        now: datetime,
        ttl_seconds: float,
    ) -> list[str]:
        expired: list[str] = []
        for room in self._store.list_rooms():
            if any(player.is_connected for player in room.players):
                continue
            self._refresh_lifecycle(room)
            if room.expires_at is None or now < room.expires_at:
                continue
            self._delete_room(room, reason="room_expired")
            expired.append(room.room_code)
        return expired

    def leave_room_with_result(self, room_code: str, player_id: str):
        room = self.get_room(room_code)
        player = self.get_player(room, player_id)
        room.players = [existing for existing in room.players if existing.player_id != player.player_id]
        room.scores.pop(player.player_id, None)
        if not room.players:
            self._delete_room(room, reason="room_closed")
            return None, None
        if room.host_player_id == player.player_id:
            connected_players = [
                candidate for candidate in room.players if candidate.is_connected
            ]
            self._assign_host(room, (connected_players or room.players)[0])
        self._remove_player_from_game(room, player.player_id)
        result = None
        if self._game_service is not None:
            room, result = self._game_service.reconcile_disconnected_player(room, player_id)
        room.current_turn_player_id = self._resolve_current_turn(room)
        self._touch(room)
        self._store.save(room)
        return room, result

    def mark_connected(self, room_code: str, player_id: str, connected: bool) -> Room:
        room = self.get_room(room_code)
        player = self.get_player(room, player_id)
        player.is_connected = connected
        if connected:
            player.connection_epoch += 1
            player.disconnected_at = None
            player.voting_excluded = False
            room.last_connected_at = self._now()
            current_host = self.get_player(room, room.host_player_id)
            if current_host.voting_excluded and not current_host.is_connected:
                self._assign_host(room, player)
        else:
            player.disconnected_at = self._now()
        self._refresh_lifecycle(room)
        self._touch(room)
        self._store.save(room)
        return room

    def expire_disconnected_player(
        self,
        room_code: str,
        player_id: str,
        connection_epoch: int,
    ):
        room = self.get_room(room_code)
        player = self.get_player(room, player_id)
        if player.is_connected or player.connection_epoch != connection_epoch:
            return room, None, False, False
        player.voting_excluded = True
        host_transferred = False
        if room.host_player_id == player_id:
            next_host = next(
                (
                    candidate
                    for candidate in room.players
                    if candidate.player_id != player_id and candidate.is_connected
                ),
                None,
            )
            if next_host is not None:
                self._assign_host(room, next_host)
                host_transferred = True
        result = None
        if self._game_service is not None:
            room, result = self._game_service.reconcile_disconnected_player(room, player_id)
        self._touch(room)
        self._store.save(room)
        return room, result, True, host_transferred

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
        wrong_answer_behavior: WrongAnswerBehavior | None = None,
        max_players: int | None = None,
    ) -> Room:
        room = self.get_room(room_code)
        self.require_host(room, player_id)
        if room.status != RoomStatus.WAITING:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Cannot edit lobby after game start.")
        if max_players is not None and max_players < len(room.players):
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="maxPlayers is below joined players.")

        settings_data = room.settings.model_dump()
        if region_id is not None:
            settings_data["region_id"] = region_id
        if cards_per_player is not None:
            settings_data["cards_per_player"] = cards_per_player
        if language is not None:
            settings_data["language"] = language
        if wrong_answer_behavior is not None:
            settings_data["wrong_answer_behavior"] = wrong_answer_behavior

        updated_settings = LobbySettings.model_validate(settings_data)
        room.settings = updated_settings
        if max_players is not None:
            room.max_players = max_players
        self._touch(room)
        self._store.save(room)
        return room

    def remove_expired_lobby_players(self, room: Room) -> Room:
        while any(
            not player.is_connected and player.voting_excluded
            for player in room.players
        ):
            self._evict_expired_lobby_player(room)
        return room

    def player_in_room(self, room_code: str, player_id: str) -> bool:
        room = self.get_room(room_code)
        return any(player.player_id == player_id for player in room.players)

    def save(self, room: Room) -> Room:
        self._refresh_lifecycle(room)
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
            last_pass = room.game.last_pass
            if last_pass is not None and last_pass.player_id != viewer_player_id:
                last_pass = None
            game_payload = {
                "tableCards": [card.model_dump(mode="json", by_alias=True) for card in room.game.table_cards],
                "discardPile": [card.model_dump(mode="json", by_alias=True) for card in room.game.discard_pile],
                "deckCount": len(room.game.deck),
                "drawPoolCount": len(room.game.draw_pool),
                "pendingRound": pending_round,
                "lastRound": room.game.last_round.model_dump(mode="json", by_alias=True)
                if room.game.last_round
                else None,
                "lastPass": last_pass.model_dump(mode="json", by_alias=True)
                if last_pass
                else None,
            }

        return RoomSnapshot(
            roomCode=room.room_code,
            hostPlayerId=room.host_player_id,
            hostEpoch=room.host_epoch,
            version=room.version,
            status=room.status.value,
            maxPlayers=room.max_players,
            currentTurnPlayerId=room.current_turn_player_id,
            settings=room.settings,
            players=players,
            game=game_payload,
            createdAt=room.created_at,
            updatedAt=room.updated_at,
            lastConnectedAt=room.last_connected_at,
            allDisconnectedAt=room.all_disconnected_at,
            expiresAt=room.expires_at,
            finishedAt=room.finished_at,
        )

    def require_host(self, room: Room, player_id: str) -> None:
        if room.host_player_id != player_id:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Only the host can perform this action.")

    def get_player(self, room: Room, player_id: str) -> Player:
        for player in room.players:
            if player.player_id == player_id:
                return player
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail={"code": "invalid_player_session"})

    def authenticate_player(self, room: Room, player_id: str, session_token: str) -> Player:
        try:
            player = self.get_player(room, player_id)
        except HTTPException:
            removed_hash = room.removed_player_sessions.get(player_id)
            if removed_hash is not None and compare_digest(
                removed_hash, self._hash_session_token(session_token)
            ):
                raise HTTPException(
                    status_code=status.HTTP_410_GONE,
                    detail={"code": "player_removed"},
                )
            raise
        if not compare_digest(
            player.session_token_hash,
            self._hash_session_token(session_token),
        ):
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail={"code": "invalid_player_session"})
        return player

    def authenticate_session(self, room: Room, session_token: str) -> Player:
        token_hash = self._hash_session_token(session_token)
        for player in room.players:
            if compare_digest(player.session_token_hash, token_hash):
                return player
        if any(
            compare_digest(removed_hash, token_hash)
            for removed_hash in room.removed_player_sessions.values()
        ):
            raise HTTPException(
                status_code=status.HTTP_410_GONE,
                detail={"code": "player_removed"},
            )
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail={"code": "invalid_player_session"})

    @staticmethod
    def normalize_room_code(room_code: str) -> str:
        normalized = room_code.strip().upper()
        if not normalized or len(normalized) > 6:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail={"code": "invalid_room_code"})
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
    def _assign_host(room: Room, player: Player) -> None:
        if room.host_player_id == player.player_id and player.is_host:
            return
        room.host_player_id = player.player_id
        room.host_epoch += 1
        for candidate in room.players:
            candidate.is_host = candidate.player_id == player.player_id

    @staticmethod
    def _remove_player_from_game(room: Room, player_id: str) -> None:
        if room.status != RoomStatus.PLAYING or room.game is None:
            return

        if len(room.players) < 2:
            room.status = RoomStatus.FINISHED
            room.game.pending_round = None
            room.game.last_pass = None
            return

        pending_round = room.game.pending_round
        if pending_round is None:
            return

        if pending_round.player_id == player_id:
            room.game.pending_round = None
            room.game.last_pass = None
            return

        pending_round.votes = [
            vote for vote in pending_round.votes if vote.player_id != player_id
        ]

    def _delete_room(self, room: Room, *, reason: str) -> None:
        room_code = room.room_code
        self._store.delete(room_code)
        self._closed_rooms[room_code] = (reason, time.monotonic())
        self._idempotency_records = {
            key: record
            for key, record in self._idempotency_records.items()
            if record[2] != room_code
        }
        for listener in tuple(self._room_deleted_listeners):
            listener(room_code)
        for observer in tuple(self._room_deletion_observers):
            observer(room_code, reason, room.status)

    def _evict_expired_lobby_player(self, room: Room) -> None:
        if room.status != RoomStatus.WAITING:
            return
        candidate = next(
            (
                player
                for player in room.players
                if not player.is_connected and player.voting_excluded
            ),
            None,
        )
        if candidate is None:
            return
        room.removed_player_sessions[candidate.player_id] = candidate.session_token_hash
        room.players = [
            player for player in room.players if player.player_id != candidate.player_id
        ]
        room.scores.pop(candidate.player_id, None)
        if room.host_player_id == candidate.player_id and room.players:
            connected = [player for player in room.players if player.is_connected]
            self._assign_host(room, (connected or room.players)[0])
        room.current_turn_player_id = self._resolve_current_turn(room)
        self._refresh_lifecycle(room)
        self._touch(room)
        self._store.save(room)

    def _refresh_lifecycle(self, room: Room) -> None:
        now = self._now()
        if room.status == RoomStatus.FINISHED:
            if room.finished_at is None:
                room.finished_at = now
        else:
            room.finished_at = None
        if any(player.is_connected for player in room.players):
            room.all_disconnected_at = None
            room.expires_at = None
            return
        if room.all_disconnected_at is None:
            room.all_disconnected_at = now
        room.expires_at = room.all_disconnected_at + timedelta(
            seconds=self._room_ttl_seconds[room.status]
        )

    def _prune_closed_rooms(self) -> None:
        now = time.monotonic()
        self._closed_rooms = {
            room_code: record
            for room_code, record in self._closed_rooms.items()
            if now - record[1] <= self._idempotency_ttl_seconds
        }

    @staticmethod
    def _touch(room: Room) -> None:
        room.updated_at = RoomService._now()

    @staticmethod
    def _now() -> datetime:
        return datetime.now(timezone.utc)

    @staticmethod
    def _generate_session_token() -> str:
        return secrets.token_urlsafe(32)

    @staticmethod
    def _hash_session_token(session_token: str) -> str:
        return hashlib.sha256(session_token.encode("utf-8")).hexdigest()

    def _idempotency_replay(
        self, key: str, operation: str, fingerprint: str
    ) -> tuple[str, str, str] | None:
        now = time.monotonic()
        self._idempotency_records = {
            existing_key: record
            for existing_key, record in self._idempotency_records.items()
            if now - record[5] <= self._idempotency_ttl_seconds
        }
        record = self._idempotency_records.get(key)
        if record is None:
            return None
        if record[0] != operation or record[1] != fingerprint:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={"code": "idempotency_key_reused"},
            )
        return record[2], record[3], record[4]

    def _store_idempotency(
        self,
        key: str,
        operation: str,
        fingerprint: str,
        room_code: str,
        player_id: str,
        session_token: str,
    ) -> None:
        self._idempotency_records[key] = (
            operation,
            fingerprint,
            room_code,
            player_id,
            session_token,
            time.monotonic(),
        )
