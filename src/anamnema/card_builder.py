"""Rebuild cards from current linked evidence and never commit stale model output."""

import logging
from datetime import UTC, datetime
from typing import Any

from anamnema.card_graph import CardGraph
from anamnema.card_rating import CardRanker
from anamnema.card_writer import CardWriter
from anamnema.memory_rating import NoteRating, effective_trust
from anamnema.setup_config import SetupError

LOGGER = logging.getLogger(__name__)


class CardBuilder:
    """A rebuild failure leaves a visible pending revision, not a previously generated card."""

    def __init__(self, graph: CardGraph, writer: CardWriter) -> None:
        self.graph, self.writer = graph, writer

    def rebuild(self, instance: str, name: str) -> dict[str, Any]:
        """Select and rate source-backed facts, then compare the input revision at commit."""
        inputs = self.graph.inputs(instance, name)
        held = [note["id"] for note in inputs.notes if not self._eligible(note)]
        eligible = [note for note in inputs.notes if self._eligible(note)]
        ranker = CardRanker(self.graph.policy, self.graph.store.settings.rating, datetime.now(UTC))
        chosen = ranker.pick(name, eligible)
        card = self.writer.write(inputs.entity, chosen, inputs.moves)
        ranked = ranker.order(card.statements, chosen, inputs.moves)
        self.graph.commit(
            instance,
            name,
            int(inputs.entity["revision"]),
            ranked.statements,
            card.model,
            tuple(str(move["id"]) for move in ranked.unsettled),
        )
        problems = [*card.problems]
        problems.extend(
            "manual move needs settlement: " + str(move["id"]) for move in ranked.unsettled
        )
        return {
            "entity": name,
            "status": "success",
            "statements": len(ranked.statements),
            "held_back": held,
            "problems": problems,
            "model": card.model,
        }

    def _eligible(self, note: dict[str, Any]) -> bool:
        rating = NoteRating(
            float(note["trust_positive"]),
            float(note["trust_negative"]),
            bool(note["wrong_open"]),
            str(note["status"]),
            int(note["useful"]),
            int(note["not_useful"]),
            None,
        )
        settings = self.graph.store.settings.rating
        return not rating.wrong_open and effective_trust(rating, settings) >= settings.floor

    def refresh_for_check(self, instance: str, check: str) -> list[dict[str, Any]]:
        """Retry also runs for replayed checks, without counting the correction twice."""
        results: list[dict[str, Any]] = []
        for name in self.graph.affected(instance, check):
            entity = self.graph.entity(instance, name)
            if entity["status"] == "dropped":
                results.append({"entity": name, "status": "success", "skipped": "dropped"})
                continue
            if entity.get("card_valid") and entity.get("card_built_revision") == entity["revision"]:
                results.append({"entity": name, "status": "success", "rebuilt": False})
                continue
            try:
                results.append(self.rebuild(instance, name))
            except SetupError as exc:
                LOGGER.warning("card rebuild pending after correction: %s", exc)
                results.append(
                    {
                        "entity": name,
                        "status": "failed",
                        "rebuild_pending": True,
                        "reason": str(exc),
                    }
                )
        return results
