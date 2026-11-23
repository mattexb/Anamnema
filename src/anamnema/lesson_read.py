"""Current evidence, real-event admission and counterexample votes for owned lessons."""

from typing import Any

from anamnema.archive_models import digest
from anamnema.graph_store import MemoryDenied
from anamnema.lesson_evidence import Episode, event_count, vote
from anamnema.lesson_gate import admitted, system_here
from anamnema.lesson_graph import LessonGraph
from anamnema.lesson_models import NOTE_CONTENT_FIELDS
from anamnema.lesson_policy import LessonPolicy


class LessonReader:
    """Keep audit evidence visible but prevent stale/wrong/deleted episodes from voting."""

    def __init__(self, graph: LessonGraph, policy: LessonPolicy) -> None:
        self.graph, self.policy = graph, policy

    def read(
        self, instance: str, identity: str, *, historical: bool = False, cwd: str | None = None
    ) -> dict[str, Any]:
        """Explicit reads retain lesson history; admission describes whether it may be delivered."""
        result = self.graph.get(instance, identity, current=not historical)
        episodes = [self._evidence(instance, item) for item in result["episodes"]]
        against = [self._evidence(instance, item) for item in result.get("against", [])]
        standing = [
            Episode(
                note=item["note"],
                lesson=identity,
                evidence=item["evidence"],
                event=item["event"],
                records=tuple(item["records"]),
                event_reason=item["event_reason"],
            )
            for item in episodes
            if not item["held_back"]
        ]
        counts = event_count(standing, identity, self.policy.evidence_types)
        voted = vote(
            {"id": identity, "episodes": episodes, "against": against}, self.policy.unbacked_prefix
        )
        projects = self._projects(
            instance, [item["note"] for item in episodes if not item["held_back"]]
        )
        allowed = bool(result["valid"]) and result["status"] == "reviewed" and not voted["stopped"]
        allowed = allowed and admitted(
            counts,
            projects,
            system_here(cwd, self.policy.system_projects),
            self.policy.minimum_events,
            self.policy.word_kinds,
            self.policy.system_projects,
        )
        self.graph.assert_revision(instance, identity, result["revision"], current=not historical)
        return {
            **result,
            "episodes": episodes,
            "against": against,
            "events": counts,
            "vote": voted,
            "admitted": allowed,
        }

    def _evidence(self, instance: str, evidence: dict[str, Any]) -> dict[str, Any]:
        try:
            note = self.graph.store.note(instance, evidence["note"])
        except MemoryDenied:
            return {**evidence, "held_back": True, "held_reason": "source note absent"}
        current = digest({field: note[field] for field in NOTE_CONTENT_FIELDS})
        stale = evidence.get("content_token") != current
        held = stale or note["wrong_open"] or note["status"] != "active"
        return {
            **evidence,
            "held_back": bool(held),
            "held_reason": "source changed or is not current eligible evidence" if held else "",
        }

    def _projects(self, instance: str, notes: list[str]) -> list[str | None]:
        s = self.graph.store.settings
        rows = self.graph.store.read(
            f"MATCH (n:{s.note_label} {{instance:$instance}}) WHERE n.note_id IN $notes "
            f"OPTIONAL MATCH (n)-[:IN_GROUP]->(g:{s.group_label} {{kind:'project'}}) "
            "RETURN g.value AS project",
            instance=instance,
            notes=notes,
        )
        return [row["project"] for row in rows]
