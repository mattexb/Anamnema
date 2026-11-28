"""A lesson's routes reworded by hand, as a new version with the old one in its history.

A route condition too narrow for the action it names keeps a fitting lesson away: in the
KinTools replay of 2026-11-28 a condition naming only "the full gate" kept the lint-first lesson
from every scoped gate run, and no command could reword it. The reword goes through the same
revision- and evidence-guarded judgement write as a model judgement: every route still cites the
lesson's own episode notes, each note's current source token is checked again, and the passage
embedding is computed from the new text.
"""

import json
from typing import Any

from anamnema.lesson_graph import LessonGraph
from anamnema.lesson_models import (
    LessonDocument,
    LessonEpisode,
    LessonJudgement,
    LessonRoute,
    LessonWrite,
)
from anamnema.search_services import CPUEncoder
from anamnema.setup_config import SetupError

HAND = "hand-reworded"


def reword_routes(
    graph: LessonGraph,
    encoder: CPUEncoder,
    instance: str,
    identity: str,
    routes: tuple[LessonRoute, ...],
    reason: str,
) -> dict[str, Any]:
    """Replace one owned current lesson's routes and return its new version.

    Args:
        graph: The lesson graph.
        encoder: The passage encoder the lesson's embedding is computed with.
        instance: The assistant owning the lesson.
        identity: The lesson id.
        routes: The new routes; each cites notes among the lesson's episodes.
        reason: Why the routes change, kept as the write's model identity.

    Returns:
        ``lesson``, ``version`` and ``routes`` (how many).

    Raises:
        SetupError: No routes, or no reason.
    """
    if not routes or not reason.strip():
        raise SetupError("a reword needs at least one route and a reason")
    current = graph.get(instance, identity)
    # Stored episodes also hold the evidence token the write records; the document takes the rest.
    episodes = tuple(
        LessonEpisode.model_validate_json(
            json.dumps({k: v for k, v in item.items() if k != "content_token"})
        )
        for item in current["episodes"]
    )
    notes = graph.sources(instance, tuple(episode.note for episode in episodes))
    text = " ".join(
        [
            current["statement"],
            current["mechanism"],
            *(f"{route.condition} {route.correction}" for route in routes),
        ]
    )
    model = f"{HAND}: {reason.strip()}"
    document = LessonDocument(
        id=identity,
        statement=current["statement"],
        mechanism=current["mechanism"],
        routes=routes,
        severity=current["severity"],
        status=current["status"],
        conflict=current["conflict"],
        concepts=tuple(current.get("concepts") or ()),
        model=model,
        embedding=encoder.encode([text], passage=True)[0],
        episodes=episodes,
    )
    judgement = LessonJudgement(
        lessons=(LessonWrite(lesson=document, revision=current["revision"]),),
        source_tokens={note["note"]: note["token"] for note in notes},
        model=model,
    )
    version = graph.write_judgement(instance, judgement)[0]
    return {"lesson": identity, "version": version, "routes": len(routes)}
