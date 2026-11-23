"""Strict local graph configuration; identifiers are validated before Cypher interpolation."""

from __future__ import annotations

from importlib.resources import files
from pathlib import Path
from typing import Annotated, Literal, Self

from pydantic import Field, ValidationError, model_validator

from anamnema.setup_config import Nonempty, SetupError, StrictModel

Identifier = Annotated[str, Field(pattern=r"^[A-Za-z][A-Za-z0-9_]*$", max_length=64)]
Positive = Annotated[float, Field(gt=0, allow_inf_nan=False)]
Unit = Annotated[float, Field(ge=0, le=1, allow_inf_nan=False)]


class RatingSettings(StrictModel):
    """Source rating coefficients, separately configured from relevance."""

    floor: Unit
    wrong_cap_margin: Positive
    right_weight: Positive
    partly_weight: Positive
    wrong_weight: Positive
    freshness_boost: Unit
    freshness_half_life_days: Positive
    usefulness_weight: Unit
    stale_penalty: Unit
    event_time_kinds: tuple[Nonempty, ...]

    @model_validator(mode="after")
    def cap_below_floor(self) -> Self:
        """An open wrong finding must remain strictly under the trust floor."""
        if self.wrong_cap_margin > self.floor:
            raise ValueError("wrong cap margin exceeds floor")
        return self


class TopicSettings(StrictModel):
    """Source topic-neighbour and membership bounds, independent of recall ranking."""

    candidates: Annotated[int, Field(gt=0)]
    join_similarity: Unit
    max_memberships: Annotated[int, Field(gt=0)]

    @model_validator(mode="after")
    def membership_bound(self) -> Self:
        """Do not request more memberships than the candidate budget."""
        if self.max_memberships > self.candidates:
            raise ValueError("topic memberships exceed candidates")
        return self


class MemorySettings(StrictModel):
    """Neutral graph schema and limits, with no URI or embedded credentials."""

    schema_version: Literal[1]
    database: Identifier
    keyring_entry: Nonempty
    user_field: Nonempty
    password_field: Nonempty
    timeout_seconds: Positive
    index_wait_seconds: Annotated[int, Field(gt=0)]
    note_label: Identifier
    piece_label: Identifier
    check_label: Identifier
    group_label: Identifier
    occurrence_label: Identifier
    decision_label: Identifier
    entity_label: Identifier
    statement_label: Identifier
    move_label: Identifier
    entity_event_label: Identifier
    lesson_label: Identifier
    concept_label: Identifier
    source_occurrences: Annotated[int, Field(gt=0)]
    dimensions: Annotated[int, Field(gt=0, le=4096)]
    max_items: Annotated[int, Field(gt=0)]
    max_note_characters: Annotated[int, Field(gt=0)]
    max_source_bytes: Annotated[int, Field(gt=0)]
    trust_prior_positive: Positive
    trust_prior_negative: Positive
    rating: RatingSettings
    topics: TopicSettings

    @model_validator(mode="after")
    def distinct_labels(self) -> Self:
        """Do not let derived node types overwrite one another."""
        labels = (
            self.note_label,
            self.piece_label,
            self.check_label,
            self.group_label,
            self.occurrence_label,
            self.decision_label,
            self.entity_label,
            self.statement_label,
            self.move_label,
            self.entity_event_label,
            self.lesson_label,
            self.concept_label,
        )
        if len(set(labels)) != len(labels):
            raise ValueError("graph labels must be distinct")
        if self.topics.candidates > self.max_items:
            raise ValueError("topic candidates exceed graph batch bound")
        return self


def load_memory(path: Path | None = None) -> MemorySettings:
    """Load complete nonsecret graph settings, never a private checkout default."""
    try:
        text = (
            path.read_text(encoding="utf-8")
            if path is not None
            else files("anamnema").joinpath("config/memory.json").read_text(encoding="utf-8")
        )
        return MemorySettings.model_validate_json(text)
    except (OSError, UnicodeError, ValidationError) as exc:
        raise SetupError(f"invalid memory configuration ({type(exc).__name__})") from exc
