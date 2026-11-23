"""Strict graph-boundary contracts shared by CLI, service and storage."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Annotated, Any, Self

from pydantic import (
    Field,
    SerializerFunctionWrapHandler,
    field_validator,
    model_serializer,
    model_validator,
)

from anamnema.archive_models import digest
from anamnema.setup_config import Nonempty, SetupError, StrictModel

Finite = Annotated[float, Field(allow_inf_nan=False)]
Vector = Annotated[tuple[Finite, ...], Field(min_length=1)]


class MemoryInputError(SetupError):
    """A validly typed request violates a configured memory-operation bound."""


class Outcome(StrEnum):
    """Source correction outcomes, not model confidence labels."""

    RIGHT = "right"
    PARTLY = "partly"
    WRONG = "wrong"
    OUTDATED = "outdated"
    POINTER = "pointer"
    CLEARED = "cleared"


class Piece(StrictModel):
    """A later overlapping passage of a long note, never a separate memory."""

    offset: Annotated[int, Field(gt=0)]
    vector: Vector


class Facet(StrictModel):
    """Explicit identifier membership, scoped to an assistant by the store."""

    kind: Nonempty
    value: Nonempty


class CaptureOrigin(StrictModel):
    """Immutable handwritten origin survives subsequent text/vector corrections."""

    source_id: Nonempty
    index: Annotated[int, Field(ge=0)]
    marker: Nonempty
    text_digest: Nonempty
    entity: str = ""
    lesson_of: Nonempty | None = None


class MemoryNote(StrictModel):
    """An immutable insertion proposal; corrections never change this identity."""

    id: Nonempty
    instance: Nonempty
    text: Nonempty
    kind: Nonempty
    source_ids: Annotated[tuple[Nonempty, ...], Field(min_length=1)]
    session: Nonempty
    substrate: Nonempty
    window: Nonempty | None
    run: Nonempty
    said_at: str | None = None
    event_time: str | None = None
    problems: tuple[str, ...] = ()
    candidate_evidence: bool = False
    query_vector: Vector
    passage_vector: Vector
    embedded_with: Nonempty
    pieces: tuple[Piece, ...] = ()
    facets: tuple[Facet, ...] = ()
    merge_deferred: str | None = None
    changes_note: str | None = None
    unmerged: str | None = None
    capture_origin: CaptureOrigin | None = None

    @model_serializer(mode="wrap")
    def _serialize(self, handler: SerializerFunctionWrapHandler) -> dict[str, Any]:
        """Absent new metadata must not change older notes' insertion fingerprints."""
        result: dict[str, Any] = handler(self)
        if self.capture_origin is None:
            result.pop("capture_origin", None)
        return result

    @field_validator("said_at", "event_time")
    @classmethod
    def aware_time(cls, value: str | None) -> str | None:
        """Historical comparisons cannot silently mix local and timezone-aware times."""
        if value is not None and datetime.fromisoformat(value).tzinfo is None:
            raise ValueError("memory timestamps require a timezone")
        return value

    @model_validator(mode="after")
    def unique_parts(self) -> Self:
        """Duplicate sources/pieces are not separate evidence or independent vectors."""
        if len(set(self.source_ids)) != len(self.source_ids):
            raise ValueError("note repeats a source id")
        if len({piece.offset for piece in self.pieces}) != len(self.pieces):
            raise ValueError("note repeats a passage offset")
        if self.window is None and self.capture_origin is None:
            raise ValueError("unwindowed notes require a capture origin")
        if self.capture_origin is not None and (
            self.capture_origin.source_id not in self.source_ids
            or self.capture_origin.text_digest != digest(self.text)
        ):
            raise ValueError("capture origin differs from insertion source/text")
        return self

    @property
    def fingerprint(self) -> str:
        """Compare replays against original input, not a subsequently corrected text."""
        return digest(self.model_dump(mode="json"))


class Correction(StrictModel):
    """Accountable source-backed check; malformed booleans cannot become deletion."""

    id: Nonempty
    note: Nonempty
    outcome: Outcome
    reason: Nonempty
    against: tuple[Nonempty, ...] = ()
    records: tuple[Nonempty, ...] = ()
    useful: bool | None = None
    delete: bool | None = None
    superseded_by: Nonempty | None = None
    rewrite: Nonempty | None = None
    session: Nonempty | None = None

    @model_validator(mode="after")
    def coherent(self) -> Self:
        """Preserve the source distinctions between wrong, partly and outdated."""
        if self.outcome != Outcome.POINTER and not self.against:
            raise ValueError("a source-backed check needs against references")
        if self.outcome == Outcome.POINTER and not self.records:
            raise ValueError("a pointer check needs source records")
        if self.records and self.outcome != Outcome.POINTER:
            raise ValueError("only a pointer check replaces source records")
        if self.delete is True and self.outcome != Outcome.WRONG:
            raise ValueError("only a wrong check may delete")
        if (self.outcome == Outcome.PARTLY) != (self.rewrite is not None):
            raise ValueError("only a partly check requires a rewrite")
        if self.superseded_by and self.outcome not in (Outcome.RIGHT, Outcome.OUTDATED):
            raise ValueError("only right/outdated may name a replacement")
        return self


class Rewrite(StrictModel):
    """New vectors are required before a rewrite can replace the stored text."""

    query_vector: Vector
    passage_vector: Vector
    embedded_with: Nonempty
    pieces: tuple[Piece, ...] = ()
