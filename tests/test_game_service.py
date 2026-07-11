import asyncio
import unittest

from fastapi import HTTPException
from pydantic import ValidationError

from app.core.config import Settings
from app.models.game import GameCard, GameState, LobbySettings, PendingRound, Vote, VoteChoice, WrongAnswerBehavior
from app.models.player import Player
from app.models.room import Room, RoomStatus
from app.services.game_service import GameService
from app.services.in_memory import InMemoryRoomStore
from app.services.room_service import RoomService


def card(card_id: str) -> GameCard:
    return GameCard(
        id=card_id,
        type="club",
        regionId="tunisia",
        packId="pack",
        names={"en": card_id},
    )


def room_with_round(wrong_answer_behavior: WrongAnswerBehavior) -> Room:
    player_one = Player(playerId="p1", name="A", hand=[card("held")])
    player_two = Player(playerId="p2", name="B", hand=[card("b1")])
    played_card = card("played")
    related_card = card("related")
    return Room(
        roomCode="ABC123",
        hostPlayerId="p1",
        players=[player_one, player_two],
        status=RoomStatus.PLAYING,
        maxPlayers=4,
        currentTurnPlayerId="p1",
        scores={"p1": 0, "p2": 0},
        settings=LobbySettings(wrongAnswerBehavior=wrong_answer_behavior),
        game=GameState(
            drawPool=[card("draw1"), card("draw2"), card("draw3")],
            tableCards=[related_card, card("other")],
            pendingRound=PendingRound(
                playerId="p1",
                playedCard=played_card,
                relatedCard=related_card,
                answerText="answer",
                votes=[],
            ),
        ),
    )


class GameServiceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.service = GameService(Settings())

    def test_wrong_answer_discards_played_card_and_draws_two(self) -> None:
        room = room_with_round(WrongAnswerBehavior.DISCARD_CARD)

        room, result = self.service.cast_vote(room, "p2", VoteChoice.WRONG)

        player = next(player for player in room.players if player.player_id == "p1")
        self.assertIsNotNone(result)
        self.assertEqual(len(result.drawn_cards), 2)
        self.assertEqual(len(player.hand), 3)
        self.assertFalse(any(item.id == "played" for item in player.hand))
        self.assertEqual(room.current_turn_player_id, "p1")
        self.assertIsNotNone(room.game.last_round)

    def test_wrong_answer_can_return_played_card_and_draw_two(self) -> None:
        room = room_with_round(WrongAnswerBehavior.RETURN_TO_HAND)

        room, result = self.service.cast_vote(room, "p2", VoteChoice.WRONG)

        player = next(player for player in room.players if player.player_id == "p1")
        self.assertIsNotNone(result)
        self.assertEqual(len(result.drawn_cards), 2)
        self.assertEqual(len(player.hand), 4)
        self.assertTrue(any(item.id == "played" for item in player.hand))
        self.assertEqual(room.current_turn_player_id, "p1")

    def test_continue_round_result_advances_turn(self) -> None:
        room = room_with_round(WrongAnswerBehavior.DISCARD_CARD)
        room, _ = self.service.cast_vote(room, "p2", VoteChoice.WRONG)

        room = self.service.continue_round_result(room)

        self.assertIsNone(room.game.last_round)
        self.assertEqual(room.current_turn_player_id, "p2")

    def test_pass_turn_draws_one_and_advances_after_result(self) -> None:
        player_one = Player(playerId="p1", name="A", hand=[card("a1")])
        player_two = Player(playerId="p2", name="B", hand=[card("b1")])
        room = Room(
            roomCode="ABC123",
            hostPlayerId="p1",
            players=[player_one, player_two],
            status=RoomStatus.PLAYING,
            maxPlayers=4,
            currentTurnPlayerId="p1",
            scores={"p1": 0, "p2": 0},
            settings=LobbySettings(),
            game=GameState(drawPool=[card("draw1"), card("draw2")]),
        )

        room = self.service.pass_turn(room, "p1")

        self.assertIsNotNone(room.game.last_pass)
        self.assertEqual(len(room.game.last_pass.drawn_cards), 1)
        self.assertEqual(len(player_one.hand), 2)
        self.assertEqual(room.current_turn_player_id, "p1")

        room = self.service.continue_pass_result(room)

        self.assertIsNone(room.game.last_pass)
        self.assertEqual(room.current_turn_player_id, "p2")

    def test_pass_result_is_only_visible_to_passing_player(self) -> None:
        room_service = RoomService(InMemoryRoomStore())
        player_one = Player(playerId="p1", name="A", hand=[card("a1")])
        player_two = Player(playerId="p2", name="B", hand=[card("b1")])
        room = Room(
            roomCode="ABC123",
            hostPlayerId="p1",
            players=[player_one, player_two],
            status=RoomStatus.PLAYING,
            maxPlayers=4,
            currentTurnPlayerId="p1",
            scores={"p1": 0, "p2": 0},
            settings=LobbySettings(),
            game=GameState(drawPool=[card("draw1"), card("draw2")]),
        )
        room = self.service.pass_turn(room, "p1")

        passer_snapshot = room_service.build_snapshot(room, "p1")
        next_player_snapshot = room_service.build_snapshot(room, "p2")

        self.assertIsNotNone(passer_snapshot.game["lastPass"])
        self.assertIsNone(next_player_snapshot.game["lastPass"])
        self.assertEqual(next_player_snapshot.current_turn_player_id, "p1")

    def test_wrong_answer_draws_two_cards_in_player_snapshot(self) -> None:
        room_service = RoomService(InMemoryRoomStore())
        room = room_with_round(WrongAnswerBehavior.DISCARD_CARD)

        room, result = self.service.cast_vote(room, "p2", VoteChoice.WRONG)
        snapshot = room_service.build_snapshot(room, "p1")
        player = next(player for player in snapshot.players if player.player_id == "p1")

        self.assertIsNotNone(result)
        self.assertEqual(len(result.drawn_cards), 2)
        self.assertEqual(player.hand_count, 3)
        self.assertEqual(len(player.hand), 3)

    def test_submit_answer_rejects_missing_card_without_mutating_round(self) -> None:
        room = Room(
            roomCode="ABC123",
            hostPlayerId="p1",
            players=[Player(playerId="p1", name="A", hand=[card("a1")])],
            status=RoomStatus.PLAYING,
            maxPlayers=4,
            currentTurnPlayerId="p1",
            scores={"p1": 0},
            settings=LobbySettings(),
            game=GameState(tableCards=[card("table")]),
        )

        with self.assertRaisesRegex(ValueError, "Card is not in your hand"):
            self.service.submit_answer(room, "p1", "missing", "table", "answer")

        self.assertIsNone(room.game.pending_round)
        self.assertEqual(len(room.players[0].hand), 1)

    def test_leaving_pending_round_removes_stale_vote(self) -> None:
        room_service = RoomService(InMemoryRoomStore())
        room = room_with_round(WrongAnswerBehavior.DISCARD_CARD)
        room.players.append(Player(playerId="p3", name="C", hand=[card("c1")]))
        room.scores["p3"] = 0
        room.game.pending_round.votes = [Vote(playerId="p3", choice=VoteChoice.WRONG)]
        room_service.save(room)

        room = room_service.leave_room("ABC123", "p3")

        self.assertIsNotNone(room)
        self.assertEqual(room.status, RoomStatus.PLAYING)
        self.assertEqual(room.game.pending_round.votes, [])

    def test_leaving_pending_round_owner_clears_round(self) -> None:
        room_service = RoomService(InMemoryRoomStore())
        room = room_with_round(WrongAnswerBehavior.DISCARD_CARD)
        room_service.save(room)

        room = room_service.leave_room("ABC123", "p1")

        self.assertIsNotNone(room)
        self.assertEqual(room.status, RoomStatus.FINISHED)
        self.assertIsNone(room.game.pending_round)

    def test_pair_key_matches_copied_and_drawn_card_ids(self) -> None:
        key = self.service._pair_key("club_0_draw_123_0", "nation_2")

        self.assertEqual(key, "club::nation")

    def test_load_cards_does_not_mix_unrelated_fallback_regions(self) -> None:
        self.service._fallback_cards = [card("tunisia1"), card("tunisia2")]

        cards = asyncio.run(self.service.load_cards("europe"))

        self.assertEqual(cards, [])

    def test_reset_game_returns_finished_room_to_a_ready_lobby(self) -> None:
        room = room_with_round(WrongAnswerBehavior.DISCARD_CARD)
        room.status = RoomStatus.FINISHED
        room.players[0].is_ready = True
        room.players[1].is_ready = True
        room.scores = {"p1": 3, "p2": 1}
        room.game.accepted_answers["played::related"] = "answer"

        room = self.service.reset_game(room)

        self.assertEqual(room.status, RoomStatus.WAITING)
        self.assertEqual(room.current_turn_player_id, "p1")
        self.assertEqual(room.scores, {"p1": 0, "p2": 0})
        self.assertFalse(any(player.is_ready for player in room.players))
        self.assertTrue(all(not player.hand for player in room.players))
        self.assertEqual(room.game, GameState())

    def test_lobby_models_validate_assignment(self) -> None:
        room = room_with_round(WrongAnswerBehavior.DISCARD_CARD)

        with self.assertRaises(ValidationError):
            room.settings.cards_per_player = 1_000_000
        with self.assertRaises(ValidationError):
            room.max_players = 1_000_000

        self.assertEqual(room.settings.cards_per_player, 11)
        self.assertEqual(room.max_players, 4)

    def test_lobby_update_is_atomic_when_capacity_is_invalid(self) -> None:
        room_service = RoomService(InMemoryRoomStore())
        room = room_with_round(WrongAnswerBehavior.DISCARD_CARD)
        room.status = RoomStatus.WAITING
        room_service.save(room)

        with self.assertRaises(HTTPException):
            room_service.update_lobby_settings(
                room.room_code,
                room.host_player_id,
                region_id="world",
                max_players=1,
            )

        stored = room_service.get_room(room.room_code)
        self.assertEqual(stored.settings.region_id, "tunisia")
        self.assertEqual(stored.max_players, 4)

    def test_start_game_rejects_deck_allocation_above_hard_limit(self) -> None:
        room = room_with_round(WrongAnswerBehavior.DISCARD_CARD)
        object.__setattr__(room.settings, "cards_per_player", 1_000_000)

        with self.assertRaises(HTTPException) as raised:
            asyncio.run(self.service.start_game(room))

        self.assertEqual(raised.exception.status_code, 422)


if __name__ == "__main__":
    unittest.main()
