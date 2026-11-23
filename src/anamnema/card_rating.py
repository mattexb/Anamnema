"""Source card fact selection and ordering; human moves are not attached to ephemeral lines."""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from anamnema.card_policy import CardPolicy
from anamnema.memory_config import RatingSettings
from anamnema.memory_rating import NoteRating, effective_trust

RAISED, WHO, RATED, LOWERED = 0, 1, 2, 3
SCORE_DIGITS = 4


@dataclass(frozen=True)
class RankedCard:
    """Ordered copied statement dictionaries and moves requiring explicit settlement."""

    statements: list[dict[str, Any]]
    unsettled: list[dict[str, Any]]


class CardRanker:
    """A fixed as-of time makes fact ranking reproducible and independent of recall relevance."""

    def __init__(self, policy: CardPolicy, ratings: RatingSettings, now: datetime) -> None:
        self.policy, self.ratings, self.now = policy, ratings, now

    def note_value(self, note: dict[str, Any]) -> float:
        """Trust times real repeats times original age; a retelling cannot refresh an event."""
        if note.get("status", "active") != "active":
            return 0.0
        value = note.get("event_time") or note.get("said_at")
        age = self.policy.age_floor
        if value:
            when = datetime.fromisoformat(str(value))
            when = when.replace(tzinfo=when.tzinfo or UTC)
            days = max((self.now - when).days, 0)
            age = max(age, 0.5 ** (days / self.policy.age_half_days))
        rating = NoteRating(
            float(note["trust_positive"]),
            float(note["trust_negative"]),
            bool(note.get("wrong_open", False)),
            str(note.get("status", "active")),
            int(note.get("useful", 0)),
            int(note.get("not_useful", 0)),
            None,
        )
        repeated = 1 + math.log2(1 + int(note.get("said_again") or 0))
        return effective_trust(rating, self.ratings) * repeated * age

    def line_score(self, notes: list[dict[str, Any]]) -> float:
        """Identical text counts once; at most the best note of each window adds weight."""
        copies: dict[str, tuple[float, dict[str, Any]]] = {}
        for note in notes:
            value = self.note_value(note)
            text = " ".join(str(note.get("text") or note["id"]).lower().split())
            if text not in copies or value > copies[text][0]:
                copies[text] = value, note
        sources: dict[str, float] = {}
        for value, note in copies.values():
            source = str(note.get("window") or note["id"])
            sources[source] = max(sources.get(source, 0.0), value)
        score = sum(sources.values())
        if score <= 0:
            return 0.0
        opens = max((int(note.get("opens") or 0) for note in notes), default=0)
        return score + self.policy.open_weight * min(opens, self.policy.open_cap)

    def pick(self, name: str, notes: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Keep pins, moves and existing front support; split remaining room by age and value."""
        limit = self.policy.notes * self.policy.factor(name)
        kept = {
            str(note["id"])
            for note in notes
            if note.get("pinned") or note.get("moved") or note.get("on_front")
        }
        room = max(limit - len(kept), 0)
        rest = [note for note in notes if str(note["id"]) not in kept]
        newest = rest[: round(room * self.policy.newest_share)]
        kept.update(str(note["id"]) for note in newest)
        rest = [note for note in rest if str(note["id"]) not in kept]
        best = sorted(rest, key=lambda note: (-self.note_value(note), str(note["id"])))
        kept.update(str(note["id"]) for note in best[: room - len(newest)])
        return [note for note in notes if str(note["id"]) in kept]

    def order(
        self,
        statements: list[dict[str, Any]],
        notes: list[dict[str, Any]],
        moves: list[dict[str, Any]],
    ) -> RankedCard:
        """Human-raised, identity, rated, human-lowered; ambiguous moves never apply by guess."""
        statements = [dict(statement) for statement in statements]
        by_id = {str(note["id"]): note for note in notes}
        tiers, unsettled = self._tiers(statements, moves)
        for index, statement in enumerate(statements):
            behind = [by_id[identity] for identity in statement["notes"] if identity in by_id]
            statement["score"] = round(self.line_score(behind), SCORE_DIGITS)
            statement["moved"] = tiers[index] != RATED
            if tiers[index] == RATED and any(note.get("pinned") for note in behind):
                tiers[index] = RAISED
            elif tiers[index] == RATED and statement["front"]:
                tiers[index] = WHO
            statement["who"] = tiers[index] == WHO
        ranked = sorted(
            range(len(statements)),
            key=lambda i: (tiers[i], -statements[i]["score"], statements[i]["text"]),
        )
        for place, index in enumerate(ranked):
            statements[index]["front"] = place < self.policy.front and tiers[index] != LOWERED
        return RankedCard([statements[index] for index in ranked], unsettled)

    @staticmethod
    def _tiers(
        statements: list[dict[str, Any]], moves: list[dict[str, Any]]
    ) -> tuple[list[int], list[dict[str, Any]]]:
        tiers = [RATED] * len(statements)
        unsettled: list[dict[str, Any]] = []
        for move in moves:
            same = [
                i for i, statement in enumerate(statements) if statement["text"] == move["text"]
            ]
            marked = set(move.get("notes") or [])
            near = [i for i, statement in enumerate(statements) if marked & set(statement["notes"])]
            found = same or near
            if len(found) == 1:
                tiers[found[0]] = RAISED if move["front"] else LOWERED
            else:
                unsettled.append(move)
        return tiers, unsettled
