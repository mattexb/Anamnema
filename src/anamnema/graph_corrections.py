"""Source-derived correction semantics in one ownership-checked Neo4j transaction."""

from __future__ import annotations

from typing import Any

from neo4j import ManagedTransaction
from pydantic import JsonValue

from anamnema.archive_models import digest
from anamnema.graph_store import GraphStore, MemoryConflict, MemoryDenied, query, vector_check
from anamnema.memory_models import Correction, Outcome, Rewrite
from anamnema.memory_rating import evidence
from anamnema.setup_config import SetupError


def correct(
    store: GraphStore, instance: str, check: Correction, rewritten: Rewrite | None = None
) -> bool:
    """Apply an accountable check once; replay also works after explicit deletion."""
    if not instance:
        raise SetupError("correction requires an assistant instance")
    if rewritten is not None and check.outcome != Outcome.PARTLY:
        raise SetupError("only a partly correction accepts replacement embeddings")
    if check.rewrite and len(check.rewrite) > store.settings.max_note_characters:
        raise SetupError("corrected text exceeds configured graph limit")
    if rewritten is not None:
        for vector in (
            rewritten.query_vector,
            rewritten.passage_vector,
            *(p.vector for p in rewritten.pieces),
        ):
            vector_check(vector, store.settings)
    return store.write(lambda tx: _apply(tx, store, instance, check, rewritten))


def _apply(
    tx: ManagedTransaction,
    store: GraphStore,
    instance: str,
    check: Correction,
    rewritten: Rewrite | None,
) -> bool:
    settings = store.settings
    check_key = digest([instance, check.id])
    payload: dict[str, JsonValue] = {"check": check.model_dump(mode="json")}
    fingerprint = digest(payload)
    existing = query(
        tx,
        f"MATCH (c:{settings.check_label} {{id:$id}}) RETURN c.fingerprint AS hash",
        id=check_key,
    )
    if existing:
        if existing[0]["hash"] != fingerprint:
            raise MemoryConflict("check id already has different content")
        return False
    if check.outcome == Outcome.PARTLY and rewritten is None:
        raise SetupError("a new partly correction requires replacement embeddings")
    key = digest([instance, check.note])
    # Acquire a write lock before reading counters or deciding whether this check exists.
    owned = query(
        tx,
        f"""
MATCH (n:{settings.note_label} {{id:$key, instance:$instance}})
SET n.correction_lock = true
RETURN n.source_ids AS sources, n.insertion_sha256 AS insertion_hash
""",
        key=key,
        instance=instance,
    )
    if not owned:
        raise MemoryDenied("memory is absent or belongs to another assistant")
    # A concurrent check with this id may have committed while the note lock was awaited.
    existing = query(
        tx,
        f"MATCH (c:{settings.check_label} {{id:$id}}) RETURN c.fingerprint AS hash",
        id=check_key,
    )
    if existing:
        if existing[0]["hash"] != fingerprint:
            raise MemoryConflict("check id already has different content")
        query(tx, f"MATCH (n:{settings.note_label} {{id:$key}}) REMOVE n.correction_lock", key=key)
        return False
    if check.superseded_by:
        replacement = query(
            tx,
            f"""
MATCH (n:{settings.note_label} {{id:$key, instance:$instance}}) RETURN n.id AS id
""",
            key=digest([instance, check.superseded_by]),
            instance=instance,
        )
        if not replacement or check.superseded_by == check.note:
            raise MemoryDenied("replacement must be another note owned by this assistant")
    props = check.model_dump(mode="json", exclude_none=True)
    props.update(
        id=check_key,
        check_id=check.id,
        instance=instance,
        fingerprint=fingerprint,
        before_source_ids=owned[0]["sources"],
        deleted_note=check.delete is True,
        before_insertion_sha256=owned[0]["insertion_hash"],
    )
    query(
        tx,
        f"""
MATCH (n:{settings.note_label} {{id:$key, instance:$instance}})
CREATE (c:{settings.check_label}) SET c=$props, c.at=datetime()
CREATE (n)-[:CHECKED_BY]->(c)
""",
        key=key,
        instance=instance,
        props=props,
    )
    _update(tx, store, key, check)
    if rewritten is not None:
        _rewrite(tx, store, key, instance, check, rewritten)
    if check.delete is True:
        query(
            tx,
            f"""
MATCH (n:{settings.note_label} {{id:$key}})
OPTIONAL MATCH (p:{settings.piece_label})-[:PART_OF]->(n)
DETACH DELETE p, n
""",
            key=key,
        )
    return True


def _update(tx: ManagedTransaction, store: GraphStore, key: str, check: Correction) -> None:
    settings = store.settings
    positive, negative = evidence(check.outcome.value, settings.rating)
    wrong = {Outcome.WRONG: True, Outcome.CLEARED: False, Outcome.PARTLY: False}.get(check.outcome)
    status = "outdated" if check.outcome == Outcome.OUTDATED or check.superseded_by else None
    if check.outcome == Outcome.PARTLY:
        status = "active"
    query(
        tx,
        f"""
MATCH (n:{settings.note_label} {{id:$key}})
WITH n,size($records)>0 AND coalesce(n.source_ids,[])<>$records AS provenance_changed
SET n.trust_positive=n.trust_positive+$positive,
    n.trust_negative=n.trust_negative+$negative,
    n.useful=n.useful+$useful, n.not_useful=n.not_useful+$not_useful,
    n.wrong_open=coalesce($wrong,n.wrong_open),
    n.status=coalesce($status,n.status),
    n.superseded_by=coalesce($replacement,n.superseded_by),
    n.source_ids=CASE WHEN size($records)>0 THEN $records ELSE n.source_ids END,
    n.checked_at=datetime()
FOREACH (_ IN CASE WHEN provenance_changed THEN [1] ELSE [] END |
 SET n.subject_state='pending', n.subject_vector=n.passage_vector, n.subject=null,
     n.subject_records=null, n.subject_model=null, n.subject_version=null,n.subject_at=null,
     n.naming_attempts=0)
REMOVE n.correction_lock
WITH n
OPTIONAL MATCH (n)-[:IN_GROUP]->(g:{settings.group_label})
SET g.card_valid=false, g.notes_changed_at=datetime()
WITH DISTINCT n
OPTIONAL MATCH (n)-[:ABOUT_ENTITY]->(e:{settings.entity_label})
WITH n,collect(DISTINCT e) AS entities
FOREACH (entity IN entities | SET entity.card_valid=false,
         entity.revision=entity.revision+1,entity.updated_at=datetime())
WITH n,[entity IN entities | entity.name] AS affected
MATCH (c:{settings.check_label} {{instance:n.instance,check_id:$check}})
SET c.affected_entities=affected
WITH n,c
OPTIONAL MATCH (n)-[:EPISODE_OF|AGAINST]->(l:{settings.lesson_label})
WITH c,collect(DISTINCT l) AS lessons
FOREACH (lesson IN lessons | SET lesson.valid=false,lesson.revision=lesson.revision+1)
SET c.affected_lessons=[lesson IN lessons | lesson.lesson_id]
""",
        key=key,
        positive=positive,
        negative=negative,
        useful=int(check.useful is True),
        not_useful=int(check.useful is False),
        wrong=wrong,
        status=status,
        replacement=check.superseded_by,
        records=list(check.records),
        check=check.id,
    )


def _rewrite(
    tx: ManagedTransaction,
    store: GraphStore,
    key: str,
    instance: str,
    check: Correction,
    rewritten: Rewrite,
) -> None:
    s = store.settings
    query(
        tx,
        f"""
MATCH (n:{s.note_label} {{id:$key}})
SET n.text=$text, n.query_vector=$query_vector, n.passage_vector=$passage_vector,
    n.embedded_with=$model, n.superseded_by=null, n.status='active',
    n.subject_vector=$passage_vector, n.subject=null, n.subject_state='pending',
    n.lesson_state=null, n.naming_attempts=0,
    n.subject_records=null, n.subject_model=null, n.subject_version=null, n.subject_at=null,
    n.rewritten_at=datetime(), n.rewritten_by_check=$check
WITH n
OPTIONAL MATCH (old:{s.piece_label})-[:PART_OF]->(n)
DETACH DELETE old
""",
        key=key,
        text=check.rewrite,
        query_vector=list(rewritten.query_vector),
        passage_vector=list(rewritten.passage_vector),
        model=rewritten.embedded_with,
        check=check.id,
    )
    store.write_pieces(tx, key, instance, rewritten.pieces)


def history(store: GraphStore, instance: str, note: str) -> list[dict[str, Any]]:
    """Keep a deleted note's checks discoverable without opening another owner's data."""
    return [
        row["check"]
        for row in store.read(
            f"""
MATCH (c:{store.settings.check_label} {{instance:$instance, note:$note}})
RETURN c {{.*, id:c.check_id, at:toString(c.at)}} AS check ORDER BY c.at, c.check_id
""",
            instance=instance,
            note=note,
        )
    ]
