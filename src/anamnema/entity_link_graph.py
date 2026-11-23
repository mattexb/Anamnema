"""Persist automatic entity links: only on the text the linker judged, never over a person's answer.

Port of the source write path at KinTools 1cac580 (graph_entity_links.write_entity_links and
entity_feed.link_notes), on this project's schema. A linker row lands only while the note still
holds the exact text that was judged; a stale row writes nothing and the note stays due. A link
settled by hand (``settled``) survives every relink. A note is due again when its text changes,
so a rewrite needs no separate invalidation: the linked text is recorded with the link version.
"""

import json
from typing import Any

from neo4j import ManagedTransaction

from anamnema.archive_models import digest
from anamnema.card_graph import CardGraph
from anamnema.entity_link_policy import EntityLinkPolicy
from anamnema.entity_link_writer import (
    EntityLink,
    EntityLinkWriter,
    EntityProposal,
    RosterEntity,
)
from anamnema.graph_store import GraphStore, query


class EntityLinkGraph:
    """Owned selection, unconfirmed proposals and the guarded link write for one assistant."""

    def __init__(self, store: GraphStore, policy: EntityLinkPolicy) -> None:
        self.store, self.policy = store, policy
        self.note, self.entity = store.settings.note_label, store.settings.entity_label

    def roster(self, instance: str) -> tuple[RosterEntity, ...]:
        """Live entities, unconfirmed included; a dropped name is never offered again."""
        rows = self.store.read(
            f"MATCH (e:{self.entity} {{instance:$instance}}) WHERE e.status<>'dropped' "
            "RETURN e.name AS name,e.type AS type,coalesce(e.aliases,[]) AS aliases,"
            "e.status AS status ORDER BY e.name LIMIT $limit",
            instance=instance,
            limit=self.policy.max_roster_entities,
        )
        return tuple(
            RosterEntity(
                name=row["name"],
                type=row["type"],
                aliases=tuple(row["aliases"]),
                status=row["status"],
            )
            for row in rows
        )

    def pending(self, instance: str, limit: int) -> list[dict[str, Any]]:
        """Active notes unlinked under this version or their current text; least tried first."""
        return self.store.read(
            f"MATCH (n:{self.note} {{instance:$instance}}) "
            "WHERE n.insertion_complete=true AND n.status='active' "
            "AND NOT coalesce(n.wrong_open,false) "
            "AND (coalesce(n.entity_link_version,'')<>$version "
            "     OR coalesce(n.entity_linked_text,'')<>n.text) "
            "RETURN n.note_id AS id,n.text AS text,n.kind AS kind,n.said_at AS said_at "
            "ORDER BY coalesce(n.entity_link_attempts,0),n.note_id LIMIT $limit",
            instance=instance,
            version=self.policy.version,
            limit=limit,
        )

    def propose(self, instance: str, proposals: tuple[EntityProposal, ...]) -> list[str]:
        """Create each free name as unconfirmed; a taken or dropped name is left as it is."""
        rows = [
            {"key": CardGraph.key(instance, p.name), "name": p.name, "type": p.type}
            for p in proposals
        ]
        if not rows:
            return []

        def work(tx: ManagedTransaction) -> list[str]:
            created = query(
                tx,
                f"UNWIND $rows AS p MERGE (e:{self.entity} {{id:p.key}}) "
                "ON CREATE SET e.instance=$instance,e.name=p.name,e.type=p.type,e.aliases=[],"
                "e.status='unconfirmed',e.revision=0,e.card_valid=false,e.proposed=true,"
                "e.created_at=datetime(),e.updated_at=datetime(),e.fresh=true "
                "WITH e WHERE e.fresh=true REMOVE e.fresh RETURN e.name AS name",
                rows=rows,
                instance=instance,
            )
            return [row["name"] for row in created]

        return self.store.write(work)

    def write(self, instance: str, links: tuple[EntityLink, ...], model: str) -> int:
        """Replace generated links on each note still holding the judged text; the notes written."""
        rows = [
            {
                "key": digest([instance, link.note]),
                "text": link.text,
                "entities": [
                    CardGraph.key(instance, name)
                    for name in sorted(set(link.entities) | set(link.proposed))
                ],
                "ambiguous": [
                    json.dumps(a.model_dump(mode="json"), sort_keys=True) for a in link.ambiguous
                ],
            }
            for link in links
        ]
        if not rows:
            return 0

        def work(tx: ManagedTransaction) -> int:
            result = query(
                tx,
                _WRITE.format(note=self.note, entity=self.entity),
                rows=rows,
                instance=instance,
                version=self.policy.version,
                model=model,
            )
            return int(result[0]["written"]) if result else 0

        return self.store.write(work)

    def attempted(self, instance: str, notes: list[str]) -> None:
        """Rotate notes the model left unanswered behind untouched ones, without retiring them."""
        if not notes:
            return

        def work(tx: ManagedTransaction) -> None:
            query(
                tx,
                f"UNWIND $keys AS key MATCH (n:{self.note} {{id:key,instance:$instance}}) "
                "SET n.entity_link_attempts=coalesce(n.entity_link_attempts,0)+1",
                keys=[digest([instance, identity]) for identity in notes],
                instance=instance,
            )

        self.store.write(work)


_WRITE = """
UNWIND $rows AS l
MATCH (n:{note} {{id:l.key, instance:$instance}})
WHERE n.text = l.text AND n.status = 'active'
OPTIONAL MATCH (n)-[old:ABOUT_ENTITY]->(gone:{entity})
WHERE NOT coalesce(old.settled, false)
WITH n, l, collect(old) AS olds, collect(DISTINCT gone) AS touched
FOREACH (r IN olds | DELETE r)
FOREACH (e IN touched | SET e.card_valid=false, e.revision=e.revision+1, e.updated_at=datetime())
SET n.entity_ambiguous = l.ambiguous, n.entity_link_version = $version,
    n.entity_linked_text = n.text, n.entity_link_model = $model, n.entity_linked_at = datetime()
WITH n, l
UNWIND (CASE WHEN size(l.entities) = 0 THEN [null] ELSE l.entities END) AS key
OPTIONAL MATCH (e:{entity} {{id:key, instance:$instance}})
WHERE e.status <> 'dropped' AND NOT EXISTS {{ MATCH (n)-[s:ABOUT_ENTITY]->(e) WHERE s.settled }}
FOREACH (_ IN CASE WHEN e IS NULL THEN [] ELSE [1] END |
    MERGE (n)-[r:ABOUT_ENTITY]->(e)
    SET r.settled = false, r.pinned = false, r.version = $version, r.model = $model,
        r.linked_at = datetime(), e.card_valid = false, e.revision = e.revision + 1,
        e.updated_at = datetime())
RETURN count(DISTINCT n) AS written
"""


def link_pending(
    graph: EntityLinkGraph, writer: EntityLinkWriter, instance: str, limit: int
) -> dict[str, Any]:
    """Judge due notes in policy-sized batches and persist; problems are reported, not raised."""
    notes = graph.pending(instance, limit)
    report: dict[str, Any] = {"notes": len(notes), "written": 0, "created": [], "problems": []}
    batch = graph.policy.batch
    for start in range(0, len(notes), batch):
        chunk = notes[start : start + batch]
        linking = writer.judge(chunk, graph.roster(instance))
        report["created"] += graph.propose(instance, linking.proposals)
        report["written"] += graph.write(instance, linking.links, linking.model)
        answered = {link.note for link in linking.links}
        graph.attempted(instance, [str(n["id"]) for n in chunk if str(n["id"]) not in answered])
        report["problems"] += list(linking.problems)
    return report
