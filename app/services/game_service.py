from __future__ import annotations


class GameService:
    async def validate_answer(self, answer: str, card_id: str | None = None) -> bool:
        # TODO: Replace this placeholder with real football card/trivia validation logic.
        # This may later call a database, cache, or external trivia source.
        return bool(answer.strip())
