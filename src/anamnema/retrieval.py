"""Source-derived fusion, relevance/trust floors, budget paging and accountable reranking."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Protocol

from anamnema.graph_search import GraphSearch, fuse, words_of
from anamnema.graph_store import GraphStore
from anamnema.memory_models import Facet, MemoryInputError
from anamnema.memory_rating import NoteRating, effective_trust, priority
from anamnema.retrieval_config import RetrievalSettings
from anamnema.setup_config import SetupError


class Ranker(Protocol):
    """Raw score per input document, never a semantic truth verdict."""

    def scores(self, question: str, documents: list[str]) -> list[float]:
        """Return validated finite scores in the documents' original order."""
        ...


class Retriever:
    """Common retrieval engine, independent of Claude or a future substrate extender."""

    def __init__(
        self, store: GraphStore, settings: RetrievalSettings, ranker: Ranker | None = None
    ) -> None:
        self.store = store
        self.settings = settings
        self.search = GraphSearch(store)
        self.ranker = ranker
        if (
            max(
                settings.vector_candidates * settings.vector_oversample,
                settings.text_candidates,
                settings.pool + settings.meaning_pool,
            )
            > store.settings.max_items
        ):
            raise SetupError("retrieval candidate limits exceed graph request bounds")

    def recall(
        self,
        instance: str,
        question: str,
        vector: tuple[float, ...],
        facets: tuple[Facet, ...] = (),
        at: datetime | None = None,
        page: int = 1,
    ) -> dict[str, Any]:
        """Deliver one source-linked page; outages are errors, not archive-absence claims."""
        s = self.settings
        if not question.strip() or len(question) > s.max_question_characters or page < 1:
            raise MemoryInputError("question or page violates retrieval limits")
        if at is not None and at.tzinfo is None:
            raise MemoryInputError("historical search needs a timezone-aware instant")
        scored = self.search.meaning(instance, vector, s.vector_candidates * s.vector_oversample)
        scored = scored[: s.vector_candidates]
        similarity = dict(scored)
        meaning = list(similarity)
        words = self.search.words(instance, words_of(question, s.max_words), s.text_candidates)
        named: set[str] = set(self.search.identifiers(instance, facets)) if facets else set()
        unscored = list(dict.fromkeys([*words, *named]))
        unscored = [identity for identity in unscored if identity not in similarity]
        similarity.update(self.search.similarities(instance, vector, unscored))
        fused = fuse([meaning, words, [i for i in meaning + words if i in named]], s.rank_fusion_k)
        fused = {
            identity: score
            for identity, score in fused.items()
            if similarity.get(identity, -1.0) >= s.min_similarity
        }
        ranked = sorted(fused, key=lambda identity: (-fused[identity], identity))
        place = {identity: rank for rank, identity in enumerate(ranked)}
        closest = [identity for identity in meaning if identity in fused][: s.meaning_pool]
        for rank, identity in enumerate(closest):
            place[identity] = min(place[identity], rank)
        pool = sorted(
            set(ranked[: s.pool]) | set(closest), key=lambda identity: (place[identity], identity)
        )
        pages, readable = self._pages(instance, pool, place, similarity, at)
        rerank = self._rerank(question, pages)
        return {
            "page": page,
            "more": page < len(pages),
            "candidates": len(pool),
            "above_floor": readable,
            "notes": pages[page - 1] if page <= len(pages) else [],
            "rerank": rerank,
            "answer": None
            if readable
            else "no adequate index answer; the archive itself was not searched",
        }

    def _pages(
        self,
        instance: str,
        pool: list[str],
        place: dict[str, int],
        similarity: dict[str, float],
        at: datetime | None,
    ) -> tuple[list[list[dict[str, Any]]], int]:
        now = datetime.now(UTC)
        kept: dict[str, tuple[float, int, dict[str, Any]]] = {}
        for identity in pool:
            note = self.store.note(instance, identity)
            said = datetime.fromisoformat(note["said_at"]) if note.get("said_at") else None
            if at is not None and said is not None and said > at:
                continue
            rating = rating_of(note)
            if (
                effective_trust(rating, self.store.settings.rating)
                < self.store.settings.rating.floor
            ):
                continue
            text_key = " ".join(note["text"].split()).lower()
            kept.setdefault(
                text_key,
                (
                    priority(rating, at or now, at is None, self.store.settings.rating),
                    place[identity],
                    note,
                ),
            )
        ordered = sorted(kept.values(), key=lambda item: (item[1], -item[0], item[2]["id"]))
        pages: list[list[dict[str, Any]]] = []
        current: list[dict[str, Any]] = []
        used = 0
        budget = self.settings.header_tokens * self.settings.characters_per_token
        for score, _, note in ordered:
            header = {
                key: note.get(key)
                for key in (
                    "id",
                    "text",
                    "kind",
                    "status",
                    "superseded_by",
                    "said_at",
                    "source_ids",
                )
            }
            header.update(priority=score, similarity=similarity[note["id"]])
            size = len(note["text"]) + self.settings.header_overhead_characters
            if current and (len(current) == self.settings.page or used + size > budget):
                pages.append(current)
                current, used = [], 0
            current.append(header)
            used += size
        if current:
            pages.append(current)
        return pages, len(ordered)

    def _rerank(self, question: str, pages: list[list[dict[str, Any]]]) -> dict[str, Any]:
        if not self.settings.rerank_enabled or not pages:
            return {"status": "disabled" if not self.settings.rerank_enabled else "empty"}
        if self.ranker is None:
            raise SetupError("reranking is enabled but no reranker was configured")
        head, rest = pages[0][: self.settings.rerank_page], pages[0][self.settings.rerank_page :]
        try:
            scores = self.ranker.scores(
                question[: self.settings.rerank_query_characters],
                [str(note["text"]) for note in head],
            )
        except SetupError as exc:
            return {"status": "failed", "reason": str(exc)}
        order = sorted(range(len(head)), key=lambda index: (-scores[index], index))
        ordered = [head[index] for index in order]
        kept = sum(
            scores[order[0]] - scores[index] <= self.settings.rerank_keep_within for index in order
        )
        pages[0] = ordered[:kept] + rest
        if kept < len(ordered):
            pages.insert(1, ordered[kept:])
        return {"status": "reranked", "kept": kept}


def rating_of(note: dict[str, Any]) -> NoteRating:
    """Decode graph counters without inventing an event time from extraction time."""
    return NoteRating(
        positive=float(note["trust_positive"]),
        negative=float(note["trust_negative"]),
        wrong_open=bool(note["wrong_open"]),
        status=str(note["status"]),
        useful=int(note["useful"]),
        not_useful=int(note["not_useful"]),
        event_time=datetime.fromisoformat(note["event_time"]) if note.get("event_time") else None,
    )
