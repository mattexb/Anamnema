"""Owned subject representations with text-bound writes after judgement, never note rewrites."""

from typing import Annotated, Any, Literal, Self

from neo4j import ManagedTransaction
from pydantic import Field, model_validator

from anamnema.archive_models import digest
from anamnema.graph_store import GraphStore, query, vector_check
from anamnema.setup_config import Nonempty, SetupError, StrictModel

Finite = Annotated[float, Field(allow_inf_nan=False)]


class SubjectWrite(StrictModel):
    """Every persisted label binds the exact text and sources used in its judgement."""

    note: Nonempty
    text: Nonempty
    source_ids: tuple[Nonempty, ...]
    state: Literal["named", "unknown"]
    subject: str
    model: Nonempty
    version: Nonempty
    vector: tuple[Finite, ...] | None = None

    @model_validator(mode="after")
    def coherent(self) -> Self:
        if self.state == "named" and (not self.subject.strip() or self.vector is None):
            raise ValueError("named subjects require nonempty text and an embedding")
        if self.state == "unknown" and (self.subject or self.vector is not None):
            raise ValueError("unknown subjects use only the original passage embedding")
        if not self.source_ids or len(self.source_ids) != len(set(self.source_ids)):
            raise ValueError("subject provenance requires distinct source ids")
        return self


class NamingGraph:
    """A corrected note stays pending when a judgement describes its earlier text or sources."""

    def __init__(self, store: GraphStore) -> None:
        self.store = store

    def pending(self, instance: str, limit: int) -> tuple[dict[str, Any], ...]:
        """Bound selection explicitly; callers can resume until no pending notes remain."""
        if not instance or not 1 <= limit <= self.store.settings.max_items:
            raise SetupError("naming selection exceeds configured bounds or lacks an owner")
        rows = self.store.read(
            f"""
MATCH (n:{self.store.settings.note_label} {{instance:$instance}})
WHERE coalesce(n.subject_state,'pending')='pending'
  AND n.status='active' AND NOT coalesce(n.wrong_open,false)
RETURN n.note_id AS id,coalesce(n.naming_attempts,0) AS attempts
ORDER BY attempts,id LIMIT $limit
""",
            instance=instance,
            limit=limit,
        )
        return tuple(self.store.note(instance, str(row["id"])) for row in rows)

    def seed(self, instance: str) -> int:
        """Existing unnamed notes get their original passage vector, without changing content."""
        if not instance:
            raise SetupError("naming requires an assistant instance")
        rows = self.store.write(
            lambda tx: query(
                tx,
                f"""
MATCH (n:{self.store.settings.note_label} {{instance:$instance}})
WHERE n.subject_vector IS NULL
SET n.subject_vector=n.passage_vector,
    n.subject_state=coalesce(n.subject_state,'pending')
RETURN count(n) AS seeded
""",
                instance=instance,
            )
        )
        return int(rows[0]["seeded"])

    def write(self, instance: str, subjects: tuple[SubjectWrite, ...]) -> int:
        """Lock before comparing; stale/foreign/deleted rows are skipped, never marked named."""
        if not instance or len(subjects) > self.store.settings.max_items:
            raise SetupError("naming write exceeds configured bounds or lacks an owner")
        if len({subject.note for subject in subjects}) != len(subjects):
            raise SetupError("naming write repeats a note")
        for subject in subjects:
            if subject.vector is not None:
                vector_check(subject.vector, self.store.settings)
        bounded = tuple(
            s for s in subjects if len(s.text) <= self.store.settings.max_note_characters
        )
        return self.store.write(lambda tx: sum(self._write(tx, instance, s) for s in bounded))

    def attempted(self, instance: str, notes: list[dict[str, Any]]) -> int:
        """Rotate failed/missing answers behind untouched pending notes, without retiring them."""
        if not instance or len(notes) > self.store.settings.max_items:
            raise SetupError("naming attempt batch exceeds configured bounds")
        rows = [
            {
                "key": digest([instance, str(note["id"])]),
                "text": note["text"],
                "sources": note["source_ids"],
            }
            for note in notes
        ]
        result = self.store.write(
            lambda tx: query(
                tx,
                f"""
UNWIND $rows AS item
MATCH (n:{self.store.settings.note_label} {{id:item.key,instance:$instance}})
SET n.naming_lock=true
WITH n,item,n.text=item.text AND n.source_ids=item.sources AS current
FOREACH (_ IN CASE WHEN current THEN [1] ELSE [] END |
 SET n.naming_attempts=coalesce(n.naming_attempts,0)+1)
REMOVE n.naming_lock
RETURN sum(CASE WHEN current THEN 1 ELSE 0 END) AS attempted
""",
                instance=instance,
                rows=rows,
            )
        )
        return int(result[0]["attempted"] or 0)

    def _write(self, tx: ManagedTransaction, instance: str, subject: SubjectWrite) -> int:
        rows = query(
            tx,
            f"""
MATCH (n:{self.store.settings.note_label} {{id:$key,instance:$instance}})
SET n.naming_lock=true
WITH n, n.text=$text AND n.source_ids=$sources AND n.status='active'
        AND NOT coalesce(n.wrong_open,false) AS current
FOREACH (_ IN CASE WHEN current THEN [1] ELSE [] END |
 SET n.subject=$subject, n.subject_state=$state, n.subject_records=$sources,
     n.subject_model=$model, n.subject_version=$version, n.subject_at=datetime(),
     n.subject_vector=coalesce($vector,n.passage_vector))
REMOVE n.naming_lock
RETURN CASE WHEN current THEN 1 ELSE 0 END AS written
""",
            key=digest([instance, subject.note]),
            instance=instance,
            text=subject.text,
            sources=list(subject.source_ids),
            subject=subject.subject,
            state=subject.state,
            model=subject.model,
            version=subject.version,
            vector=list(subject.vector) if subject.vector is not None else None,
        )
        return sum(int(row["written"]) for row in rows)
