"""Lessons that fit an action, chosen by fit rather than by a fixed count.

Every owned lesson whose passage is near the action text and that is admitted for delivery
comes back, nearest first. There is no cap on how many fit: a fixed count of two had
dropped lessons that applied (lesson-delivery fix, 2026-11-27). The candidate bound only
limits the vector search; the hook's byte budget bounds what reaches the session.
"""

from importlib.resources import files
from pathlib import Path
from typing import Annotated, Any

from pydantic import Field, ValidationError

from anamnema.condition_judge import ConditionCheck
from anamnema.graph_store import GraphStore
from anamnema.lesson_graph import LessonGraph, LessonPending
from anamnema.lesson_policy import LessonPolicy
from anamnema.lesson_read import LessonReader
from anamnema.lesson_search import LessonSearch
from anamnema.memory_models import MemoryInputError
from anamnema.search_services import CPUEncoder
from anamnema.setup_config import SetupError, StrictModel


class ActionPolicy(StrictModel):
    """How far the search looks and how near a lesson must be to fit."""

    candidates: Annotated[int, Field(gt=0)]
    min_similarity: Annotated[float, Field(ge=0, le=1, allow_inf_nan=False)]
    max_action_characters: Annotated[int, Field(gt=0)]


def action_policy(path: Path | None = None) -> ActionPolicy:
    """Read the packaged policy or an explicit replacement."""
    try:
        raw = (
            path.read_text(encoding="utf-8")
            if path
            else files("anamnema").joinpath("config/action_lessons.json").read_text("utf-8")
        )
        return ActionPolicy.model_validate_json(raw)
    except (OSError, UnicodeError, ValidationError) as exc:
        raise SetupError(f"invalid action lesson policy ({type(exc).__name__})") from exc


class ActionLessons:
    """Owned, admitted, current lessons near one action; nothing is written."""

    def __init__(
        self,
        store: GraphStore,
        encoder: CPUEncoder,
        lessons: LessonPolicy,
        policy: ActionPolicy | None = None,
        check: ConditionCheck | None = None,
    ) -> None:
        self.store, self.encoder = store, encoder
        self.reader = LessonReader(LessonGraph(store), lessons)
        self.policy = policy or action_policy()
        self.check = check

    def fitting(self, instance: str, action: str, cwd: str | None = None) -> dict[str, Any]:
        """Each fitting lesson with its similarity, statement and routes, nearest first.

        With a condition check, only the lessons with a route whose condition holds come,
        each with those routes only.
        """
        if not action.strip() or len(action) > self.policy.max_action_characters:
            raise MemoryInputError("action violates configured lesson limit")
        vector = self.encoder.encode([action], passage=False)[0]
        near = LessonSearch(self.store).scored(instance, vector, self.policy.candidates)
        found: list[dict[str, Any]] = []
        for identity, score in near:
            if score < self.policy.min_similarity:
                continue
            try:
                lesson = self.reader.read(instance, identity, cwd=cwd)
            except LessonPending:
                continue
            if lesson["admitted"]:
                found.append(_shown(lesson, score))
        if self.check is not None and found:
            holds = self.check.holding(action, cwd, found)
            found = [
                {**item, "routes": [item["routes"][i] for i in holds[item["id"]]]}
                for item in found
                if item["id"] in holds
            ]
        return {"lessons": found}


def _shown(lesson: dict[str, Any], score: float) -> dict[str, Any]:
    return {
        "id": lesson["lesson_id"],
        "similarity": score,
        "statement": lesson["statement"],
        "severity": lesson["severity"],
        "routes": [
            {
                "condition": route["condition"],
                "correction": route["correction"],
                "exception": route["exception"],
            }
            for route in lesson["routes"]
        ],
    }
