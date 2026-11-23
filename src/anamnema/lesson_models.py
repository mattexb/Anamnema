"""Strict judged lesson content and its separately auditable evidence references."""

from typing import Annotated, Self

from pydantic import Field, model_validator

from anamnema.setup_config import Nonempty, StrictModel

CONTENT_FIELDS = ("statement", "mechanism", "routes", "severity", "status", "conflict")
SNAPSHOT_FIELDS = ("version", *CONTENT_FIELDS, "model", "judged_at")
NOTE_FIELDS = ("text", "source_ids", "wrong_open", "status", "trust_positive", "trust_negative")
NOTE_CONTENT_FIELDS = ("text", "source_ids")


class LessonRoute(StrictModel):
    """An explicitly judged exception flag, never a truthy string or inferred default."""

    condition: Nonempty
    correction: Nonempty
    notes: tuple[Nonempty, ...]
    exception: bool


class LessonEpisode(StrictModel):
    """A judged event with the original note's own source records, not model-made citations."""

    note: Nonempty
    evidence: Nonempty
    event: Nonempty
    event_reason: str
    records: tuple[Nonempty, ...]


class LessonAgainst(StrictModel):
    """A learner-nominated counterexample with scope and why a narrower route does not fit."""

    note: Nonempty
    records: tuple[Nonempty, ...]
    condition: Nonempty
    reason: Nonempty
    judged_by: Nonempty


class LessonContent(StrictModel):
    """Judged content shared by structured generation and persistence."""

    statement: Nonempty
    mechanism: str
    routes: tuple[LessonRoute, ...]
    severity: Nonempty
    status: Nonempty
    conflict: str
    concepts: tuple[Nonempty, ...]


class LessonDocument(LessonContent):
    """Input to a bounded atomic save; source tokens and revision are supplied separately."""

    id: Nonempty
    model: Nonempty
    embedding: tuple[Annotated[float, Field(allow_inf_nan=False)], ...]
    episodes: tuple[LessonEpisode, ...]

    @model_validator(mode="after")
    def coherent(self) -> Self:
        """A route must cite supplied evidence; repeated episode identities are ambiguous."""
        validate_support(self.episodes, self.routes)
        return self


class LessonWrite(StrictModel):
    """One proposed lesson with the revision actually supplied to its judge."""

    lesson: LessonDocument
    revision: Annotated[int, Field(ge=0)]


class LessonJudgement(StrictModel):
    """A complete bounded model decision, never a sequence of independent commits."""

    lessons: tuple[LessonWrite, ...]
    unresolved: tuple[Nonempty, ...] = ()
    source_tokens: dict[Nonempty, Nonempty]
    model: Nonempty
    pending_only: bool = False

    @model_validator(mode="after")
    def complete(self) -> Self:
        """Every input is either placed or unresolved, with one matching generation token."""
        ids = [item.lesson.id for item in self.lessons]
        placed = {episode.note for item in self.lessons for episode in item.lesson.episodes}
        unresolved = set(self.unresolved)
        if len(set(ids)) != len(ids) or len(unresolved) != len(self.unresolved):
            raise ValueError("judgement repeats a lesson or unresolved note")
        if placed & unresolved or set(self.source_tokens) != placed | unresolved:
            raise ValueError("judgement needs exactly one input token for each decided note")
        if not self.source_tokens or any(item.lesson.model != self.model for item in self.lessons):
            raise ValueError("judgement requires evidence and one model identity")
        return self


def validate_support(episodes: tuple[LessonEpisode, ...], routes: tuple[LessonRoute, ...]) -> None:
    """Apply the same support rules before embedding a judgement and before graph persistence."""
    ids = [episode.note for episode in episodes]
    if not ids or len(set(ids)) != len(ids):
        raise ValueError("lesson requires distinct evidence notes")
    if any(not episode.records for episode in episodes):
        raise ValueError("lesson episodes need source records")
    for event in {episode.event for episode in episodes}:
        members = [episode for episode in episodes if episode.event == event]
        if len(members) > 1 and any(not episode.event_reason.strip() for episode in members):
            raise ValueError("a shared judged event needs reasons for every member")
    if any(not route.notes or not set(route.notes) <= set(ids) for route in routes):
        raise ValueError("lesson routes must cite supplied episode notes")
