"""Durable keyed occurrences and decision audit; root note evidence stays untouched."""

from __future__ import annotations

from typing import Any

from neo4j import ManagedTransaction
from pydantic import JsonValue, ValidationError

from anamnema.archive_models import canonical, digest
from anamnema.graph_store import GraphStore, MemoryConflict, MemoryDenied, query
from anamnema.graph_topics import GraphTopics
from anamnema.memory_models import MemoryNote
from anamnema.setup_config import SetupError


def source_key(note: MemoryNote) -> str:
    """One claim in one telling, independent of extraction-run and graph aliases."""
    sources: list[JsonValue] = [source for source in sorted(note.source_ids)]
    return digest([note.instance, note.window, sources, note.text])


class MergeGraph:
    """All occurrence operations carry an explicit owner, including repair and history."""

    def __init__(self, store: GraphStore) -> None:
        self.store = store

    def refuse_other_run(self, instance: str, window: str, run: str) -> None:
        """A second extraction of one telling must not count its paraphrase as new evidence."""
        s = self.store.settings
        rows = self.store.read(
            f"MATCH (n:{s.note_label} {{instance:$instance,window:$window}}) "
            "RETURN n.run AS run UNION "
            f"MATCH (o:{s.occurrence_label} {{instance:$instance,window:$window}}) "
            "RETURN o.run AS run",
            instance=instance,
            window=window,
        )
        if any(row["run"] != run for row in rows):
            raise MemoryConflict("window already stored under another extraction run")

    def known_sources(self, instance: str, notes: list[MemoryNote]) -> set[str]:
        """Keyed source replays do not invoke the judge or add another occurrence."""
        rows = self.store.read(
            f"MATCH (o:{self.store.settings.occurrence_label} {{instance:$instance}}) "
            "WHERE o.id IN $ids RETURN o.id AS id",
            instance=instance,
            ids=[source_key(note) for note in notes],
        )
        return {str(row["id"]) for row in rows}

    def audit(self, instance: str, rows: list[dict[str, Any]]) -> None:
        """Keep all decisions, including different and deferred, without raw prompts."""
        if not rows:
            return
        props = [
            {
                "id": digest([instance, row]),
                "payload": canonical(row).decode("utf-8"),
                "instance": instance,
                "new": row["new"],
                "old": row["old"],
                "verdict": row["verdict"],
            }
            for row in rows
        ]
        self.store.write(
            lambda tx: query(
                tx,
                f"UNWIND $rows AS row MERGE (d:{self.store.settings.decision_label} {{id:row.id}}) "
                "ON CREATE SET d+=row, d.at=datetime()",
                rows=props,
            )
        )

    def append(self, instance: str, occurrences: list[dict[str, Any]]) -> int:
        """Write a bounded batch once, rejecting changed targets rather than double-counting."""
        if len(occurrences) > self.store.settings.max_items:
            raise SetupError("occurrence batch exceeds configured limit")
        return self.store.write(
            lambda tx: sum(self._append(tx, instance, row) for row in occurrences)
        )

    def _append(self, tx: ManagedTransaction, instance: str, row: dict[str, Any]) -> bool:
        note: MemoryNote = row["note"]
        if note.instance != instance:
            raise MemoryDenied("occurrence belongs to another assistant")
        props: dict[str, JsonValue] = {
            "id": source_key(note),
            "instance": instance,
            "alias": note.id,
            "under": row["under"],
            "verdict": row["verdict"],
            "score": row["score"],
            "judged_by": row["model"],
            "run": note.run,
            "window": note.window,
            "session": note.session,
            "substrate": note.substrate,
            "said_at": note.said_at,
            "text": note.text,
            "source_ids": list(note.source_ids),
            "proposal": note.model_dump_json(),
        }
        fingerprint = digest(props)
        s = self.store.settings
        root = query(
            tx,
            f"MATCH (n:{s.note_label} {{id:$key,instance:$instance}}) "
            "SET n.repeat_lock=true RETURN n.status AS status,n.wrong_open AS wrong",
            key=digest([instance, row["under"]]),
            instance=instance,
        )
        if not root or root[0]["status"] != "active" or root[0]["wrong"]:
            raise MemoryDenied("repeat target is absent, inactive or wrong-open")
        previous = query(
            tx,
            f"MATCH (o:{s.occurrence_label} {{id:$id}}) RETURN o.fingerprint AS fingerprint",
            id=props["id"],
        )
        if previous and previous[0]["fingerprint"] != fingerprint:
            raise MemoryConflict("source occurrence already has a different target or judgment")
        query(
            tx,
            f"MATCH (n:{s.note_label} {{id:$root}}) "
            f"MERGE (o:{s.occurrence_label} {{id:$id}}) "
            "ON CREATE SET o+=$props,o.fingerprint=$fingerprint,o.at=datetime() "
            "MERGE (n)-[:ALSO_SAID]->(o) REMOVE n.repeat_lock",
            root=digest([instance, row["under"]]),
            id=props["id"],
            props=props,
            fingerprint=fingerprint,
        )
        if not previous:
            query(
                tx,
                f"MATCH (n:{s.note_label} {{id:$root}})-[:ABOUT_ENTITY]->"
                f"(e:{s.entity_label}) SET e.revision=e.revision+1,e.card_valid=false",
                root=digest([instance, row["under"]]),
            )
        return not previous

    def occurrences(self, instance: str, root: str) -> list[dict[str, Any]]:
        """Bound source display independently of the number of stored retellings."""
        rows = self.store.read(
            f"MATCH (o:{self.store.settings.occurrence_label} {{instance:$instance,under:$root}}) "
            "RETURN o {.*,proposal:null,at:toString(o.at)} AS occurrence "
            "ORDER BY o.said_at,o.id LIMIT $count",
            instance=instance,
            root=root,
            count=self.store.settings.source_occurrences,
        )
        return [row["occurrence"] for row in rows]

    def unmerge(self, instance: str, alias: str, reason: str) -> dict[str, Any]:
        """Place the restored note before removing its occurrence; either stop is retryable."""
        if not reason.strip() or len(reason) > self.store.settings.max_note_characters:
            raise SetupError("unmerge requires a bounded reason")
        s = self.store.settings
        rows = self.store.read(
            f"MATCH (o:{s.occurrence_label} {{instance:$instance,alias:$alias}}) "
            "RETURN o.proposal AS proposal,o.under AS root,o {.*} AS stored",
            instance=instance,
            alias=alias,
        )
        if not rows:
            undone = self.store.read(
                f"MATCH (d:{s.decision_label} {{instance:$instance,new:$alias,verdict:'undone'}}) "
                "RETURN d.id AS id",
                instance=instance,
                alias=alias,
            )
            if undone:
                return {"note": alias, "undone": False}
            raise MemoryDenied("occurrence is absent or belongs to another assistant")
        stored = rows[0]["stored"]
        content = {key: value for key, value in stored.items() if key not in ("at", "fingerprint")}
        content.setdefault("said_at", None)
        if digest(content) != stored["fingerprint"]:
            raise SetupError("stored occurrence fingerprint differs; no automatic repair")
        try:
            original = MemoryNote.model_validate_json(rows[0]["proposal"])
        except ValidationError as exc:
            raise SetupError("stored occurrence proposal is corrupt; no automatic repair") from exc
        if original.instance != instance or original.id != alias:
            raise MemoryDenied("stored occurrence proposal ownership/alias differs")
        proposal = original.model_copy(update={"unmerged": reason})
        self.store.put(instance, (proposal,))
        GraphTopics(self.store).place(instance, alias)
        self.store.write(lambda tx: self._finish_undo(tx, instance, alias, rows[0]["root"], reason))
        return {"note": alias, "left": rows[0]["root"], "undone": True}

    def _finish_undo(
        self, tx: ManagedTransaction, instance: str, alias: str, root: str, reason: str
    ) -> None:
        props = {
            "instance": instance,
            "new": alias,
            "old": root,
            "verdict": "undone",
            "reason": reason,
        }
        query(
            tx,
            f"MATCH (o:{self.store.settings.occurrence_label} "
            "{instance:$instance,alias:$alias}) DETACH DELETE o",
            instance=instance,
            alias=alias,
        )
        query(
            tx,
            f"MERGE (d:{self.store.settings.decision_label} {{id:$id}}) "
            "ON CREATE SET d=$props,d.at=datetime()",
            id=digest([instance, alias, "undone"]),
            props=props,
        )
        query(
            tx,
            f"MATCH (n:{self.store.settings.note_label} "
            "{instance:$instance,note_id:$root})-[:ABOUT_ENTITY]->"
            f"(e:{self.store.settings.entity_label}) "
            "SET e.revision=e.revision+1,e.card_valid=false",
            instance=instance,
            root=root,
        )
