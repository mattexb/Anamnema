"""Neutral source-aligned lesson bounds and operator concept vocabulary."""

from importlib.resources import files
from pathlib import Path
from typing import Annotated, Self

from pydantic import Field, JsonValue, ValidationError, model_validator

from anamnema.archive_models import canonical
from anamnema.setup_config import Nonempty, SetupError, StrictModel


class LessonPolicy(StrictModel):
    """No private runner, catalogue, host or hidden model selection."""

    batch: Annotated[int, Field(gt=0)]
    candidates: Annotated[int, Field(gt=0)]
    source_rows: Annotated[int, Field(gt=0)]
    row_characters: Annotated[int, Field(gt=0)]
    answer_tokens: Annotated[int, Field(gt=0)]
    max_lessons: Annotated[int, Field(gt=0)]
    minimum_events: Annotated[int, Field(gt=0)]
    word_kinds: tuple[Nonempty, ...]
    system_projects: dict[str, tuple[Nonempty, ...]]
    unbacked_prefix: Nonempty
    evidence_types: tuple[Nonempty, ...]
    severities: tuple[Nonempty, ...]
    concepts: dict[str, Nonempty]
    instructions: Nonempty
    model_options: dict[str, JsonValue]

    @model_validator(mode="after")
    def coherent(self) -> Self:
        """Protect structured request fields and validate configured vocabularies."""
        if any(
            not values or len(set(values)) != len(values)
            for values in (self.evidence_types, self.severities)
        ):
            raise ValueError("lesson evidence and severity lists must be distinct and nonempty")
        if any(not key.strip() for key in self.concepts):
            raise ValueError("lesson concept names must be nonempty")
        if not set(self.word_kinds) <= set(self.evidence_types):
            raise ValueError("lesson admission word kinds must be configured evidence types")
        if set(self.model_options) & {
            "model",
            "messages",
            "stream",
            "response_format",
            "max_tokens",
        }:
            raise ValueError("lesson options cannot replace controlled request fields")
        canonical(self.model_options)
        return self


def lesson_policy(path: Path | None = None) -> LessonPolicy:
    """Read complete public policy without model or infrastructure access."""
    try:
        raw = (
            path.read_text(encoding="utf-8")
            if path
            else files("anamnema").joinpath("config/lessons.json").read_text(encoding="utf-8")
        )
        return LessonPolicy.model_validate_json(raw)
    except (OSError, UnicodeError, ValidationError) as exc:
        raise SetupError("lesson policy cannot be read or validated") from exc
