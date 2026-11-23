"""Source-derived trust, freshness and usefulness; relevance remains separate."""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime

from anamnema.memory_config import RatingSettings

SECONDS_PER_DAY = 86_400


@dataclass(frozen=True)
class NoteRating:
    """Inputs to the source Beta reputation and question-specific priority."""

    positive: float
    negative: float
    wrong_open: bool
    status: str
    useful: int
    not_useful: int
    event_time: datetime | None


def evidence(outcome: str, settings: RatingSettings) -> tuple[float, float]:
    """Return one check's positive and negative evidence."""
    if outcome == "right":
        return settings.right_weight, 0.0
    if outcome == "partly":
        half = settings.partly_weight / 2
        return half, half
    if outcome == "wrong":
        return 0.0, settings.wrong_weight
    return 0.0, 0.0


def effective_trust(rating: NoteRating, settings: RatingSettings) -> float:
    """Keep an unresolved wrong finding below the floor independently of age."""
    total = rating.positive + rating.negative
    value = rating.positive / total if total else 0.0
    if rating.wrong_open:
        return min(value, settings.floor - settings.wrong_cap_margin)
    return value


def priority(rating: NoteRating, now: datetime, about_now: bool, settings: RatingSettings) -> float:
    """Return the source's bounded priority, not a relevance or truth claim."""
    value = effective_trust(rating, settings)
    if about_now and rating.event_time is not None:
        age = max(0.0, (now - rating.event_time).total_seconds() / SECONDS_PER_DAY)
        value += settings.freshness_boost * math.pow(2.0, -age / settings.freshness_half_life_days)
    usefulness = (1 + rating.useful) / (2 + rating.useful + rating.not_useful)
    value += settings.usefulness_weight * (usefulness - 0.5)
    if about_now and rating.status == "outdated":
        value -= settings.stale_penalty
    value = min(1.0, max(0.0, value))
    if rating.wrong_open:
        value = min(value, settings.floor - settings.wrong_cap_margin)
    return round(100 * value, 1)
