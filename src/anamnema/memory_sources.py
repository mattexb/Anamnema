"""Exact original-source lookup against the owning archive, independent of note wording."""

from __future__ import annotations

from typing import Any

from pydantic import JsonValue
from pymongo.errors import PyMongoError

from anamnema.archive_codec import recover_record
from anamnema.archive_models import Record, canonical
from anamnema.archive_mongo import MongoArchive
from anamnema.graph_store import GraphStore, MemoryDenied
from anamnema.merge_graph import MergeGraph
from anamnema.setup_config import SetupError
from anamnema.source_display import SourceDisplay
from anamnema.source_policy import source_policy
from anamnema.window_store import WindowStore


class SourceLookup:
    """Open requested records only; an index miss never triggers archive-wide search."""

    def __init__(
        self,
        archive: MongoArchive,
        maximum_records: int,
        maximum_bytes: int,
        display: SourceDisplay | None = None,
    ) -> None:
        self.archive = archive
        self.maximum_records = maximum_records
        self.maximum_bytes = maximum_bytes
        self.display = display
        self.windows = display.windows if display else WindowStore(archive, source_policy())
        if display and display.policy.context_records > maximum_records:
            raise SetupError("source context scan exceeds the record lookup bound")
        self.decode_settings = archive.settings.model_copy(
            update={"max_file_bytes": min(maximum_bytes, archive.settings.max_file_bytes)}
        )

    def records(self, instance: str, identities: tuple[str, ...]) -> tuple[Record, ...]:
        """Reconstruct and verify original identities/content, including compressed records."""
        if not instance or not 1 <= len(identities) <= self.maximum_records:
            raise SetupError("empty source request or record count exceeds configured limit")
        if len(set(identities)) != len(identities):
            raise SetupError("source request repeats an id")
        try:
            documents = self.archive.database[
                self.archive.settings.collections["conversation"]
            ].find({"_id": {"$in": list(identities)}, "instance": instance})
            found: dict[str, Record] = {}
            total = 0
            for document in documents:
                row = self._record(document)
                total += len(canonical(row.source))
                if total > self.maximum_bytes:
                    raise SetupError("source lookup exceeds configured byte limit")
                if row.instance != instance or row.identity not in identities:
                    raise SetupError("archive source ownership/identity differs")
                found[row.identity] = row
        except PyMongoError as exc:
            raise SetupError(f"source lookup failed ({type(exc).__name__})") from exc
        if len(found) != len(identities):
            raise MemoryDenied("source is absent or belongs to another assistant")
        return tuple(found[identity] for identity in identities)

    def _record(self, document: dict[str, Any]) -> Record:
        record = recover_record(document, self.decode_settings)
        if record.kind != "conversation":
            raise SetupError("archived conversation has the wrong record kind")
        return record

    def open_note(
        self, store: GraphStore, instance: str, identity: str, include_raw: bool = False
    ) -> dict[str, JsonValue]:
        """Show bounded context; include exact originals only when explicitly requested."""
        note = store.note(instance, identity)
        records = self.records(instance, tuple(note["source_ids"]))
        result: dict[str, JsonValue] = {
            "note": identity,
            "text": note["text"],
            "judge_against": [record.identity for record in records],
            "also_said_count": note["said_again"],
            "retold_count": note["retold_count"],
        }
        if include_raw:
            result["sources"] = [
                {**record.core(), "source_line": record.source_line, "source": record.source}
                for record in records
            ]
        if self.display is not None:
            result.update(self.display.read(instance, note, records, self.records))
        elif not include_raw:
            result.update(
                rows=[], context={"status": "unavailable", "reason": "no source reader configured"}
            )
        result["occurrences"] = self._occurrences(store, instance, identity, include_raw)
        return result

    def _occurrences(
        self, store: GraphStore, instance: str, identity: str, raw: bool
    ) -> list[JsonValue]:
        """Retellings retain separately verified sources instead of borrowing the root's."""
        result: list[JsonValue] = []
        for occurrence in MergeGraph(store).occurrences(instance, identity):
            records = self.records(instance, tuple(occurrence["source_ids"]))
            shown: dict[str, JsonValue] = {
                key: occurrence.get(key)
                for key in ("alias", "text", "verdict", "said_at", "session", "window", "judged_by")
            }
            shown["judge_against"] = [record.identity for record in records]
            if raw:
                shown["sources"] = [record.source for record in records]
            if self.display:
                shown.update(self.display.read(instance, occurrence, records, self.records))
            result.append(shown)
        return result
