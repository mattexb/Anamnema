"""Source owner-budget widening, shared by vector and full-text candidate searches."""

from collections.abc import Callable
from typing import Any

WIDEN = 4


def owned_nearest(fetch: Callable[[int], list[dict[str, Any]]], count: int) -> list[dict[str, Any]]:
    """Keep owned candidates, widening until the requested count or index exhaustion.

    Fixed global oversampling cannot guarantee an owner's candidate budget. The
    underlying index still supplies approximate nearest-neighbour results.
    """
    if count <= 0:
        return []
    limit = count
    while True:
        rows = fetch(limit)
        owned = [row for row in rows if row["mine"] is True]
        if len(owned) >= count or len(rows) < limit:
            return owned[:count]
        limit *= WIDEN
