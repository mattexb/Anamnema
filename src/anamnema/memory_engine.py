"""Shared user-facing memory operations; substrate-specific code is not part of this core."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from anamnema.card_builder import CardBuilder
from anamnema.card_graph import CardGraph
from anamnema.card_policy import CardPolicy, card_policy
from anamnema.graph_corrections import correct, history
from anamnema.graph_store import GraphStore
from anamnema.lesson_builder import LessonBuilder
from anamnema.lesson_graph import LessonGraph
from anamnema.lesson_policy import LessonPolicy, lesson_policy
from anamnema.memory_models import Correction, Facet, MemoryInputError, Outcome
from anamnema.memory_sources import SourceLookup
from anamnema.retrieval import Retriever
from anamnema.search_services import CPUEncoder
from anamnema.setup_config import SetupError


class MemoryEngine:
    """The same core serves CLI and authenticated HTTP, without changing ownership semantics."""

    def __init__(
        self,
        store: GraphStore,
        sources: SourceLookup,
        encoder: CPUEncoder,
        retriever: Retriever,
        cards: CardBuilder | None = None,
        card_settings: CardPolicy | None = None,
        lessons: LessonBuilder | None = None,
        lesson_settings: LessonPolicy | None = None,
    ) -> None:
        if store.settings.dimensions != encoder.policy.dimensions:
            raise SetupError("graph and encoder dimensions differ")
        self.store, self.sources, self.encoder, self.retriever = store, sources, encoder, retriever
        self.cards = cards
        self.lessons = lessons
        self.lesson_settings = (
            lessons.writer.policy if lessons else lesson_settings or lesson_policy()
        )
        self.card_graph = cards.graph if cards else CardGraph(store, card_settings or card_policy())

    def search(
        self,
        instance: str,
        question: str,
        facets: tuple[Facet, ...] = (),
        at: datetime | None = None,
        page: int = 1,
    ) -> dict[str, Any]:
        """Use the actual question, never a model-rewritten search query."""
        if not question.strip() or len(question) > self.retriever.settings.max_question_characters:
            raise MemoryInputError("question violates configured retrieval limit")
        vector = self.encoder.encode([question], passage=False)[0]
        return self.retriever.recall(instance, question, vector, facets, at, page)

    def check(self, instance: str, check: Correction) -> dict[str, Any]:
        """Verify cited/replacement sources before mutation; never erase the archive."""
        for identities in (check.against, check.records):
            if identities:
                self.sources.records(instance, tuple(dict.fromkeys(identities)))
        previous = any(
            event["id"] == check.id for event in history(self.store, instance, check.note)
        )
        rewritten = None
        if not previous and check.outcome == Outcome.PARTLY:
            # Check ownership before the model call as well as inside the write transaction.
            self.store.note(instance, check.note)
            rewritten = self.encoder.embed_note(check.rewrite or "")
        applied = correct(self.store, instance, check, rewritten)
        result: dict[str, Any] = {"check": check.id, "note": check.note, "applied": applied}
        affected_lessons = LessonGraph(self.store).affected(instance, check.id)
        if self.lessons:
            result["lessons"] = self.lessons.refresh_for_check(instance, check.id)
        elif affected_lessons:
            result["lessons"] = [
                {
                    "lesson": identity,
                    "status": "pending",
                    "rebuild_pending": True,
                    "reason": "lesson model rebuilding is not configured",
                }
                for identity in affected_lessons
            ]
        if self.cards:
            result["cards"] = self.cards.refresh_for_check(instance, check.id)
        else:
            affected = self.card_graph.affected(instance, check.id)
            if affected:
                result["cards"] = [
                    {
                        "entity": name,
                        "status": "pending",
                        "rebuild_pending": True,
                        "reason": "no card model selected",
                    }
                    for name in affected
                ]
        return result
