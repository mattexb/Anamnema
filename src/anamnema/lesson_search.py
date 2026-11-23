"""Owner-widened lesson candidates and exact owner-budget pairing from the reviewed fixes."""

from typing import Any

from anamnema.graph_nearest import owned_nearest
from anamnema.graph_store import GraphStore, vector_check
from anamnema.setup_config import SetupError


class LessonSearch:
    """Invalidated lessons cannot be current candidates; foreign rows never leave the store."""

    def __init__(self, store: GraphStore) -> None:
        self.store = store

    def candidates(self, instance: str, vector: tuple[float, ...], count: int) -> list[str]:
        """Widen past every foreign or invalid candidate, rather than fixed oversampling."""
        self._limit(instance, count)
        vector_check(vector, self.store.settings)
        label = self.store.settings.lesson_label
        rows = owned_nearest(
            lambda budget: self.store.read(
                f"CYPHER 25 MATCH (l:{label}) "
                f"SEARCH l IN (VECTOR INDEX {label.lower()}_passage FOR $vector LIMIT $budget) "
                "SCORE AS score RETURN l.lesson_id AS id, "
                "l.instance=$instance AND l.valid=true AS mine",
                vector=list(vector),
                budget=budget,
                instance=instance,
            ),
            count,
        )
        return [str(row["id"]) for row in rows]

    def pairs(self, instance: str, count: int, floor: float) -> list[tuple[str, str, float]]:
        """Each valid owned lesson's k owned neighbours, with pair deduplication and a floor."""
        self._limit(instance, count)
        if not 0 <= floor <= 1:
            raise SetupError("lesson pair floor must be between zero and one")
        label = self.store.settings.lesson_label
        rows = self.store.read(
            f"MATCH (l:{label} {{instance:$instance,valid:true}}) "
            "WHERE l.passage_vector IS NOT NULL RETURN l.lesson_id AS id,"
            "l.passage_vector AS vector ORDER BY l.lesson_id LIMIT $limit",
            instance=instance,
            limit=self.store.settings.max_items + 1,
        )
        if len(rows) > self.store.settings.max_items:
            raise SetupError("lesson pairing exceeds configured graph batch bound")
        found: dict[tuple[str, str], float] = {}
        for row in rows:
            near = self._neighbours(instance, row, count)
            for hit in near:
                if hit["score"] >= floor:
                    a, b = sorted((str(row["id"]), str(hit["id"])))
                    found[(a, b)] = max(found.get((a, b), 0.0), float(hit["score"]))
        return [(a, b, score) for (a, b), score in sorted(found.items())]

    def _neighbours(self, instance: str, row: dict[str, Any], count: int) -> list[dict[str, Any]]:
        label = self.store.settings.lesson_label
        return owned_nearest(
            lambda budget: self.store.read(
                f"CYPHER 25 MATCH (l:{label}) "
                f"SEARCH l IN (VECTOR INDEX {label.lower()}_passage FOR $vector LIMIT $budget) "
                "SCORE AS score RETURN l.lesson_id AS id, score, "
                "l.instance=$instance AND l.valid=true AND l.lesson_id<>$id AS mine",
                vector=row["vector"],
                budget=budget,
                instance=instance,
                id=row["id"],
            ),
            count,
        )

    def _limit(self, instance: str, count: int) -> None:
        if (
            not instance
            or type(count) is not int
            or not 1 <= count <= self.store.settings.max_items
        ):
            raise SetupError("lesson search requires bounded count and assistant instance")
