"""Verified immutable extraction chunks become source-linked graph insertion proposals."""

from __future__ import annotations

from datetime import UTC, datetime

from pydantic import ValidationError

from anamnema.archive_models import canonical, digest
from anamnema.chunks import Chunk
from anamnema.extraction_store import ExtractionStore
from anamnema.graph_store import GraphStore
from anamnema.graph_topics import GraphTopics
from anamnema.memory_models import Facet, MemoryNote
from anamnema.memory_sources import SourceLookup
from anamnema.merge_graph import MergeGraph
from anamnema.processing import Window
from anamnema.repeat_merge import MergeResult, RepeatMerger
from anamnema.search_services import CPUEncoder
from anamnema.setup_config import Nonempty, SetupError, StrictModel


class ExtractedNote(StrictModel):
    """The M3 normalized saved-note shape, revalidated at the graph boundary."""

    text: Nonempty
    kind: Nonempty
    rows: tuple[int, ...]
    ruled_by: int
    owner_source: str | None
    source_ids: tuple[Nonempty, ...]
    said_at: str | None
    candidate_evidence: bool
    problems: tuple[str, ...]


class MemoryLoader:
    """Common loader consumes shared windows; Claude translation stays in its extender."""

    def __init__(
        self,
        store: GraphStore,
        sources: SourceLookup,
        extraction: ExtractionStore,
        encoder: CPUEncoder,
        merger: RepeatMerger | None = None,
    ) -> None:
        self.store, self.sources, self.extraction, self.encoder = (
            store,
            sources,
            extraction,
            encoder,
        )
        self.topics = GraphTopics(store)
        self.merger = merger
        if merger is not None and merger.store is not store:
            raise SetupError("repeat merge must use the same owned graph store as insertion")
        if store.settings.dimensions != encoder.policy.dimensions:
            raise SetupError("graph and encoder dimensions differ")
        if sources.windows.policy.windows_collection in (
            extraction.config.chunks_collection,
            extraction.config.attempts_collection,
        ):
            raise SetupError("window references must not share an extraction collection")

    def window(
        self, window: Window, chunks: tuple[Chunk, ...], run: str, facets: tuple[Facet, ...] = ()
    ) -> dict[str, int]:
        """Revalidate saved answers and source records, then incrementally insert once."""
        instance = window.boundary.instance
        if self.merger:
            self.merger.graph.refuse_other_run(instance, str(window.report()["key"]), run)
        saved: list[tuple[str, list[ExtractedNote]]] = []
        for chunk in chunks:
            selector = self.extraction.selector(window, chunk, run)
            document = self.extraction.load(selector, chunk)
            if document is None:
                raise SetupError("window has a missing extraction chunk; finish extraction first")
            try:
                notes = [
                    ExtractedNote.model_validate_json(canonical(value))
                    for value in document["notes"]
                ]
            except (TypeError, ValidationError) as exc:
                raise SetupError("saved extraction note violates the graph input contract") from exc
            saved.append((str(selector["_id"]), notes))
        self.sources.windows.save(window)
        fresh, pending, total = self._proposals(window, saved, run, facets)
        merge = self.merger.plan(fresh) if self.merger else MergeResult(fresh)
        return self._finish(instance, merge, pending, total)

    def _proposals(
        self,
        window: Window,
        saved: list[tuple[str, list[ExtractedNote]]],
        run: str,
        facets: tuple[Facet, ...],
    ) -> tuple[list[MemoryNote], list[tuple[str, bool]], int]:
        instance = window.boundary.instance
        total = 0
        fresh: list[MemoryNote] = []
        pending: list[tuple[str, bool]] = []
        for chunk_key, notes in saved:
            for index, note in enumerate(notes):
                self.sources.records(instance, note.source_ids)
                identity = digest([chunk_key, index])
                if self.store.known(instance, identity):
                    pending.append((identity, True))
                    total += 1
                    continue
                vectors = self.encoder.embed_note(note.text)
                said = _time(note.said_at)
                event = said if note.kind in self.store.settings.rating.event_time_kinds else None
                proposal = MemoryNote(
                    id=identity,
                    instance=instance,
                    text=note.text,
                    kind=note.kind,
                    source_ids=note.source_ids,
                    session=window.boundary.session,
                    substrate=window.boundary.substrate,
                    window=str(window.report()["key"]),
                    run=run,
                    said_at=said,
                    event_time=event,
                    problems=note.problems,
                    candidate_evidence=note.candidate_evidence,
                    facets=facets,
                    **vectors.model_dump(),
                )
                fresh.append(proposal)
                total += 1
        return fresh, pending, total

    def _finish(
        self, instance: str, merge: MergeResult, pending: list[tuple[str, bool]], total: int
    ) -> dict[str, int]:
        inserted = joined = created = placed_again = 0
        already = len(pending)
        for proposal in merge.kept:
            inserted += self.store.put(instance, (proposal,))
            pending.append((proposal.id, False))
        # An occurrence is durable before any placement can fail.
        MergeGraph(self.store).append(instance, merge.occurrences)
        for identity, existing in pending:
            # Deleted notes and occurrence aliases are known but have no note to place.
            if self._materialized(instance, identity):
                result = self.topics.place(instance, identity)
                joined += result.joined
                created += result.created
                placed_again += int(existing and bool(result.joined or result.created))
        return {
            "notes": total,
            "inserted": inserted,
            "already_present": already,
            "topic_joins": joined,
            "topics_created": created,
            "placed_again": placed_again,
            "merge_judged": merge.judged,
            "merge_deferred": merge.deferred,
            "merge_changed": merge.changed,
            "merge_replayed": merge.replayed,
            "repeats": sum(row["verdict"] == "same_claim" for row in merge.occurrences),
            "retellings": sum(row["verdict"] == "same_event" for row in merge.occurrences),
        }

    def _materialized(self, instance: str, identity: str) -> bool:
        """Only stored notes need placement; occurrences and deletion checks do not."""
        return bool(
            self.store.read(
                f"MATCH (n:{self.store.settings.note_label} "
                "{instance:$instance, note_id:$note}) RETURN n.id AS id",
                instance=instance,
                note=identity,
            )
        )


def _time(value: str | None) -> str | None:
    if value is None:
        return None
    try:
        moment = datetime.fromisoformat(value)
        # The source reader treats unzoned archive times as UTC; retain that convention.
        return moment.replace(tzinfo=moment.tzinfo or UTC).isoformat()
    except ValueError as exc:
        raise SetupError("saved note source time is invalid") from exc
