"""Source event equivalence and lesson votes, with source and judged bases kept distinct."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from anamnema.setup_config import SetupError


@dataclass(frozen=True)
class Episode:
    """One note's lesson evidence, immutable source references and judged equivalence label."""

    note: str
    lesson: str
    evidence: str
    event: str
    records: tuple[str, ...] = ()
    event_reason: str = ""


@dataclass(frozen=True)
class EventClass:
    """One event with auditable source-backed or model-judged equivalence."""

    lesson: str
    notes: tuple[str, ...]
    evidence: tuple[str, ...]
    records: tuple[str, ...]
    basis: Literal["source", "judged"]
    reasons: tuple[str, ...]


def event_classes(episodes: list[Episode], lesson: str) -> list[EventClass]:
    """Shared records merge first; a same-label join across sources remains explicitly judged."""
    mine = [episode for episode in episodes if episode.lesson == lesson]
    parent = list(range(len(mine)))
    judged: set[int] = set()

    def root(index: int) -> int:
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index

    def join(a: int, b: int, by_judge: bool) -> None:
        first, second = root(a), root(b)
        if first != second:
            parent[second] = first
            if by_judge or second in judged:
                judged.add(first)

    for index, episode in enumerate(mine):
        for other in range(index + 1, len(mine)):
            if set(episode.records) & set(mine[other].records):
                join(index, other, False)
    for index, episode in enumerate(mine):
        for other in range(index + 1, len(mine)):
            if episode.event == mine[other].event and root(index) != root(other):
                join(index, other, True)
    groups: dict[int, list[Episode]] = {}
    for index, episode in enumerate(mine):
        groups.setdefault(root(index), []).append(episode)
    result = [
        EventClass(
            lesson,
            tuple(sorted({episode.note for episode in members})),
            tuple(sorted({episode.evidence for episode in members})),
            tuple(sorted({record for episode in members for record in episode.records})),
            "judged" if head in judged else "source",
            tuple(sorted({episode.event_reason for episode in members if episode.event_reason}))
            if head in judged
            else (),
        )
        for head, members in groups.items()
    ]
    return sorted(result, key=lambda event: event.notes)


def event_count(episodes: list[Episode], lesson: str, kinds: tuple[str, ...]) -> dict[str, int]:
    """Count each class once, using the configured strongest-first evidence categories."""
    classes = event_classes(episodes, lesson)
    count = {"events": len(classes), "judged": sum(event.basis == "judged" for event in classes)}
    count.update(dict.fromkeys(kinds, 0))
    for event in classes:
        kind = next((kind for kind in kinds if kind in event.evidence), event.evidence[0])
        count[kind] = count.get(kind, 0) + 1
    return count


def vote(lesson: dict[str, Any], unbacked_prefix: str) -> dict[str, Any]:
    """Source-backed counterexamples remove their event from support; delivery is no vote."""
    if not unbacked_prefix:
        raise SetupError("lesson vote requires the configured unbacked-record prefix")
    key = str(lesson["id"])
    support = [
        Episode(
            str(item["note"]),
            key,
            str(item["evidence"]),
            str(item["event"]),
            tuple(item.get("records") or ()),
        )
        for item in lesson["episodes"]
        if not item.get("held_back")
    ]
    against = [
        Episode(
            str(item["note"]),
            key,
            "against",
            "against:" + str(item["note"]),
            tuple(item.get("records") or ()),
        )
        for item in lesson.get("against", [])
        if not item.get("held_back")
    ]
    counter = [event for event in event_classes(against, key) if _backed(event, unbacked_prefix)]
    taken = {record for event in counter for record in event.records}
    events = [event for event in event_classes(support, key) if _backed(event, unbacked_prefix)]
    standing = [event for event in events if not taken & set(event.records)]
    return {
        "support": len(standing),
        "against": len(counter),
        "stopped": len(counter) > len(standing),
    }


def _backed(event: EventClass, unbacked_prefix: str) -> bool:
    return any(not record.startswith(unbacked_prefix) for record in event.records)
