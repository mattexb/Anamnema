"""Assistant-scoped topic placement with the source's durable retry marker."""

from __future__ import annotations

import math
from dataclasses import dataclass

from neo4j import ManagedTransaction

from anamnema.archive_models import digest
from anamnema.graph_nearest import owned_nearest
from anamnema.graph_store import GraphStore, MemoryDenied, query, vector_check
from anamnema.setup_config import SetupError


def cosine(left: tuple[float, ...], right: tuple[float, ...]) -> float:
    """Exact cosine for exemplars not yet visible in the approximate index."""
    denominator = math.sqrt(sum(x * x for x in left) * sum(x * x for x in right))
    return sum(x * y for x, y in zip(left, right, strict=True)) / denominator if denominator else 0


@dataclass(frozen=True)
class Placement:
    """Committed memberships, not an inference that insertion completed placement."""

    joined: int = 0
    created: int = 0


class GraphTopics:
    """One placement pass; newly created exemplars are cached separately for each owner."""

    def __init__(self, store: GraphStore) -> None:
        self.store = store
        self.exemplars: dict[tuple[str, str], tuple[float, ...]] = {}

    def place(self, instance: str, identity: str) -> Placement:
        """Lock the note and place its current stored vector, or leave its marker on failure."""

        def work(tx: ManagedTransaction) -> tuple[Placement, tuple[float, ...] | None]:
            rows = query(
                tx,
                f"""
MATCH (n:{self.store.settings.note_label} {{id:$key, instance:$instance}})
SET n.placement_lock = true
RETURN n.query_vector AS vector, coalesce(n.placement_pending,false) AS pending
""",
                key=digest([instance, identity]),
                instance=instance,
            )
            if not rows:
                raise MemoryDenied("topic exemplar is absent or belongs to another assistant")
            result, vector = Placement(), None
            if rows[0]["pending"]:
                vector = tuple(float(x) for x in rows[0]["vector"])
                vector_check(vector, self.store.settings)
                close = self._candidates(tx, instance, vector)
                if close:
                    self._join(tx, instance, identity, close)
                    result, vector = Placement(joined=len(close)), None
                else:
                    self._create(tx, instance, self.group_id(instance, identity), identity, vector)
                    result = Placement(created=1)
            query(
                tx,
                f"MATCH (n:{self.store.settings.note_label} {{id:$key}}) REMOVE n.placement_lock",
                key=digest([instance, identity]),
            )
            return result, vector

        result, vector = self.store.write(work)
        if vector is not None:
            self.exemplars[instance, self.group_id(instance, identity)] = vector
        return result

    @staticmethod
    def group_id(instance: str, identity: str) -> str:
        """Use the full project-scoped identity, not a private synth name prefix."""
        return digest([instance, ["topic", identity]])

    def _candidates(
        self, tx: ManagedTransaction, instance: str, vector: tuple[float, ...]
    ) -> list[tuple[str, float]]:
        s = self.store.settings
        rows = owned_nearest(
            lambda limit: query(
                tx,
                f"""
CYPHER 25
MATCH (g:{s.group_label})
 SEARCH g IN (VECTOR INDEX {s.group_label.lower()}_exemplar FOR $vector LIMIT $count)
 SCORE AS score
RETURN g.id AS id, score, (g.instance=$instance AND g.group_type='topic') AS mine
""",
                instance=instance,
                vector=list(vector),
                count=limit,
            ),
            s.topics.candidates,
        )
        scores = {str(row["id"]): 2.0 * float(row["score"]) - 1.0 for row in rows}
        for (owner, group), exemplar in self.exemplars.items():
            if owner == instance:
                scores[group] = cosine(vector, exemplar)
        return sorted(
            (
                (group, score)
                for group, score in scores.items()
                if score >= s.topics.join_similarity
            ),
            key=lambda pair: (-pair[1], pair[0]),
        )[: s.topics.max_memberships]

    def create(self, instance: str, group: str, exemplar: str, vector: tuple[float, ...]) -> None:
        """Create a topic only when both the exemplar and any existing group are owned."""
        vector_check(vector, self.store.settings)
        self.store.write(lambda tx: self._create(tx, instance, group, exemplar, vector))

    def _create(
        self,
        tx: ManagedTransaction,
        instance: str,
        group: str,
        exemplar: str,
        vector: tuple[float, ...],
    ) -> None:
        rows = query(
            tx,
            f"""
MATCH (n:{self.store.settings.note_label} {{id:$key, instance:$instance}})
MERGE (g:{self.store.settings.group_label} {{id:$group}})
ON CREATE SET g.instance=$instance, g.kind='topic', g.group_type='topic',
              g.exemplar=$exemplar, g.exemplar_vector=$vector, g.created_at=datetime()
WITH n,g WHERE g.instance=$instance AND g.group_type='topic'
MERGE (n)-[r:IN_GROUP]->(g)
ON CREATE SET r.how='topic', r.similarity=1.0, r.at=datetime()
REMOVE n.placement_pending
RETURN count(g) AS written
""",
            key=digest([instance, exemplar]),
            instance=instance,
            group=group,
            exemplar=exemplar,
            vector=list(vector),
        )
        if rows[0]["written"] != 1:
            raise MemoryDenied("topic exemplar/group is absent, foreign or not a topic")

    def join(self, instance: str, identity: str, joins: list[tuple[str, float]]) -> None:
        """Write every membership or none; empty joins cannot clear a retry marker."""
        if not joins:
            return
        if len(joins) > self.store.settings.topics.max_memberships:
            raise SetupError("topic joins exceed configured membership bound")
        if len({group for group, _ in joins}) != len(joins):
            raise SetupError("topic joins repeat a group")
        if any(not math.isfinite(score) or not -1 <= score <= 1 for _, score in joins):
            raise SetupError("topic similarity must be a finite cosine")
        self.store.write(lambda tx: self._join(tx, instance, identity, joins))

    def _join(
        self, tx: ManagedTransaction, instance: str, identity: str, joins: list[tuple[str, float]]
    ) -> None:
        rows = query(
            tx,
            f"""
MATCH (n:{self.store.settings.note_label} {{id:$key, instance:$instance}})
UNWIND $joins AS item
MATCH (g:{self.store.settings.group_label} {{id:item.id, instance:$instance, group_type:'topic'}})
MERGE (n)-[r:IN_GROUP]->(g)
ON CREATE SET r.how='topic', r.similarity=item.score, r.at=datetime()
REMOVE n.placement_pending
RETURN count(g) AS written
""",
            key=digest([instance, identity]),
            instance=instance,
            joins=[{"id": group, "score": round(score, 4)} for group, score in joins],
        )
        if rows[0]["written"] != len(joins):
            raise MemoryDenied("a topic join target is absent or belongs to another assistant")
