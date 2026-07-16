import asyncio
import json
import unittest

from fastapi import HTTPException

from app.core.config import Settings
from app.models.game import (
    GameCard,
    GameState,
    LobbySettings,
    PassResult,
    PendingRound,
    RoundResult,
)
from app.models.player import Player
from app.models.room import Room, RoomStatus
from app.routers.websocket import (
    _handle_event,
    _send_command_result,
    run_serialized_room_command,
)
from app.services.game_service import GameService
from app.services.in_memory import InMemoryRoomStore
from app.services.room_service import RoomService


def stored_room() -> tuple[RoomService, Room]:
    store = InMemoryRoomStore()
    room_service = RoomService(store)
    room = Room(
        roomCode="ABC123",
        hostPlayerId="p1",
        players=[
            Player(
                playerId="p1",
                name="Host",
                isHost=True,
                isReady=True,
                isConnected=True,
            ),
            Player(playerId="p2", name="Guest", isReady=True, isConnected=True),
        ],
        maxPlayers=4,
        settings=LobbySettings(),
        game=GameState(),
    )
    room_service.save(room)
    return room_service, room


def card(card_id: str) -> GameCard:
    return GameCard(
        id=card_id,
        type="club",
        regionId="world",
        packId="pack",
        names={"en": card_id},
    )


class RecordingConnectionManager:
    def __init__(self) -> None:
        self.messages: list[tuple[str, dict]] = []

    async def send_to_player(self, _room_code, player_id, event) -> bool:
        self.messages.append(
            (player_id, event.model_dump(mode="json", by_alias=True))
        )
        return True


class DeterministicGameService(GameService):
    async def start_game(self, room):
        room.status = RoomStatus.PLAYING
        room.current_turn_player_id = room.players[0].player_id
        room.game.table_cards = [card("table-1"), card("table-2")]
        for index, player in enumerate(room.players):
            player.hand = [card(f"hand-{index}")]
        return room


class FailingGameService(GameService):
    async def start_game(self, room):
        raise RuntimeError("card loading failed")


class RoomCommandSerializationTest(unittest.TestCase):
    def test_normal_commands_send_one_snapshot_per_player_then_ack(self) -> None:
        commands = [
            "set_ready",
            "update_lobby_settings",
            "replay_game",
            "submit_answer",
            "pass_turn",
            "continue_pass_result",
            "cast_vote",
            "continue_round_result",
        ]

        for command_type in commands:
            with self.subTest(command_type=command_type):
                asyncio.run(self._assert_normal_command_messages(command_type))

    def test_start_game_preserves_starting_and_playing_transitions(self) -> None:
        async def scenario() -> None:
            room_service, room = stored_room()
            manager = RecordingConnectionManager()
            await _handle_event(
                websocket=None,
                room=room,
                player_id="p1",
                event_type="start_game",
                payload={},
                room_service=room_service,
                connection_manager=manager,
                game_service=DeterministicGameService(Settings()),
                cancel_grace=lambda *_: None,
                mark_disconnected=lambda *_: None,
                correlation_id="start-1",
            )
            await _send_command_result(
                manager,
                room_service,
                room,
                "p1",
                "start-1",
                "start_game",
                replayed=False,
            )

            actor_messages = [event for target, event in manager.messages if target == "p1"]
            self.assertEqual(
                [event["type"] for event in actor_messages],
                ["game_starting", "game_snapshot", "command_result"],
            )
            self.assertEqual(
                [event["payload"]["snapshot"]["status"] for event in actor_messages[:2]],
                ["starting", "playing"],
            )
            self.assertNotIn("snapshot", actor_messages[-1]["payload"])
            snapshot_messages = [
                event for _, event in manager.messages if "snapshot" in event["payload"]
            ]
            self.assertEqual(len(snapshot_messages), len(room.players) * 2)

        asyncio.run(scenario())

    def test_failed_start_broadcasts_waiting_rollback(self) -> None:
        async def scenario() -> None:
            room_service, room = stored_room()
            manager = RecordingConnectionManager()

            with self.assertRaisesRegex(RuntimeError, "card loading failed"):
                await _handle_event(
                    websocket=None,
                    room=room,
                    player_id="p1",
                    event_type="start_game",
                    payload={},
                    room_service=room_service,
                    connection_manager=manager,
                    game_service=FailingGameService(Settings()),
                    cancel_grace=lambda *_: None,
                    mark_disconnected=lambda *_: None,
                    correlation_id="start-failure",
                )

            self.assertEqual(room.status, RoomStatus.WAITING)
            actor_messages = [
                event for target, event in manager.messages if target == "p1"
            ]
            self.assertEqual(
                [event["type"] for event in actor_messages],
                ["game_starting", "room_snapshot"],
            )
            self.assertEqual(
                [event["payload"]["snapshot"]["status"] for event in actor_messages],
                ["starting", "waiting"],
            )

        asyncio.run(scenario())

    def test_replayed_command_gets_current_snapshot_without_broadcast(self) -> None:
        async def scenario() -> None:
            room_service, room = stored_room()
            manager = RecordingConnectionManager()

            await _send_command_result(
                manager,
                room_service,
                room,
                "p1",
                "ready-1",
                "set_ready",
                replayed=True,
            )

            self.assertEqual(len(manager.messages), 1)
            event = manager.messages[0][1]
            self.assertEqual(event["type"], "command_result")
            self.assertTrue(event["payload"]["replayed"])
            self.assertEqual(event["payload"]["snapshot"]["version"], room.version)

        asyncio.run(scenario())

    def test_resolving_vote_sends_one_round_snapshot_per_player_then_ack(self) -> None:
        async def scenario() -> None:
            room_service, room = stored_room()
            room.status = RoomStatus.PLAYING
            room.players[0].hand = [card("held")]
            room.players[1].hand = [card("guest")]
            room.game.table_cards = [card("table")]
            room.game.pending_round = PendingRound(
                playerId="p1",
                playedCard=card("played"),
                relatedCard=card("table"),
                answerText="answer",
            )
            room_service.save(room)
            manager = RecordingConnectionManager()

            await _handle_event(
                websocket=None,
                room=room,
                player_id="p2",
                event_type="cast_vote",
                payload={"choice": "correct"},
                room_service=room_service,
                connection_manager=manager,
                game_service=GameService(Settings()),
                cancel_grace=lambda *_: None,
                mark_disconnected=lambda *_: None,
                correlation_id="vote-resolve-1",
            )
            await _send_command_result(
                manager,
                room_service,
                room,
                "p2",
                "vote-resolve-1",
                "cast_vote",
                replayed=False,
            )

            snapshot_messages = [
                (target, event)
                for target, event in manager.messages
                if "snapshot" in event["payload"]
            ]
            self.assertEqual(len(snapshot_messages), len(room.players))
            self.assertTrue(
                all(event["type"] == "round_resolved" for _, event in snapshot_messages)
            )
            actor_messages = [
                event for target, event in manager.messages if target == "p2"
            ]
            self.assertEqual(
                [event["type"] for event in actor_messages],
                ["round_resolved", "command_result"],
            )
            self.assertNotIn("snapshot", actor_messages[-1]["payload"])

        asyncio.run(scenario())

    def test_leave_ack_has_no_snapshot_and_only_peers_receive_state(self) -> None:
        async def scenario() -> None:
            room_service, room = stored_room()
            manager = RecordingConnectionManager()

            await _handle_event(
                websocket=None,
                room=room,
                player_id="p1",
                event_type="leave_room",
                payload={},
                room_service=room_service,
                connection_manager=manager,
                game_service=GameService(Settings()),
                cancel_grace=lambda *_: None,
                mark_disconnected=lambda *_: None,
                correlation_id="leave-1",
            )
            await _send_command_result(
                manager,
                room_service,
                room,
                "p1",
                "leave-1",
                "leave_room",
                replayed=False,
            )

            snapshot_targets = [
                target
                for target, event in manager.messages
                if "snapshot" in event["payload"]
            ]
            self.assertEqual(snapshot_targets, ["p2"])
            ack = manager.messages[-1][1]
            self.assertEqual(ack["type"], "command_result")
            self.assertNotIn("snapshot", ack["payload"])

        asyncio.run(scenario())

    def test_maximum_initial_snapshot_fits_frame_limit_and_is_private(self) -> None:
        room_service = RoomService(InMemoryRoomStore())
        players = [
            Player(
                playerId=f"p{index}",
                name=f"Player {index}",
                isHost=index == 0,
                hand=[card(f"p{index}-card-{card_index}") for card_index in range(14)],
            )
            for index in range(8)
        ]
        room = Room(
            roomCode="ABC123",
            hostPlayerId="p0",
            players=players,
            status=RoomStatus.PLAYING,
            maxPlayers=8,
            currentTurnPlayerId="p0",
            scores={player.player_id: 0 for player in players},
            settings=LobbySettings(cardsPerPlayer=14),
            game=GameState(
                tableCards=[card("table-1"), card("table-2")],
                deck=[card(f"deck-{index}") for index in range(24)],
                drawPool=[card(f"draw-{index}") for index in range(10)],
            ),
        )
        room_service.save(room)

        payload_sizes = []
        for viewer in players:
            snapshot_model = room_service.build_snapshot(room, viewer.player_id)
            snapshot = snapshot_model.model_dump(mode="json", by_alias=True)
            payload_sizes.append(
                len(json.dumps(snapshot, separators=(",", ":")).encode("utf-8"))
            )
            visible_hands = [
                player.hand
                for player in snapshot_model.players
                if player.hand is not None
            ]
            self.assertEqual(len(visible_hands), 1)
            self.assertEqual(len(visible_hands[0]), 14)

        self.assertLess(max(payload_sizes), Settings().ws_max_frame_bytes)

    def test_only_round_owner_can_continue_result(self) -> None:
        async def scenario() -> None:
            room_service, room = stored_room()
            room.status = RoomStatus.PLAYING
            room.game = GameState()
            room.game.last_round = RoundResult(
                playerId="p1",
                playedCard=GameCard(id="played", type="club", regionId="r", packId="p", names={"en": "played"}),
                relatedCard=GameCard(id="related", type="club", regionId="r", packId="p", names={"en": "related"}),
                answerText="answer",
                accepted=True,
                correctVotes=1,
                wrongVotes=0,
            )
            room_service.save(room)

            class ConnectionManagerStub:
                async def send_to_player(self, *_args, **_kwargs) -> bool:
                    return True

            with self.assertRaises(HTTPException) as raised:
                await _handle_event(
                    websocket=None,
                    room=room,
                    player_id="p2",
                    event_type="continue_round_result",
                    payload={},
                    room_service=room_service,
                    connection_manager=ConnectionManagerStub(),
                    game_service=GameService(Settings()),
                    cancel_grace=lambda *_: None,
                    mark_disconnected=lambda *_: None,
                    correlation_id="continue-result",
                )

            self.assertEqual(raised.exception.status_code, 403)
            self.assertIsNotNone(room.game.last_round)

        asyncio.run(scenario())
    def test_duplicate_start_executes_once(self) -> None:
        asyncio.run(self._assert_duplicate_executes_once("start-command"))

    def test_duplicate_replay_executes_once(self) -> None:
        asyncio.run(self._assert_duplicate_executes_once("replay-command"))

    def test_duplicate_leave_executes_once(self) -> None:
        asyncio.run(self._assert_duplicate_executes_once("leave-command"))

    def test_simultaneous_votes_are_serialized_without_loss(self) -> None:
        async def scenario() -> None:
            room_service, room = stored_room()
            lock = asyncio.Lock()
            active = 0
            max_active = 0
            applied: list[str] = []

            async def vote(command_id: str) -> None:
                nonlocal active, max_active

                async def apply_vote(_room) -> None:
                    nonlocal active, max_active
                    active += 1
                    max_active = max(max_active, active)
                    await asyncio.sleep(0)
                    applied.append(command_id)
                    active -= 1

                await run_serialized_room_command(
                    room.room_code,
                    command_id,
                    room_service,
                    lock,
                    apply_vote,
                )

            await asyncio.gather(vote("vote-1"), vote("vote-2"))
            self.assertEqual(applied, ["vote-1", "vote-2"])
            self.assertEqual(max_active, 1)

        asyncio.run(scenario())

    def test_starting_state_is_saved_before_card_loading_finishes(self) -> None:
        async def scenario() -> None:
            room_service, room = stored_room()
            entered = asyncio.Event()
            release = asyncio.Event()

            class DelayedGameService(GameService):
                async def start_game(self, room):
                    self.assert_starting(room)
                    entered.set()
                    await release.wait()
                    room.status = RoomStatus.PLAYING
                    return room

                @staticmethod
                def assert_starting(room) -> None:
                    if room.status != RoomStatus.STARTING:
                        raise AssertionError("Room was not marked starting")

            class ConnectionManagerStub:
                async def send_to_player(self, *_args, **_kwargs) -> bool:
                    return True

            service = DelayedGameService(Settings())
            task = asyncio.create_task(
                _handle_event(
                    websocket=None,
                    room=room,
                    player_id="p1",
                    event_type="start_game",
                    payload={},
                    room_service=room_service,
                    connection_manager=ConnectionManagerStub(),
                    game_service=service,
                    cancel_grace=lambda *_: None,
                    mark_disconnected=lambda *_: None,
                    correlation_id="start-transition",
                )
            )
            await entered.wait()
            self.assertEqual(
                room_service.get_room(room.room_code).status,
                RoomStatus.STARTING,
            )
            release.set()
            await task
            self.assertEqual(room.status, RoomStatus.PLAYING)
            self.assertGreaterEqual(room.version, 3)

        asyncio.run(scenario())

    async def _assert_duplicate_executes_once(self, command_id: str) -> None:
        room_service, room = stored_room()
        lock = asyncio.Lock()
        calls = 0

        async def handler(_room) -> None:
            nonlocal calls
            calls += 1
            await asyncio.sleep(0)

        results = await asyncio.gather(
            run_serialized_room_command(
                room.room_code, command_id, room_service, lock, handler
            ),
            run_serialized_room_command(
                room.room_code, command_id, room_service, lock, handler
            ),
        )

        self.assertEqual(calls, 1)
        self.assertEqual([executed for executed, _ in results], [True, False])

    async def _assert_normal_command_messages(self, command_type: str) -> None:
        room_service, room = stored_room()
        manager = RecordingConnectionManager()
        game_service = GameService(Settings())
        actor_id = "p1"
        payload: dict = {}
        expected_event = "game_snapshot"

        if command_type == "set_ready":
            payload = {"ready": False}
            expected_event = "room_snapshot"
        elif command_type == "update_lobby_settings":
            payload = {"language": "fr"}
            expected_event = "room_snapshot"
        elif command_type == "replay_game":
            room.status = RoomStatus.FINISHED
            expected_event = "room_snapshot"
        elif command_type == "submit_answer":
            room.status = RoomStatus.PLAYING
            room.current_turn_player_id = "p1"
            room.players[0].hand = [card("held")]
            room.game.table_cards = [card("table")]
            payload = {
                "cardId": "held",
                "relatedCardId": "table",
                "answer": "answer",
            }
        elif command_type == "pass_turn":
            room.status = RoomStatus.PLAYING
            room.current_turn_player_id = "p1"
            room.game.draw_pool = [card("draw")]
        elif command_type == "continue_pass_result":
            room.status = RoomStatus.PLAYING
            room.current_turn_player_id = "p1"
            room.game.last_pass = PassResult(
                playerId="p1", playerName="Host", drawnCards=[card("draw")]
            )
        elif command_type == "cast_vote":
            actor_id = "p2"
            room.status = RoomStatus.PLAYING
            room.players.append(Player(playerId="p3", name="Third"))
            room.scores["p3"] = 0
            room.game.draw_pool = [card("draw")]
            room.game.table_cards = [card("table")]
            room.game.pending_round = PendingRound(
                playerId="p1",
                playedCard=card("played"),
                relatedCard=card("table"),
                answerText="answer",
            )
            payload = {"choice": "correct"}
        elif command_type == "continue_round_result":
            room.status = RoomStatus.PLAYING
            room.game.last_round = RoundResult(
                playerId="p1",
                playedCard=card("played"),
                relatedCard=card("table"),
                answerText="answer",
                accepted=True,
                correctVotes=1,
                wrongVotes=0,
            )

        room_service.save(room)
        await _handle_event(
            websocket=None,
            room=room,
            player_id=actor_id,
            event_type=command_type,
            payload=payload,
            room_service=room_service,
            connection_manager=manager,
            game_service=game_service,
            cancel_grace=lambda *_: None,
            mark_disconnected=lambda *_: None,
            correlation_id=f"{command_type}-1",
        )
        await _send_command_result(
            manager,
            room_service,
            room,
            actor_id,
            f"{command_type}-1",
            command_type,
            replayed=False,
        )

        snapshot_messages = [
            (target, event)
            for target, event in manager.messages
            if "snapshot" in event["payload"]
        ]
        self.assertEqual(len(snapshot_messages), len(room.players))
        self.assertEqual(
            {target for target, _ in snapshot_messages},
            {player.player_id for player in room.players},
        )
        actor_messages = [
            event for target, event in manager.messages if target == actor_id
        ]
        self.assertEqual(
            [event["type"] for event in actor_messages],
            [expected_event, "command_result"],
        )
        ack = actor_messages[-1]["payload"]
        self.assertEqual(ack["commandId"], f"{command_type}-1")
        self.assertEqual(ack["commandType"], command_type)
        self.assertEqual(ack["status"], "applied")
        self.assertFalse(ack["replayed"])
        self.assertNotIn("snapshot", ack)


if __name__ == "__main__":
    unittest.main()
