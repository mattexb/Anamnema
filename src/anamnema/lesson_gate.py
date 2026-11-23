"""Source lesson admission and route budgets, independent of any private project catalogue."""

from collections.abc import Callable
from pathlib import PurePath
from typing import Any

from anamnema.setup_config import SetupError


def system_of(project: str | None, systems: dict[str, tuple[str, ...]]) -> str | None:
    """A configured external system, not every folder the lesson has ever appeared in."""
    if not project:
        return None
    return next(
        (
            name
            for name, projects in systems.items()
            if project.lower() in {item.lower() for item in projects}
        ),
        None,
    )


def system_here(cwd: str | None, systems: dict[str, tuple[str, ...]]) -> str | None:
    """Resolve path components through operator configuration without private machine paths."""
    if not cwd:
        return None
    return next(
        (found for part in PurePath(cwd).parts if (found := system_of(part, systems))), None
    )


def admitted(
    count: dict[str, int],
    projects: list[str | None],
    here: str | None,
    min_events: int,
    word_kinds: tuple[str, ...],
    systems: dict[str, tuple[str, ...]],
) -> bool:
    """Require consolidation; scope only a lesson evidenced exclusively in one external system."""
    if count.get("events", 0) < min_events and not any(
        count.get(kind, 0) > 0 for kind in word_kinds
    ):
        return False
    named = [project for project in projects if project]
    found = {system_of(project, systems) for project in named}
    return not (named and len(found) == 1 and None not in found and here not in found)


def routes_for(
    routes: list[dict[str, Any]],
    vector: tuple[float, ...],
    limit: int,
    embed: Callable[[list[str]], list[tuple[float, ...]]],
) -> list[dict[str, Any]]:
    """Keep every exception and the closest ordinary routes, in original lesson order."""
    if limit < 0:
        raise SetupError("ordinary lesson route budget must not be negative")
    ordinary = [index for index, route in enumerate(routes) if route.get("exception") is not True]
    if len(ordinary) > limit:
        vectors = embed(
            [
                f"{routes[index].get('condition', '')}: {routes[index].get('correction', '')}"
                for index in ordinary
            ]
        )
        scores = {
            index: sum(x * y for x, y in zip(embedded, vector, strict=True))
            for index, embedded in zip(ordinary, vectors, strict=True)
        }
        ordinary = sorted(ordinary, key=lambda index: -scores[index])[:limit]
    kept = set(ordinary) | {
        index for index, route in enumerate(routes) if route.get("exception") is True
    }
    return [route for index, route in enumerate(routes) if index in kept]
