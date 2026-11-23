"""Source-backed lesson aggregation and correction rebuild through the shared public core."""

import logging
from typing import Any

from anamnema.graph_store import MemoryDenied
from anamnema.lesson_graph import LessonGraph
from anamnema.lesson_models import LessonDocument, LessonJudgement, LessonWrite
from anamnema.lesson_search import LessonSearch
from anamnema.lesson_writer import JudgedLesson, LessonWriter
from anamnema.memory_sources import SourceLookup
from anamnema.search_services import CPUEncoder
from anamnema.setup_config import SetupError

LOGGER = logging.getLogger(__name__)


class LessonBuilder:
    """Model failures leave pending derivation; a correction is never voted twice to retry it."""

    def __init__(
        self, graph: LessonGraph, writer: LessonWriter, encoder: CPUEncoder, sources: SourceLookup
    ) -> None:
        self.graph, self.writer, self.encoder, self.sources = graph, writer, encoder, sources
        if (
            max(writer.policy.batch, writer.policy.candidates, writer.policy.max_lessons)
            > graph.store.settings.max_items
        ):
            raise SetupError("lesson policy counts exceed configured graph batch bounds")
        self.search = LessonSearch(graph.store)

    def build(
        self, instance: str, identities: tuple[str, ...], *, rejudge: bool = False
    ) -> dict[str, Any]:
        """Aggregate a bounded explicit batch next to owned current semantic candidates."""
        if type(rejudge) is not bool:
            raise SetupError("lesson rejudge must be a boolean")
        selected = identities if rejudge else self.graph.pending(instance, identities)
        already = [identity for identity in identities if identity not in selected]
        if not selected:
            return {
                "status": "success",
                "lessons": [],
                "unresolved": [],
                "held_back": [],
                "already_judged": already,
                "model": self.writer.identity,
            }
        notes, held = self._inputs(instance, selected)
        if not notes:
            raise SetupError("lesson batch has no eligible evidence; nothing generated")
        existing: dict[str, dict[str, Any]] = {}
        vectors = self.encoder.encode([note["text"] for note in notes], passage=False)
        for vector in vectors:
            for key in self.search.candidates(instance, vector, self.writer.policy.candidates):
                if key not in existing and len(existing) < self.writer.policy.candidates:
                    existing[key] = self.graph.get(instance, key)
        proposed, unresolved = self.writer.judge(instance, notes, list(existing.values()))
        written = self._write(instance, proposed, notes, pending_only=not rejudge)
        return {
            "status": "success",
            "lessons": written,
            "unresolved": unresolved,
            "held_back": held,
            "model": self.writer.identity,
            "already_judged": already,
        }

    def rebuild(self, instance: str, identity: str) -> dict[str, Any]:
        """Regenerate the named lesson from current owned evidence, not old generated text."""
        old = self.graph.get(instance, identity, current=False)
        identities = tuple(item["note"] for item in old["episodes"])
        notes, held = self._inputs(instance, identities, retained=True)
        if not notes:
            raise SetupError("lesson rebuild has no eligible evidence; rebuild remains pending")
        proposed, unresolved = self.writer.judge(instance, notes, [old])
        target = [item for item in proposed if item.id == identity]
        if len(target) != 1:
            raise SetupError("lesson rebuild did not address its existing target; remains pending")
        # A split needs a separately reviewed migration, never an orphan side effect of a check.
        if len(proposed) != 1:
            raise SetupError(
                "lesson rebuild proposed additional lessons; explicit aggregation required"
            )
        written = self._write(instance, target, notes)
        return {
            "lesson": identity,
            "status": "success",
            "written": written,
            "unresolved": unresolved,
            "held_back": held,
            "model": self.writer.identity,
        }

    def _inputs(
        self, instance: str, identities: tuple[str, ...], *, retained: bool = False
    ) -> tuple[list[dict[str, Any]], list[str]]:
        policy = self.writer.policy
        if (
            not identities
            or len(set(identities)) != len(identities)
            or len(identities) > policy.batch
        ):
            raise SetupError("lesson input note count exceeds policy or repeats an identity")
        notes: list[dict[str, Any]] = []
        held: list[str] = []
        for identity in identities:
            try:
                note = self.graph.store.note(instance, identity)
            except MemoryDenied:
                if not retained:
                    raise
                LOGGER.warning("retained lesson evidence is absent; held back from rebuild")
                held.append(identity)
                continue
            if note["wrong_open"] or note["status"] != "active":
                held.append(identity)
                continue
            token = self.graph.sources(instance, (identity,))[0]
            opened = self.sources.open_note(self.graph.store, instance, identity)
            context = opened.get("context")
            if not isinstance(context, dict) or context.get("status") == "unavailable":
                raise SetupError("lesson generation requires verified attributed source context")
            raw_rows = opened.get("rows")
            if not isinstance(raw_rows, list) or not raw_rows:
                raise SetupError("lesson source context contains no attributed dialogue")
            if any(not isinstance(row, dict) for row in raw_rows):
                raise SetupError("lesson source context contains an invalid dialogue row")
            rows = [row for row in raw_rows if isinstance(row, dict)]
            positions = [i for i, row in enumerate(rows) if row.get("cited") is True]
            if not positions:
                raise SetupError("lesson context has no attributable cited dialogue")
            chosen = sorted(
                sorted(
                    range(len(rows)),
                    key=lambda i: (
                        i not in positions,
                        min(abs(i - anchor) for anchor in positions),
                        i,
                    ),
                )[: policy.source_rows]
            )
            bounded = [
                {**rows[i], "text": str(rows[i]["text"])[: policy.row_characters]} for i in chosen
            ]
            notes.append(
                {
                    **token,
                    "source": {
                        "rows": bounded,
                        "context": context,
                        "generation_clipped": len(rows) > policy.source_rows
                        or any(
                            len(str(row.get("text", ""))) > policy.row_characters
                            for row in (rows[i] for i in chosen)
                        ),
                    },
                }
            )
        return notes, held

    def _write(
        self,
        instance: str,
        proposed: list[JudgedLesson],
        notes: list[dict[str, Any]],
        *,
        pending_only: bool = False,
    ) -> list[dict[str, Any]]:
        texts = [
            " ".join(
                [
                    item.content.statement,
                    item.content.mechanism,
                    *(f"{route.condition} {route.correction}" for route in item.content.routes),
                ]
            )
            for item in proposed
        ]
        vectors: tuple[tuple[float, ...], ...] = (
            self.encoder.encode(texts, passage=True) if texts else ()
        )
        tokens = {note["note"]: note["token"] for note in notes}
        documents = [
            LessonDocument(
                **item.content.model_dump(),
                id=item.id,
                model=self.writer.identity,
                embedding=vector,
                episodes=item.episodes,
            )
            for item, vector in zip(proposed, vectors, strict=True)
        ]
        placed = {episode.note for item in proposed for episode in item.episodes}
        judgement = LessonJudgement(
            lessons=tuple(
                LessonWrite(lesson=document, revision=item.revision)
                for item, document in zip(proposed, documents, strict=True)
            ),
            unresolved=tuple(sorted(set(tokens) - placed)),
            source_tokens=tokens,
            model=self.writer.identity,
            pending_only=pending_only,
        )
        versions = self.graph.write_judgement(instance, judgement)
        return [
            {"lesson": item.id, "version": version}
            for item, version in zip(proposed, versions, strict=True)
        ]

    def refresh_for_check(self, instance: str, check: str) -> list[dict[str, Any]]:
        """Replayed corrections retry only pending derivation, with no extra correction vote."""
        results: list[dict[str, Any]] = []
        for identity in self.graph.affected(instance, check):
            state = self.graph.get(instance, identity, current=False)
            if state["valid"]:
                results.append({"lesson": identity, "status": "success", "rebuilt": False})
                continue
            try:
                results.append(self.rebuild(instance, identity))
            except SetupError as exc:
                LOGGER.warning("lesson rebuild pending after correction: %s", exc)
                results.append(
                    {
                        "lesson": identity,
                        "status": "failed",
                        "rebuild_pending": True,
                        "reason": str(exc),
                    }
                )
        return results
