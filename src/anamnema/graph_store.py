"""Project-scoped Neo4j notes, indexes and immutable insertion transactions."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, LiteralString, cast

from neo4j import Driver, GraphDatabase, ManagedTransaction, Session, unit_of_work
from neo4j.exceptions import DriverError, Neo4jError

from anamnema.archive_access import keyring_secret
from anamnema.archive_models import digest
from anamnema.graph_values import json_temporals
from anamnema.ingest_config import ArchiveSettings
from anamnema.memory_config import MemorySettings
from anamnema.memory_models import MemoryNote, Piece
from anamnema.setup_config import SetupConfig, SetupError


class MemoryDenied(SetupError):
    """The requested memory is absent or belongs to another assistant."""


class MemoryConflict(SetupError):
    """A stable identity was reused for different immutable content."""


def query(tx: ManagedTransaction, statement: str, **parameters: Any) -> list[dict[str, Any]]:
    """Run project-owned Cypher; only validated schema identifiers are interpolated."""
    return [record.data() for record in tx.run(cast(LiteralString, statement), parameters)]


def vector_check(vector: tuple[float, ...], settings: MemorySettings) -> None:
    """Reject mismatched and zero-length semantic vectors before any write/search."""
    if len(vector) != settings.dimensions or not any(vector):
        raise SetupError("embedding dimensions differ or vector has zero norm")


class GraphStore:
    """One explicit database; no private discovery or cross-assistant fallback."""

    def __init__(self, driver: Driver, settings: MemorySettings) -> None:
        self.driver = driver
        self.settings = settings

    def close(self) -> None:
        """Release only this store's driver resources."""
        self.driver.close()

    def write[T](self, operation: Callable[[ManagedTransaction], T]) -> T:
        """Execute one all-or-nothing mutation with safe infrastructure errors."""
        try:
            with self._session() as session:
                return session.execute_write(
                    unit_of_work(timeout=self.settings.timeout_seconds)(operation)
                )
        except (DriverError, Neo4jError, OSError) as exc:
            raise SetupError(f"graph write failed ({type(exc).__name__}); safe to resume") from exc

    def read(self, statement: str, **parameters: Any) -> list[dict[str, Any]]:
        """Never translate an unavailable graph into an empty search result."""

        def work(tx: ManagedTransaction) -> list[dict[str, Any]]:
            return query(tx, statement, **parameters)

        try:
            with self._session() as session:
                operation = unit_of_work(timeout=self.settings.timeout_seconds)(work)
                return session.execute_read(operation)
        except (DriverError, Neo4jError, OSError) as exc:
            raise SetupError(f"graph read failed ({type(exc).__name__})") from exc

    def _session(self) -> Session:
        # The vendor annotates keyword configuration as Unknown; constrain it at this boundary.
        factory = cast(Callable[..., Session], cast(Any, self.driver).session)
        return factory(database=self.settings.database)

    def ensure_schema(self) -> None:
        """Create the project's constraints/search indexes without touching other labels."""
        s = self.settings
        labels = (
            s.note_label,
            s.piece_label,
            s.group_label,
            s.check_label,
            s.occurrence_label,
            s.decision_label,
            s.entity_label,
            s.statement_label,
            s.move_label,
            s.entity_event_label,
            s.lesson_label,
            s.concept_label,
        )
        statements = [
            f"CREATE CONSTRAINT {label.lower()}_id IF NOT EXISTS "
            f"FOR (n:{label}) REQUIRE n.id IS UNIQUE"
            for label in labels
        ]
        for label in (s.note_label, s.piece_label, s.lesson_label):
            statements.append(
                f"CREATE VECTOR INDEX {label.lower()}_passage IF NOT EXISTS "
                f"FOR (n:{label}) ON n.passage_vector "
                f"OPTIONS {{indexConfig: {{`vector.dimensions`: {s.dimensions}, "
                "`vector.similarity_function`: 'cosine'}}"
            )
        statements.append(
            f"CREATE VECTOR INDEX {s.note_label.lower()}_subject IF NOT EXISTS "
            f"FOR (n:{s.note_label}) ON n.subject_vector "
            f"OPTIONS {{indexConfig: {{`vector.dimensions`: {s.dimensions}, "
            "`vector.similarity_function`: 'cosine'}}"
        )
        statements.append(
            f"CREATE FULLTEXT INDEX {s.note_label.lower()}_subject_text IF NOT EXISTS "
            f"FOR (n:{s.note_label}) ON EACH [n.text,n.subject]"
        )
        statements.append(
            f"CREATE VECTOR INDEX {s.group_label.lower()}_exemplar IF NOT EXISTS "
            f"FOR (g:{s.group_label}) ON g.exemplar_vector "
            f"OPTIONS {{indexConfig: {{`vector.dimensions`: {s.dimensions}, "
            "`vector.similarity_function`: 'cosine'}}"
        )
        statements.append(
            f"CREATE FULLTEXT INDEX {s.note_label.lower()}_text IF NOT EXISTS "
            f"FOR (n:{s.note_label}) ON EACH [n.text]"
        )
        for statement in statements:
            self.write(lambda tx, statement=statement: query(tx, statement))
        self.write(lambda tx: query(tx, "CALL db.awaitIndexes($wait)", wait=s.index_wait_seconds))
        self._verify_dimensions()

    def _verify_dimensions(self) -> None:
        """An existing index with different dimensions is a conflict, not a migration."""
        rows = self.read("SHOW INDEXES YIELD name, options RETURN name, options")
        expected = {
            f"{label.lower()}_passage"
            for label in (
                self.settings.note_label,
                self.settings.piece_label,
                self.settings.lesson_label,
            )
        }
        expected.add(f"{self.settings.group_label.lower()}_exemplar")
        expected.add(f"{self.settings.note_label.lower()}_subject")
        for row in rows:
            if row["name"] in expected:
                dimensions = row["options"]["indexConfig"].get("vector.dimensions")
                if dimensions != self.settings.dimensions:
                    raise MemoryConflict("existing vector index has different dimensions")

    def put(self, instance: str, notes: tuple[MemoryNote, ...]) -> int:
        """Insert a bounded batch once; conflicting identities roll back the whole batch."""
        if not instance or len(notes) > self.settings.max_items:
            raise SetupError("empty instance or graph batch exceeds configured limit")
        if len({note.id for note in notes}) != len(notes):
            raise SetupError("graph insertion repeats a note id")
        for note in notes:
            if note.instance != instance:
                raise MemoryDenied("note insertion belongs to another assistant")
            if len(note.text) > self.settings.max_note_characters:
                raise SetupError("note text exceeds configured graph limit")
            for vector in (
                note.query_vector,
                note.passage_vector,
                *(p.vector for p in note.pieces),
            ):
                vector_check(vector, self.settings)
        return self.write(lambda tx: sum(self._insert(tx, note) for note in notes))

    def _insert(self, tx: ManagedTransaction, note: MemoryNote) -> bool:
        label = self.settings.note_label
        if self._deleted(tx, note):
            return False
        values = note.model_dump(mode="json", exclude={"pieces", "facets", "capture_origin"})
        if note.capture_origin is not None:
            values.update(
                capture_mode="marked",
                capture_origin=note.capture_origin.model_dump_json(),
                lesson_of=note.capture_origin.lesson_of,
            )
        key = digest([note.instance, note.id])
        values.update(
            id=key,
            note_id=note.id,
            insertion_sha256=note.fingerprint,
            trust_positive=self.settings.trust_prior_positive,
            trust_negative=self.settings.trust_prior_negative,
            status="active",
            wrong_open=False,
            useful=0,
            not_useful=0,
            placement_pending=True,
            subject_vector=list(note.passage_vector),
            subject_state="pending",
        )
        rows = query(
            tx,
            f"""
MERGE (n:{label} {{id: $key}})
ON CREATE SET n += $props, n.created_at = datetime()
SET n.insertion_lock = true
RETURN n.insertion_sha256 AS fingerprint
""",
            key=key,
            props=values,
        )
        if rows[0]["fingerprint"] != note.fingerprint:
            raise MemoryConflict("note id already has different insertion content")
        # A deletion may have committed while MERGE waited for the note's write lock.
        if self._deleted(tx, note):
            query(tx, f"MATCH (n:{label} {{id:$key}}) DETACH DELETE n", key=key)
            return False
        return self._finish_insert(tx, note, key)

    def _deleted(self, tx: ManagedTransaction, note: MemoryNote) -> bool:
        deleted = query(
            tx,
            f"""
MATCH (c:{self.settings.check_label} {{instance:$instance, note:$note, deleted_note:true}})
RETURN c.before_insertion_sha256 AS fingerprint
""",
            instance=note.instance,
            note=note.id,
        )
        if deleted:
            if any(row["fingerprint"] != note.fingerprint for row in deleted):
                raise MemoryConflict("deleted note id has different insertion content")
            return True
        return False

    def _finish_insert(self, tx: ManagedTransaction, note: MemoryNote, key: str) -> bool:
        label = self.settings.note_label
        # A persistent insertion marker distinguishes new data without rewriting current text.
        counted = query(
            tx,
            f"""
MATCH (n:{label} {{id: $key}})
WITH n, n.insertion_complete IS NULL AS fresh
SET n.insertion_complete = true
REMOVE n.insertion_lock
RETURN fresh
""",
            key=key,
        )[0]["fresh"]
        if counted:
            self.write_pieces(tx, key, note.instance, note.pieces)
            for facet in note.facets:
                group = digest([note.instance, facet.kind, facet.value])
                query(
                    tx,
                    f"""
MATCH (n:{label} {{id: $key}})
MERGE (g:{self.settings.group_label} {{id: $group}})
ON CREATE SET g.instance=$instance, g.kind=$kind, g.value=$value, g.created_at=datetime()
MERGE (n)-[:IN_GROUP]->(g)
""",
                    key=key,
                    group=group,
                    instance=note.instance,
                    kind=facet.kind,
                    value=facet.value,
                )
        return bool(counted)

    def write_pieces(
        self, tx: ManagedTransaction, key: str, instance: str, pieces: tuple[Piece, ...]
    ) -> None:
        """Create pieces inside the insertion/correction transaction after ownership checks."""
        rows = [
            {"id": digest([key, p.offset]), "offset": p.offset, "vector": list(p.vector)}
            for p in pieces
        ]
        query(
            tx,
            f"""
MATCH (n:{self.settings.note_label} {{id:$key}})
UNWIND $rows AS row
CREATE (p:{self.settings.piece_label} {{id:row.id, note_id:n.note_id, instance:$instance,
                                     offset:row.offset, passage_vector:row.vector}})
CREATE (p)-[:PART_OF]->(n)
""",
            key=key,
            instance=instance,
            rows=rows,
        )

    def known(self, instance: str, identity: str) -> bool:
        """A saved extraction already inserted or explicitly deleted needs no embedding."""
        rows = self.read(
            f"""
OPTIONAL MATCH (n:{self.settings.note_label} {{id:$key, instance:$instance}})
OPTIONAL MATCH (c:{self.settings.check_label}
               {{instance:$instance, note:$note, deleted_note:true}})
OPTIONAL MATCH (o:{self.settings.occurrence_label} {{instance:$instance, alias:$note}})
RETURN n IS NOT NULL OR c IS NOT NULL OR o IS NOT NULL AS known
""",
            key=digest([instance, identity]),
            instance=instance,
            note=identity,
        )
        return any(row["known"] for row in rows)

    def note(self, instance: str, identity: str) -> dict[str, Any]:
        """Return only the authenticated assistant's note, with vectors excluded."""
        rows = self.read(
            f"""
MATCH (n:{self.settings.note_label} {{id:$key, instance:$instance}})
OPTIONAL MATCH (n)-[:ALSO_SAID]->(o:{self.settings.occurrence_label})
WITH n,count(CASE WHEN o.verdict='same_claim' THEN o END) AS said_again,
       count(CASE WHEN o.verdict='same_event' THEN o END) AS retold_count
RETURN n {{.*, query_vector:null, passage_vector:null, subject_vector:null, id:n.note_id,
           said_again:said_again,retold_count:retold_count,
           created_at:toString(n.created_at), checked_at:toString(n.checked_at),
           rewritten_at:toString(n.rewritten_at), subject_at:toString(n.subject_at)}} AS note
""",
            key=digest([instance, identity]),
            instance=instance,
        )
        if not rows:
            raise MemoryDenied("memory is absent or belongs to another assistant")
        return json_temporals(rows[0]["note"])


def connect_graph(
    setup: SetupConfig, archive: ArchiveSettings, settings: MemorySettings
) -> GraphStore:
    """Connect only to setup's local Bolt listener with project-keyring credentials."""
    endpoint = setup.services.graph
    host = f"[{endpoint.host}]" if endpoint.host == "::1" else endpoint.host
    user = keyring_secret(setup, archive, settings.keyring_entry, settings.user_field)
    password = keyring_secret(setup, archive, settings.keyring_entry, settings.password_field)
    factory = cast(Callable[..., Driver], cast(Any, GraphDatabase).driver)
    driver = factory(
        f"bolt://{host}:{endpoint.port}",
        auth=(user, password),
        connection_timeout=settings.timeout_seconds,
        connection_acquisition_timeout=settings.timeout_seconds,
        max_transaction_retry_time=settings.timeout_seconds,
    )
    try:
        cast(Callable[[], None], cast(Any, driver).verify_connectivity)()
    except (DriverError, Neo4jError, OSError) as exc:
        driver.close()
        raise SetupError(f"graph connection failed ({type(exc).__name__})") from exc
    return GraphStore(driver, settings)
