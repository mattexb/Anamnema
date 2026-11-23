"""Typed owned briefing selectors; unknown subjects never become empty successful recall."""

from dataclasses import dataclass
from typing import Any, Literal

from anamnema.graph_store import GraphStore
from anamnema.graph_values import json_temporals
from anamnema.setup_config import Nonempty, SetupError, StrictModel


class BriefSelector(StrictModel):
    """An explicit group id, lesson concept name or external note identity."""

    kind: Literal["group", "concept", "note"]
    value: Nonempty

    @property
    def key(self) -> str:
        return f"{self.kind}:{self.value}"


def brief_selector(value: str) -> BriefSelector:
    """Refuse untyped/free-text selectors instead of guessing a subject in a read path."""
    kind, separator, name = value.partition(":")
    if not separator or kind not in {"group", "concept", "note"} or not name.strip():
        raise SetupError("briefing selector must be group:<id>, concept:<name> or note:<id>")
    return BriefSelector.model_validate({"kind": kind, "value": name})


@dataclass(frozen=True)
class BriefSelection:
    resolved: tuple[str, ...]
    unresolved: tuple[str, ...]
    notes: list[dict[str, Any]]


def select_briefing(
    store: GraphStore, instance: str, selectors: tuple[BriefSelector, ...], limit: int
) -> BriefSelection:
    """Enumerate owned membership before generation, refusing rather than truncating a bound."""
    if not instance.strip() or not 1 <= len(selectors) <= store.settings.max_items or limit <= 0:
        raise SetupError("briefing owner, selector count or note limit is invalid")
    keys = [selector.key for selector in selectors]
    if len(keys) != len(set(keys)):
        raise SetupError("briefing selectors repeat")
    groups = [s.value for s in selectors if s.kind == "group"]
    concepts = [s.value for s in selectors if s.kind == "concept"]
    ids = [s.value for s in selectors if s.kind == "note"]
    s = store.settings
    known = store.read(
        f"MATCH (g:{s.group_label} {{instance:$instance}}) WHERE g.id IN $groups "
        "RETURN 'group:'+g.id AS selector UNION "
        f"MATCH (c:{s.concept_label} {{instance:$instance}}) WHERE c.name IN $concepts "
        "RETURN 'concept:'+c.name AS selector UNION "
        f"MATCH (n:{s.note_label} {{instance:$instance}}) WHERE n.note_id IN $ids "
        "RETURN 'note:'+n.note_id AS selector",
        instance=instance,
        groups=groups,
        concepts=concepts,
        ids=ids,
    )
    found = {str(row["selector"]) for row in known}
    notes = _notes(store, instance, groups, concepts, ids, limit)
    return BriefSelection(
        tuple(key for key in keys if key in found),
        tuple(key for key in keys if key not in found),
        _read_order(notes, ids),
    )


def _notes(
    store: GraphStore,
    instance: str,
    groups: list[str],
    concepts: list[str],
    ids: list[str],
    limit: int,
) -> list[dict[str, Any]]:
    s = store.settings
    rows = store.read(
        f"CALL () {{ MATCH (n:{s.note_label} {{instance:$instance}})-[:IN_GROUP]->"
        f"(g:{s.group_label} {{instance:$instance}}) WHERE g.id IN $groups "
        "RETURN n,'group:'+g.id AS via UNION "
        f"MATCH (n:{s.note_label} {{instance:$instance}})-[:EPISODE_OF]->"
        f"(l:{s.lesson_label} {{instance:$instance}})-[:APPLIES_WHEN]->"
        f"(c:{s.concept_label} {{instance:$instance}}) WHERE c.name IN $concepts "
        "RETURN n,'concept:'+c.name AS via UNION "
        f"MATCH (n:{s.note_label} {{instance:$instance}}) WHERE n.note_id IN $ids "
        "RETURN n,'note:'+n.note_id AS via } "
        "WITH n,collect(DISTINCT via) AS via WHERE n.repeat_of IS NULL "
        "RETURN n {.*,id:n.note_id,query_vector:null,passage_vector:null,subject_vector:null,"
        "created_at:toString(n.created_at),checked_at:toString(n.checked_at),"
        "rewritten_at:toString(n.rewritten_at),subject_at:toString(n.subject_at)} AS note,via "
        "ORDER BY n.said_at,n.note_id LIMIT $limit",
        instance=instance,
        groups=groups,
        concepts=concepts,
        ids=ids,
        limit=limit + 1,
    )
    if len(rows) > limit:
        raise SetupError("briefing selection exceeds configured note limit; narrow the selectors")
    return [{**json_temporals(row["note"]), "via": sorted(row["via"])} for row in rows]


def _read_order(notes: list[dict[str, Any]], first: list[str]) -> list[dict[str, Any]]:
    rank = {identity: index for index, identity in enumerate(first)}

    def tier(note: dict[str, Any]) -> int:
        routes = note["via"]
        if any(route.startswith("note:") for route in routes):
            return 0
        return 1 if any(route.startswith("concept:") for route in routes) else 2

    return sorted(notes, key=lambda note: (tier(note), rank.get(str(note["id"]), len(rank))))
