"""Meaning, word and explicit-identifier candidate searches over the public graph."""

from __future__ import annotations

import re
from typing import Literal

from anamnema.archive_models import digest
from anamnema.graph_nearest import owned_nearest
from anamnema.graph_store import GraphStore, vector_check
from anamnema.memory_models import Facet
from anamnema.setup_config import SetupError

LUCENE_SPECIAL = re.compile(r'[+\-&|!(){}\[\]^"~*?:\\/]')
COSINE_SCALE = 2.0


def words_of(question: str, maximum: int) -> str:
    """Escape Lucene operators and cap clauses, preserving the source query words."""
    return " ".join(list(dict.fromkeys(LUCENE_SPECIAL.sub(" ", question).split()))[:maximum])


def fuse(rankings: list[list[str]], k: int) -> dict[str, float]:
    """Source reciprocal-rank fusion: raw scores from different indexes never mix."""
    if k <= 0:
        raise SetupError("rank fusion constant must be positive")
    scores: dict[str, float] = {}
    for ranking in rankings:
        for rank, identity in enumerate(dict.fromkeys(ranking), start=1):
            scores[identity] = scores.get(identity, 0.0) + 1.0 / (k + rank)
    return scores


class GraphSearch:
    """Scoped index access shared by the retrieval core and service."""

    def __init__(self, store: GraphStore) -> None:
        self.store = store

    def meaning(
        self,
        instance: str,
        vector: tuple[float, ...],
        count: int,
        representation: Literal["text", "subject"] = "text",
    ) -> list[tuple[str, float]]:
        """Best note cosine from its own passage and all later overlapping pieces."""
        vector_check(vector, self.store.settings)
        self._limit(instance, count)
        best: dict[str, float] = {}
        indexes = [
            (self.store.settings.note_label, "passage"),
            (self.store.settings.piece_label, "passage"),
        ]
        if representation == "subject":
            indexes.append((self.store.settings.note_label, "subject"))
        for label, suffix in indexes:
            rows = owned_nearest(
                lambda limit, label=label, suffix=suffix: self.store.read(
                    f"""
CYPHER 25
MATCH (n:{label})
 SEARCH n IN (VECTOR INDEX {label.lower()}_{suffix} FOR $vector LIMIT $count) SCORE AS score
RETURN n.note_id AS id, score, n.instance=$instance AS mine
""",
                    vector=list(vector),
                    count=limit,
                    instance=instance,
                ),
                count,
            )
            for row in rows:
                score = COSINE_SCALE * float(row["score"]) - 1.0
                identity = str(row["id"])
                best[identity] = max(score, best.get(identity, score))
        return sorted(best.items(), key=lambda pair: (-pair[1], pair[0]))[:count]

    def words(
        self,
        instance: str,
        words: str,
        count: int,
        representation: Literal["text", "subject"] = "text",
    ) -> list[str]:
        """Return one assistant's full-text candidates, never a raw Lucene user expression."""
        self._limit(instance, count)
        if not words.strip():
            return []
        label = self.store.settings.note_label
        suffix = "subject_text" if representation == "subject" else "text"
        return [
            str(row["id"])
            for row in owned_nearest(
                lambda limit: self.store.read(
                    f"""
CYPHER 25
MATCH (n:{label})
 SEARCH n IN (FULLTEXT INDEX {label.lower()}_{suffix} FOR $words LIMIT $count) SCORE AS score
RETURN n.note_id AS id, n.instance=$instance AS mine
""",
                    words=words,
                    count=limit,
                    instance=instance,
                ),
                count,
            )
        ]

    def identifiers(self, instance: str, facets: tuple[Facet, ...]) -> list[str]:
        """Exact identifier memberships are tenant-scoped, not cross-owner free text."""
        self._limit(instance, max(1, len(facets)))
        groups = [digest([instance, facet.kind, facet.value]) for facet in facets]
        return [
            str(row["id"])
            for row in self.store.read(
                f"""
MATCH (n:{self.store.settings.note_label})-[r:IN_GROUP]->(g:{self.store.settings.group_label})
WHERE n.instance=$instance AND g.instance=$instance AND g.id IN $groups
 AND coalesce(r.fit,'') <> 'incorrect'
RETURN DISTINCT n.note_id AS id ORDER BY id
""",
                instance=instance,
                groups=groups,
            )
        ]

    def similarities(
        self,
        instance: str,
        vector: tuple[float, ...],
        identities: list[str],
        representation: Literal["text", "subject"] = "text",
    ) -> dict[str, float]:
        """Word-only hits still receive their exact semantic relevance before the floor."""
        vector_check(vector, self.store.settings)
        self._limit(instance, max(1, len(identities)))
        subject = (
            "vector.similarity.cosine(n.subject_vector,$vector)"
            if representation == "subject"
            else "null"
        )
        rows = self.store.read(
            f"""
MATCH (n:{self.store.settings.note_label})
WHERE n.instance=$instance AND n.note_id IN $ids
OPTIONAL MATCH (p:{self.store.settings.piece_label})-[:PART_OF]->(n)
WITH n, max(vector.similarity.cosine(p.passage_vector,$vector)) AS piece
WITH n,piece,vector.similarity.cosine(n.passage_vector,$vector) AS original,{subject} AS subject
WITH n, CASE WHEN piece > original THEN piece ELSE original END AS best, subject
RETURN n.note_id AS id, CASE WHEN subject > best THEN subject ELSE best END AS score
""",
            instance=instance,
            ids=identities,
            vector=list(vector),
        )
        return {str(r["id"]): COSINE_SCALE * float(r["score"]) - 1 for r in rows}

    def _limit(self, instance: str, count: int) -> None:
        if not instance or not 1 <= count <= self.store.settings.max_items:
            raise SetupError("empty instance or graph search exceeds configured limit")
