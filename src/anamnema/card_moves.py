"""Atomic owned card decisions, ported from the reviewed 0.7.23 transaction boundary."""

from __future__ import annotations

import uuid
from typing import Any

from neo4j import ManagedTransaction

from anamnema.card_graph import CardGraph, CardPending
from anamnema.graph_store import MemoryDenied, query
from anamnema.memory_models import MemoryInputError


class CardMoves:
    """Visible placement and durable intent must never commit separately."""

    def __init__(self, graph: CardGraph) -> None:
        self.graph = graph
        self.store = graph.store

    def move(
        self, instance: str, name: str, statement: str, reason: str, front: bool
    ) -> dict[str, Any]:
        """Move a unique owned prefix, retain source-backed intent and enforce the front cap."""
        self.graph.validate_operation(instance, name, reason)
        if type(front) is not bool or not statement.strip():
            raise MemoryInputError("card move requires a statement and a boolean direction")
        if len(statement) > self.store.settings.max_note_characters:
            raise MemoryInputError("statement prefix exceeds configured bound")
        move_id = uuid.uuid4().hex

        def work(tx: ManagedTransaction) -> dict[str, Any]:
            self._lock(tx, instance, name, current=True)
            rows = query(
                tx,
                f"MATCH (s:{self.store.settings.statement_label} {{instance:$instance}})"
                f"-[:OF]->(e:{self.store.settings.entity_label} {{id:$key}}) "
                "WHERE s.id STARTS WITH $prefix "
                "RETURN s {.*,built_at:null} AS line",
                instance=instance,
                key=self.graph.key(instance, name),
                prefix=statement,
            )
            if len(rows) != 1:
                raise MemoryDenied("card move needs one unambiguous owned statement")
            line = rows[0]["line"]
            query(
                tx,
                f"MATCH (s:{self.store.settings.statement_label} {{id:$id,instance:$instance}}) "
                f"MATCH (o:{self.store.settings.statement_label})-[:OF]->"
                f"(e:{self.store.settings.entity_label} {{id:$key}}) "
                "WITH s,max(o.rank) AS last "
                "SET s.front=$front,s.rank=CASE WHEN $front THEN last+1 ELSE s.rank END,"
                "s.moved=true,s.moved_by=$instance,s.moved_reason=$reason,"
                "s.moved_at=toString(datetime())",
                id=line["id"],
                instance=instance,
                key=self.graph.key(instance, name),
                front=front,
                reason=reason,
            )
            self._record(tx, instance, name, line, front, reason, move_id)
            bumped = self._cap(tx, instance, name) if front else None
            self._advance(tx, instance, name, current=True)
            return {"moved": line["id"], "front": front, "bumped": bumped, "move": move_id}

        return self.store.write(work)

    def _lock(self, tx: ManagedTransaction, instance: str, name: str, current: bool) -> None:
        rows = query(
            tx,
            f"MATCH (e:{self.store.settings.entity_label} {{id:$key,instance:$instance}}) "
            "SET e.build_lock=true RETURN e.status AS status,e.revision AS revision,"
            "e.card_valid AS valid,e.card_built_revision AS built",
            key=self.graph.key(instance, name),
            instance=instance,
        )
        if not rows or rows[0]["status"] == "dropped":
            raise MemoryDenied("card move needs a live owned entity")
        row = rows[0]
        if current and (not row["valid"] or row["built"] != row["revision"]):
            raise CardPending("card rebuild is pending; cannot move stale statements")

    def _record(
        self,
        tx: ManagedTransaction,
        instance: str,
        name: str,
        line: dict[str, Any],
        front: bool,
        reason: str,
        identity: str,
    ) -> None:
        query(
            tx,
            f"MATCH (m:{self.store.settings.move_label} {{instance:$instance,entity:$key}}) "
            "WHERE m.text=$text OR (coalesce(m.unsettled,false) "
            "AND any(n IN m.notes WHERE n IN $notes)) DETACH DELETE m",
            instance=instance,
            key=self.graph.key(instance, name),
            text=line["text"],
            notes=line["notes"],
        )
        query(
            tx,
            f"CREATE (m:{self.store.settings.move_label} "
            "{id:$id,instance:$instance,entity:$key,text:$text,notes:$notes,front:$front,"
            "reason:$reason,unsettled:false,at:datetime()})",
            id=identity,
            instance=instance,
            key=self.graph.key(instance, name),
            text=line["text"],
            notes=line["notes"],
            front=front,
            reason=reason,
        )

    def _cap(self, tx: ManagedTransaction, instance: str, name: str) -> str | None:
        rows = query(
            tx,
            f"MATCH (s:{self.store.settings.statement_label} {{instance:$instance,front:true}})"
            f"-[:OF]->(e:{self.store.settings.entity_label} {{id:$key}}) "
            "WITH collect(s) AS lines WHERE size(lines)>$cap UNWIND lines AS s "
            "WITH s ORDER BY CASE WHEN coalesce(s.moved,false) THEN 1 ELSE 0 END,s.rank DESC "
            "LIMIT 1 SET s.front=false,s.moved_by='cap',s.moved_at=toString(datetime()) "
            "RETURN s.id AS id",
            instance=instance,
            key=self.graph.key(instance, name),
            cap=self.graph.policy.front,
        )
        return rows[0]["id"] if rows else None

    def _advance(self, tx: ManagedTransaction, instance: str, name: str, current: bool) -> None:
        query(
            tx,
            f"MATCH (e:{self.store.settings.entity_label} {{id:$key,instance:$instance}}) "
            "SET e.revision=e.revision+1,e.card_valid=$current "
            "SET e.card_built_revision=CASE WHEN $current THEN e.revision "
            "ELSE e.card_built_revision END REMOVE e.build_lock",
            key=self.graph.key(instance, name),
            instance=instance,
            current=current,
        )

    def forget(self, instance: str, name: str, identity: str, reason: str) -> dict[str, Any]:
        """Forget exact owned intent; require rebuilding before serving the card again."""
        self.graph.validate_operation(instance, name, reason)

        def work(tx: ManagedTransaction) -> dict[str, Any]:
            self._lock(tx, instance, name, current=False)
            rows = query(
                tx,
                f"MATCH (m:{self.store.settings.move_label} "
                "{id:$id,instance:$instance,entity:$key}) DETACH DELETE m RETURN count(*) AS count",
                id=identity,
                instance=instance,
                key=self.graph.key(instance, name),
            )
            if rows[0]["count"] != 1:
                raise MemoryDenied("forget requires an existing move of this owned entity")
            self.graph.record_event(tx, instance, name, "forget-move", reason, uuid.uuid4().hex)
            self._advance(tx, instance, name, current=False)
            return {"forgotten": identity, "rebuild_pending": True}

        return self.store.write(work)
