"""Owned entity/card persistence with revision-checked rebuild commits and durable decisions."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any, Literal

from neo4j import ManagedTransaction

from anamnema.archive_models import digest
from anamnema.card_policy import CardPolicy
from anamnema.graph_store import GraphStore, MemoryConflict, MemoryDenied, query
from anamnema.memory_models import MemoryInputError
from anamnema.setup_config import SetupError


class CardPending(SetupError):
    """The current source revision has not produced a readable card yet."""


@dataclass(frozen=True)
class CardInputs:
    """Generation reads one explicit entity revision, notes and persistent human moves."""

    entity: dict[str, Any]
    notes: list[dict[str, Any]]
    moves: list[dict[str, Any]]


class CardGraph:
    """A name resolves only inside its owner; the same name elsewhere is a different entity."""

    def __init__(self, store: GraphStore, policy: CardPolicy) -> None:
        self.store, self.policy = store, policy

    @staticmethod
    def key(instance: str, name: str) -> str:
        """Separate entities from note/topic/facet namespaces without private prefixes."""
        return digest([instance, ["entity", name]])

    def entity(self, instance: str, name: str) -> dict[str, Any]:
        """Return status and revision for a known owned entity, including its dropped state."""
        rows = self.store.read(
            f"MATCH (e:{self.store.settings.entity_label} {{id:$key,instance:$instance}}) "
            "RETURN e {.*,created_at:toString(e.created_at),updated_at:toString(e.updated_at),"
            "deleted_at:toString(e.deleted_at),card_built_at:toString(e.card_built_at),"
            "build_lock:null} AS entity",
            key=self.key(instance, name),
            instance=instance,
        )
        if not rows:
            raise MemoryDenied("entity is absent or belongs to another assistant")
        return rows[0]["entity"]

    def create(self, instance: str, name: str, kind: str, reason: str) -> None:
        """Create a confirmed entity with an accountable reason; a taken name is not replaced."""
        self.validate_operation(instance, name, reason)
        if not kind.strip() or len(kind) > self.store.settings.max_note_characters:
            raise MemoryInputError("entity type is missing or exceeds configured bound")
        request = uuid.uuid4().hex

        def work(tx: ManagedTransaction) -> None:
            rows = query(
                tx,
                f"MERGE (e:{self.store.settings.entity_label} {{id:$key}}) "
                "ON CREATE SET e.instance=$instance,e.name=$name,e.type=$kind,e.aliases=[],"
                "e.status='confirmed',e.revision=0,e.card_valid=false,"
                "e.created_request=$request,e.created_at=datetime(),e.updated_at=datetime() "
                "RETURN e.created_request=$request AS created,e.status AS status",
                key=self.key(instance, name),
                instance=instance,
                name=name,
                kind=kind,
                request=request,
            )
            if not rows[0]["created"]:
                raise MemoryConflict("entity name already taken; dropped names require undrop")
            self.record_event(tx, instance, name, "create", reason, request)

        self.store.write(work)

    def decide(
        self, instance: str, name: str, action: Literal["confirm", "drop", "undrop"], reason: str
    ) -> None:
        """Reasoned lifecycle transitions retain drop history and never restore obsolete links."""
        self.validate_operation(instance, name, reason)
        request = uuid.uuid4().hex

        def work(tx: ManagedTransaction) -> None:
            rows = query(
                tx,
                f"MATCH (e:{self.store.settings.entity_label} "
                "{id:$key,instance:$instance}) SET e.build_lock=true RETURN e.status AS status",
                key=self.key(instance, name),
                instance=instance,
            )
            if not rows:
                raise MemoryDenied("entity decision needs an existing owned entity")
            expected = {"confirm": "unconfirmed", "drop": "confirmed", "undrop": "dropped"}[action]
            if rows[0]["status"] != expected and not (
                action == "drop" and rows[0]["status"] == "unconfirmed"
            ):
                raise MemoryConflict(f"cannot {action} entity in state {rows[0]['status']}")
            status = "dropped" if action == "drop" else "confirmed"
            query(
                tx,
                f"MATCH (e:{self.store.settings.entity_label} {{id:$key}}) "
                "SET e.status=$status,e.updated_at=datetime(),e.revision=e.revision+1,"
                "e.card_valid=false,e.deleted_at=CASE WHEN $drop THEN datetime() ELSE null END,"
                "e.dropped_why=CASE WHEN $drop THEN $reason ELSE null END REMOVE e.build_lock",
                key=self.key(instance, name),
                status=status,
                drop=action == "drop",
                reason=reason,
            )
            if action == "drop":
                query(
                    tx,
                    f"MATCH ()-[r:ABOUT_ENTITY]->(e:{self.store.settings.entity_label} "
                    "{id:$key}) DELETE r",
                    key=self.key(instance, name),
                )
            self.record_event(tx, instance, name, action, reason, request)

        self.store.write(work)

    def link(
        self, instance: str, name: str, note: str, reason: str, pinned: bool | None = None
    ) -> None:
        """A settled owned link survives generated relinking; pin state belongs to that pair."""
        self.validate_operation(instance, name, reason)
        if pinned is not None and type(pinned) is not bool:
            raise SetupError("entity pin must be a JSON boolean")

        def work(tx: ManagedTransaction) -> None:
            rows = query(
                tx,
                f"MATCH (n:{self.store.settings.note_label} "
                "{id:$note,instance:$instance}) SET n.entity_lock=true "
                f"WITH n MATCH (e:{self.store.settings.entity_label} "
                "{id:$entity,instance:$instance}) WHERE e.status<>'dropped' "
                "MERGE (n)-[r:ABOUT_ENTITY]->(e) "
                "SET r.settled=true,r.pinned=coalesce($pinned,r.pinned,false),"
                "r.reason=$reason,r.linked_at=datetime(),e.revision=e.revision+1,"
                "e.card_valid=false REMOVE n.entity_lock RETURN count(e) AS written",
                note=digest([instance, note]),
                entity=self.key(instance, name),
                instance=instance,
                pinned=pinned,
                reason=reason,
            )
            if rows[0]["written"] != 1:
                raise MemoryDenied("link needs an owned note and a live owned entity")

        self.store.write(work)

    def pin(self, instance: str, name: str, note: str, reason: str, pinned: bool) -> None:
        """Pin settles a link; unpin changes an existing pair without creating a new link."""
        self.validate_operation(instance, name, reason)
        if type(pinned) is not bool:
            raise MemoryInputError("entity pin must be a JSON boolean")
        if pinned:
            self.link(instance, name, note, reason, pinned=True)
            return

        def work(tx: ManagedTransaction) -> None:
            rows = query(
                tx,
                f"MATCH (n:{self.store.settings.note_label} "
                "{id:$note,instance:$instance}) SET n.entity_lock=true "
                f"WITH n MATCH (n)-[r:ABOUT_ENTITY]->(e:{self.store.settings.entity_label} "
                "{id:$entity,instance:$instance}) WHERE e.status<>'dropped' "
                "SET r.pinned=false,r.reason=$reason,r.linked_at=datetime(),"
                "e.revision=e.revision+1,e.card_valid=false "
                "REMOVE n.entity_lock RETURN count(e) AS written",
                note=digest([instance, note]),
                entity=self.key(instance, name),
                instance=instance,
                reason=reason,
            )
            if rows[0]["written"] != 1:
                raise MemoryDenied("unpin needs an owned linked note and a live owned entity")

        self.store.write(work)

    def inputs(self, instance: str, name: str) -> CardInputs:
        """Collect bounded linked notes and durable moves; never truncate pins silently."""
        entity = self.entity(instance, name)
        if entity["status"] == "dropped":
            raise MemoryDenied("dropped entity has no current card; use undrop")
        rows = self.store.read(
            f"MATCH (n:{self.store.settings.note_label} {{instance:$instance}})-[r:ABOUT_ENTITY]->"
            f"(e:{self.store.settings.entity_label} {{id:$key}}) RETURN n.note_id AS id,"
            "coalesce(r.pinned,false) AS pinned,coalesce(r.opens,0) AS opens "
            "ORDER BY n.said_at DESC,n.note_id LIMIT $limit",
            instance=instance,
            key=self.key(instance, name),
            limit=self.store.settings.max_items + 1,
        )
        if len(rows) > self.store.settings.max_items:
            raise SetupError("card source count exceeds configured graph limit")
        lines = self._lines(instance, name)
        moves = self.moves(instance, name)
        front = {identity for line in lines if line["front"] for identity in line["notes"]}
        moved = {identity for move in moves for identity in move["notes"]}
        notes = [
            {
                **self.store.note(instance, row["id"]),
                "pinned": row["pinned"],
                "opens": row["opens"],
                "on_front": row["id"] in front,
                "moved": row["id"] in moved,
            }
            for row in rows
        ]
        return CardInputs(entity, notes, moves)

    def moves(self, instance: str, name: str) -> list[dict[str, Any]]:
        """Bounded durable intent, including unmatched decisions requiring settlement."""
        self.entity(instance, name)
        rows = self.store.read(
            f"MATCH (m:{self.store.settings.move_label} {{instance:$instance,entity:$key}}) "
            "RETURN m {.*,at:null} AS move ORDER BY m.at,m.id LIMIT $limit",
            instance=instance,
            key=self.key(instance, name),
            limit=self.store.settings.max_items + 1,
        )
        if len(rows) > self.store.settings.max_items:
            raise SetupError("card move history exceeds configured graph limit")
        return [row["move"] for row in rows]

    def commit(
        self,
        instance: str,
        name: str,
        revision: int,
        lines: list[dict[str, Any]],
        model: str,
        unsettled: tuple[str, ...] = (),
    ) -> None:
        """Reject output generated before a source change; replace statements transactionally."""
        if len(lines) > self.policy.statements * self.policy.factor(name):
            raise SetupError("card statement count exceeds policy")

        def work(tx: ManagedTransaction) -> None:
            rows = query(
                tx,
                f"MATCH (e:{self.store.settings.entity_label} "
                "{id:$key,instance:$instance}) SET e.build_lock=true "
                "RETURN e.revision AS revision,e.status AS status",
                key=self.key(instance, name),
                instance=instance,
            )
            if not rows or rows[0]["status"] == "dropped":
                raise MemoryDenied("card rebuild needs a live owned entity")
            if rows[0]["revision"] != revision:
                raise MemoryConflict("card inputs changed during generation; rebuild pending")
            self._replace(tx, instance, name, revision, lines, model)
            query(
                tx,
                f"MATCH (m:{self.store.settings.move_label} "
                "{instance:$instance,entity:$key}) SET m.unsettled=m.id IN $ids",
                instance=instance,
                key=self.key(instance, name),
                ids=list(unsettled),
            )

        self.store.write(work)

    def _replace(
        self,
        tx: ManagedTransaction,
        instance: str,
        name: str,
        revision: int,
        lines: list[dict[str, Any]],
        model: str,
    ) -> None:
        s, key = self.store.settings, self.key(instance, name)
        query(
            tx,
            f"MATCH (old:{s.statement_label})-[:OF]->(e:{s.entity_label} {{id:$key}}) "
            "DETACH DELETE old",
            key=key,
        )
        for rank, line in enumerate(lines):
            support = list(dict.fromkeys(line["notes"]))
            if not support:
                raise MemoryDenied("card line requires known supporting notes")
            found = query(
                tx,
                f"MATCH (n:{s.note_label} {{instance:$instance}})-[:ABOUT_ENTITY]->"
                f"(e:{s.entity_label} {{id:$key}}) WHERE n.note_id IN $ids "
                "RETURN n.note_id AS id,n.status AS status,n.wrong_open AS wrong",
                instance=instance,
                key=key,
                ids=support,
            )
            if {row["id"] for row in found} != set(support):
                raise MemoryDenied("card support must be linked notes of this assistant")
            if any(row["wrong"] for row in found):
                raise MemoryDenied("wrong-open notes cannot support a current card")
            props = {
                **line,
                "notes": support,
                "id": digest([key, revision, rank, model]),
                "instance": instance,
                "rank": rank,
                "historical": any(row["status"] != "active" for row in found),
            }
            query(
                tx,
                f"MATCH (e:{s.entity_label} {{id:$key}}) CREATE (s:{s.statement_label}) "
                "SET s=$props,s.built_at=datetime() CREATE (s)-[:OF]->(e)",
                key=key,
                props=props,
            )
        query(
            tx,
            f"MATCH (e:{s.entity_label} {{id:$key}}) "
            "SET e.card_valid=true,e.card_built_revision=$revision,e.card_model=$model,"
            "e.card_built_at=datetime() REMOVE e.build_lock",
            key=key,
            revision=revision,
            model=model,
        )

    def card(self, instance: str, name: str, page: int = 1) -> dict[str, Any]:
        """A dirty card is unavailable, never silently returned as the current derived state."""
        entity = self.entity(instance, name)
        if entity["status"] == "dropped":
            raise MemoryDenied("dropped entity has no current card")
        if not entity.get("card_valid") or entity.get("card_built_revision") != entity["revision"]:
            raise CardPending("card rebuild is pending for the current source revision")
        if page < 1:
            raise MemoryInputError("card page must be positive")
        lines = self._lines(instance, name)
        if page == 1:
            shown = [line for line in lines if line["front"]]
        else:
            facts = [line for line in lines if not line["front"]]
            start = (page - 2) * self.policy.page_size
            shown = facts[start : start + self.policy.page_size]
        return {
            "entity": name,
            "revision": entity["revision"],
            "model": entity.get("card_model"),
            "page": page,
            "statements": shown,
            "moves": self.moves(instance, name),
        }

    def _lines(self, instance: str, name: str) -> list[dict[str, Any]]:
        return [
            row["line"]
            for row in self.store.read(
                f"MATCH (s:{self.store.settings.statement_label} {{instance:$instance}})-[:OF]->"
                f"(e:{self.store.settings.entity_label} {{id:$key}}) "
                "RETURN s {.*,built_at:null} AS line ORDER BY s.rank",
                instance=instance,
                key=self.key(instance, name),
            )
        ]

    def history(self, instance: str, name: str) -> list[dict[str, Any]]:
        """Retirement and restoration reasons outlive either transition."""
        self.entity(instance, name)
        return [
            row["event"]
            for row in self.store.read(
                f"MATCH (e:{self.store.settings.entity_label} {{id:$key,instance:$instance}})"
                f"-[:DECIDED_BY]->(v:{self.store.settings.entity_event_label}) "
                "RETURN v {.*,at:toString(v.at)} AS event ORDER BY v.at,v.id",
                key=self.key(instance, name),
                instance=instance,
            )
        ]

    def affected(self, instance: str, check: str) -> list[str]:
        """The correction records affected names before a deleted note loses its links."""
        rows = self.store.read(
            f"MATCH (c:{self.store.settings.check_label} {{id:$id,instance:$instance}}) "
            "RETURN coalesce(c.affected_entities,[]) AS names",
            id=digest([instance, check]),
            instance=instance,
        )
        return list(rows[0]["names"]) if rows else []

    def validate_operation(self, instance: str, name: str, reason: str) -> None:
        """Require accountable bounded fields before any shared card mutation."""
        if not instance or any(
            not value.strip() or len(value) > self.store.settings.max_note_characters
            for value in (name, reason)
        ):
            raise MemoryInputError("entity operation requires bounded instance, name and reason")

    def record_event(
        self,
        tx: ManagedTransaction,
        instance: str,
        name: str,
        action: str,
        reason: str,
        request: str,
    ) -> None:
        """Persist a reason inside the caller's existing transaction."""
        query(
            tx,
            f"MATCH (e:{self.store.settings.entity_label} {{id:$key}}) "
            f"MERGE (v:{self.store.settings.entity_event_label} {{id:$request}}) "
            "ON CREATE SET v.instance=$instance,v.action=$action,v.reason=$reason,v.at=datetime() "
            "MERGE (e)-[:DECIDED_BY]->(v)",
            key=self.key(instance, name),
            request=request,
            instance=instance,
            action=action,
            reason=reason,
        )
