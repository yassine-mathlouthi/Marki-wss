from __future__ import annotations

import json
import random
from pathlib import Path

import httpx
from fastapi import HTTPException, status

from app.core.config import Settings
from app.models.game import GameCard, PendingRound, RoundResult, Vote, VoteChoice, WrongAnswerBehavior
from app.models.room import Room, RoomStatus


class GameService:
    def __init__(self, settings: Settings, randomizer: random.Random | None = None) -> None:
        self._settings = settings
        self._random = randomizer or random.Random()
        self._fallback_cards = self._load_fallback_cards()

    async def load_cards(self, region_id: str) -> list[GameCard]:
        if self._settings.cards_api_base_url:
            remote_cards = await self._load_remote_cards(region_id)
            if remote_cards:
                return remote_cards
        return [card.model_copy(deep=True) for card in self._fallback_cards if card.region_id == region_id]

    async def start_game(self, room: Room) -> Room:
        cards = await self.load_cards(room.settings.region_id)
        if len(cards) < 2:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Not enough cards are available for this region.",
            )
        required_count = (len(room.players) * room.settings.cards_per_player) + 2 + (len(room.players) * 3)
        deck = self._ensure_deck_size(cards, required_count)
        self._random.shuffle(deck)

        cursor = 0
        for player in room.players:
            player.hand = [card.model_copy(deep=True) for card in deck[cursor : cursor + room.settings.cards_per_player]]
            cursor += room.settings.cards_per_player

        table_cards = [card.model_copy(deep=True) for card in deck[cursor : cursor + 2]]
        cursor += 2

        room.game.draw_pool = [card.model_copy(deep=True) for card in cards]
        room.game.deck = [card.model_copy(deep=True) for card in deck[cursor:]]
        room.game.table_cards = table_cards
        room.game.discard_pile = []
        room.game.pending_round = None
        room.game.last_round = None
        room.current_turn_player_id = room.players[0].player_id if room.players else None
        room.status = RoomStatus.PLAYING
        for player in room.players:
            room.scores[player.player_id] = 0
        return room

    def submit_answer(
        self,
        room: Room,
        player_id: str,
        card_id: str,
        related_card_id: str,
        answer: str,
    ) -> Room:
        player = next(player for player in room.players if player.player_id == player_id)
        played_card = next(card for card in player.hand if card.id == card_id)
        related_card = next(card for card in room.game.table_cards if card.id == related_card_id)
        player.hand = [card for card in player.hand if card.id != card_id]
        pair_key = self._pair_key(played_card.id, related_card.id)
        room.game.pending_round = PendingRound(
            playerId=player_id,
            playedCard=played_card,
            relatedCard=related_card,
            answerText=answer.strip(),
            previousAcceptedAnswer=room.game.accepted_answers.get(pair_key),
            votes=[],
        )
        room.game.last_round = None
        return room

    def cast_vote(self, room: Room, player_id: str, choice: VoteChoice) -> tuple[Room, RoundResult | None]:
        pending_round = room.game.pending_round
        if pending_round is None:
            raise ValueError("No pending round.")

        votes = [vote for vote in pending_round.votes if vote.player_id != player_id]
        votes.append(Vote(playerId=player_id, choice=choice))
        pending_round.votes = votes

        expected_voters = [player.player_id for player in room.players if player.player_id != pending_round.player_id]
        if sorted(vote.player_id for vote in pending_round.votes) != sorted(expected_voters):
            return room, None

        correct_votes = len([vote for vote in pending_round.votes if vote.choice == VoteChoice.CORRECT])
        wrong_votes = len([vote for vote in pending_round.votes if vote.choice == VoteChoice.WRONG])
        accepted = correct_votes > wrong_votes
        result = RoundResult(
            playerId=pending_round.player_id,
            playedCard=pending_round.played_card,
            relatedCard=pending_round.related_card,
            answerText=pending_round.answer_text,
            votes=pending_round.votes,
            accepted=accepted,
            correctVotes=correct_votes,
            wrongVotes=wrong_votes,
            previousAcceptedAnswer=pending_round.previous_accepted_answer,
        )

        round_player = next(player for player in room.players if player.player_id == pending_round.player_id)
        if accepted:
            room.scores[round_player.player_id] = room.scores.get(round_player.player_id, 0) + 1
            room.game.table_cards = [
                card for card in room.game.table_cards if card.id != pending_round.related_card.id
            ]
            room.game.table_cards.append(pending_round.played_card.model_copy(deep=True))
            room.game.discard_pile.append(pending_round.related_card.model_copy(deep=True))
            room.game.accepted_answers[self._pair_key(pending_round.played_card.id, pending_round.related_card.id)] = (
                pending_round.answer_text
            )
        else:
            if room.settings.wrong_answer_behavior == WrongAnswerBehavior.RETURN_TO_HAND:
                round_player.hand.append(pending_round.played_card.model_copy(deep=True))
            else:
                drawn_cards = self._draw_unlimited_cards(room, 2)
                round_player.hand.extend(drawn_cards)
                room.game.discard_pile.append(pending_round.played_card.model_copy(deep=True))

        room.game.last_round = result
        room.game.pending_round = None
        self._advance_turn(room)
        if any(not player.hand for player in room.players):
            room.status = RoomStatus.FINISHED
        return room, result

    def _advance_turn(self, room: Room) -> None:
        player_ids = [player.player_id for player in room.players]
        if not player_ids:
            room.current_turn_player_id = None
            return
        current = room.current_turn_player_id
        if current not in player_ids:
            room.current_turn_player_id = player_ids[0]
            return
        next_index = (player_ids.index(current) + 1) % len(player_ids)
        room.current_turn_player_id = player_ids[next_index]

    def _draw_unlimited_cards(self, room: Room, count: int) -> list[GameCard]:
        if room.game.draw_pool:
            return [
                room.game.draw_pool[self._random.randrange(len(room.game.draw_pool))].model_copy(deep=True)
                for _ in range(count)
            ]

        drawn = [card.model_copy(deep=True) for card in room.game.deck[:count]]
        room.game.deck = room.game.deck[count:]
        return drawn

    def _ensure_deck_size(self, source_cards: list[GameCard], required_count: int) -> list[GameCard]:
        if len(source_cards) >= required_count:
            return [card.model_copy(deep=True) for card in source_cards]

        expanded: list[GameCard] = []
        copy_index = 0
        while len(expanded) < required_count:
            for card in source_cards:
                expanded.append(
                    card.model_copy(
                        update={"id": f"{card.id}_{copy_index}"},
                        deep=True,
                    )
                )
                if len(expanded) == required_count:
                    break
            copy_index += 1
        return expanded

    async def _load_remote_cards(self, region_id: str) -> list[GameCard]:
        base_url = self._settings.cards_api_base_url
        assert base_url is not None
        url = f"{base_url.rstrip('/')}/api/v1/cards/"
        async with httpx.AsyncClient(timeout=self._settings.cards_api_timeout) as client:
            response = await client.get(url, params={"regionId": region_id, "limit": 200})
            response.raise_for_status()
            payload = response.json()
        if not isinstance(payload, list):
            return []
        return [GameCard.model_validate(item) for item in payload]

    def _load_fallback_cards(self) -> list[GameCard]:
        root = Path(__file__).resolve().parents[3]
        candidate_files = [
            root / "frontEnd" / "assets" / "data" / "cards" / "tunisia_clubs.json",
            root / "frontEnd" / "assets" / "data" / "cards" / "common_numbers.json",
        ]
        cards: list[GameCard] = []
        for path in candidate_files:
            if not path.exists():
                continue
            data = json.loads(path.read_text(encoding="utf-8"))
            cards.extend(GameCard.model_validate(item) for item in data)
        return cards

    @staticmethod
    def _pair_key(card_a_id: str, card_b_id: str) -> str:
        parts = sorted([GameService._canonical_card_id(card_a_id), GameService._canonical_card_id(card_b_id)])
        return "::".join(parts)

    @staticmethod
    def _canonical_card_id(card_id: str) -> str:
        if "_" not in card_id:
            return card_id
        prefix, suffix = card_id.rsplit("_", 1)
        return prefix if suffix.isdigit() else card_id
