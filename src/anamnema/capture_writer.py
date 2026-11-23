"""Verified handwritten notes use the existing insertion, tombstone and placement protections."""

from __future__ import annotations

from typing import Any

from pydantic import ValidationError

from anamnema.archive_models import digest
from anamnema.graph_store import GraphStore, MemoryConflict
from anamnema.graph_topics import GraphTopics
from anamnema.marked_capture import CaptureMark, CapturePolicy, capture_semantics, record_marks
from anamnema.memory_models import CaptureOrigin, MemoryNote
from anamnema.memory_sources import SourceLookup
from anamnema.search_services import CPUEncoder
from anamnema.setup_config import SetupError
from anamnema.speech_policy import SpeechPolicy


def _stored_origin(value: object) -> CaptureOrigin:
    if not isinstance(value, str):
        raise MemoryConflict("capture origin is missing or invalid; existing note retained")
    try:
        return CaptureOrigin.model_validate_json(value)
    except ValidationError as exc:
        raise MemoryConflict("capture origin is invalid; existing note retained") from exc


class CaptureWriter:
    """A replay never re-embeds or rewrites existing notes, including corrected ones."""

    def __init__(
        self,
        store: GraphStore,
        sources: SourceLookup,
        encoder: CPUEncoder,
        policy: CapturePolicy,
        speech: SpeechPolicy,
    ) -> None:
        policy.check(speech)
        if store.settings.dimensions != encoder.policy.dimensions:
            raise SetupError("capture graph/encoder dimensions differ")
        if policy.max_text_characters > store.settings.max_note_characters:
            raise SetupError("capture text bound exceeds graph bound")
        self.store, self.sources, self.encoder = store, sources, encoder
        self.policy, self.speech = policy.model_copy(deep=True), speech.model_copy(deep=True)
        self.run = digest(
            [
                "marked",
                self.policy.model_dump(
                    mode="json", exclude={"max_marks", "max_text_characters", "turn_prompt"}
                ),
                capture_semantics(self.speech),
            ]
        )
        self.topics = GraphTopics(store)

    def _verified(self, instance: str, marks: tuple[CaptureMark, ...]) -> tuple[CaptureMark, ...]:
        if len(marks) > self.policy.max_marks or len({mark.id for mark in marks}) != len(marks):
            raise SetupError("capture marks repeat or exceed configured bound")
        if (
            any(mark.instance != instance for mark in marks)
            or len({(mark.session, mark.substrate) for mark in marks}) > 1
        ):
            raise SetupError("capture marks mix ownership/session/substrate")
        grouped: dict[str, list[CaptureMark]] = {}
        for mark in marks:
            grouped.setdefault(mark.source_id, []).append(mark)
        verified: list[CaptureMark] = []
        for source, expected in grouped.items():
            records = self.sources.records(instance, (source,))
            if len(records) != 1:
                raise SetupError("capture source lookup must return exactly one record")
            record = records[0]
            actual = record_marks(record, self.policy, self.speech)
            if tuple(sorted(expected, key=lambda mark: mark.index)) != actual:
                raise SetupError("capture input differs from verified source; no notes written")
            verified.extend(actual)
        return tuple(verified)

    def _note(self, instance: str, identity: str) -> dict[str, Any] | None:
        rows = self.store.read(
            f"MATCH (n:{self.store.settings.note_label} {{instance:$instance,note_id:$note}}) "
            "RETURN n{.*} AS note",
            instance=instance,
            note=identity,
        )
        return rows[0]["note"] if rows else None

    def apply(self, instance: str, marks: tuple[CaptureMark, ...]) -> dict[str, Any]:
        """Verify every input first; committed prefixes are discoverable and safe to retry."""
        verified = self._verified(instance, marks)
        result: dict[str, Any] = {
            "marks": len(verified),
            "inserted": 0,
            "already_present": 0,
            "not_materialized": 0,
            "topic_joins": 0,
            "topics_created": 0,
            "placed_again": 0,
            "notes_model_calls": 0,
            "note_ids": [],
        }
        failures: dict[str, str] = {}
        for mark in verified:
            kind = self.policy.kinds[mark.marker]
            lesson_of = (
                failures.get(mark.source_id) if kind in self.policy.principle_kinds else None
            )
            if kind in self.policy.failure_kinds:
                failures[mark.source_id] = mark.id
            origin = CaptureOrigin(
                source_id=mark.source_id,
                index=mark.index,
                marker=mark.marker,
                text_digest=digest(mark.text),
                entity=mark.entity,
                lesson_of=lesson_of,
            )
            note = self._note(instance, mark.id)
            existing = note is not None
            if note is None:
                if self.store.known(instance, mark.id):
                    result["not_materialized"] += 1
                    continue
                vectors = self.encoder.embed_note(mark.text)
                proposal = MemoryNote(
                    id=mark.id,
                    instance=instance,
                    text=mark.text,
                    kind=kind,
                    source_ids=(mark.source_id,),
                    session=mark.session,
                    substrate=mark.substrate,
                    window=None,
                    run=self.run,
                    said_at=mark.timestamp,
                    event_time=mark.timestamp if kind in self.policy.event_time_kinds else None,
                    capture_origin=origin,
                    **vectors.model_dump(),
                )
                inserted = self.store.put(instance, (proposal,))
                result["inserted"] += inserted
                existing = not bool(inserted)
                note = self._note(instance, mark.id)
                if note is None:
                    result["not_materialized"] += 1
                    continue
            if note.get("run") != self.run:
                raise MemoryConflict("capture policy changed; explicit migration is required")
            if _stored_origin(note.get("capture_origin")) != origin:
                raise MemoryConflict("capture origin differs; existing note retained")
            placed = self.topics.place(instance, mark.id)
            result["already_present"] += int(existing)
            result["placed_again"] += int(existing and bool(placed.joined or placed.created))
            result["topic_joins"] += placed.joined
            result["topics_created"] += placed.created
            result["note_ids"].append(mark.id)
        return result
