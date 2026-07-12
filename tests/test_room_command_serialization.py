import asyncio
import unittest

from fastapi import HTTPException

from app.core.config import Settings
from app.models.game import GameCard, GameState, LobbySettings, RoundResult
from app.models.player import Player
from app.models.room import Room, RoomStatus
from app.routers.websocket import _handle_event, run_serialized_room_command
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
            Player(playerId="p1", name="Host", isHost=True, isReady=True),
            Player(playerId="p2", name="Guest", isReady=True),
        ],
        maxPlayers=4,
        settings=LobbySettings(),
    )
    room_service.save(room)
    return room_service, room


class RoomCommandSerializationTest(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
