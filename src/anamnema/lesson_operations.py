"""Typed lesson commands shared by local CLI and independently authenticated HTTP."""

from typing import Any, Literal, Self

from pydantic import model_validator

from anamnema.lesson_builder import LessonBuilder
from anamnema.lesson_graph import LessonGraph
from anamnema.lesson_models import LessonAgainst
from anamnema.lesson_policy import LessonPolicy, lesson_policy
from anamnema.lesson_read import LessonReader
from anamnema.memory_sources import SourceLookup
from anamnema.setup_config import Nonempty, SetupError, StrictModel


class LessonRequest(StrictModel):
    """Reading never implies generation; historical access is an explicit boolean."""

    operation: Literal["read", "build", "rebuild", "against"]
    lesson: Nonempty | None = None
    notes: tuple[Nonempty, ...] = ()
    historical: bool = False
    rejudge: bool = False
    note: Nonempty | None = None
    condition: Nonempty | None = None
    reason: Nonempty | None = None
    cwd: str | None = None

    @model_validator(mode="after")
    def coherent(self) -> Self:
        """A build takes note ids, a read/rebuild takes one lesson id, never both."""
        if self.operation == "build":
            if self.lesson is not None or not self.notes:
                raise ValueError("lesson build requires notes and no lesson id")
        elif self.lesson is None or self.notes:
            raise ValueError("lesson read/rebuild requires a lesson id and no notes")
        if self.historical and self.operation != "read":
            raise ValueError("only a read accepts historical access")
        if self.rejudge and self.operation != "build":
            raise ValueError("only a build accepts explicit rejudging")
        counter = (self.note, self.condition, self.reason)
        if self.operation == "against":
            if any(value is None for value in counter):
                raise ValueError("counterexample needs note, condition and reason")
        elif any(value is not None for value in counter):
            raise ValueError("only a counterexample accepts note, condition and reason")
        if self.cwd is not None and self.operation != "read":
            raise ValueError("only a read accepts activity scope")
        return self


def lesson_operation(
    graph: LessonGraph,
    builder: LessonBuilder | None,
    instance: str,
    request: LessonRequest,
    policy: LessonPolicy | None = None,
    sources: SourceLookup | None = None,
) -> dict[str, Any]:
    """Enforce the same owned graph and explicit model-selection boundary on every transport."""
    selected = policy or (builder.writer.policy if builder else lesson_policy())
    if request.operation == "read":
        return LessonReader(graph, selected).read(
            instance, request.lesson or "", historical=request.historical, cwd=request.cwd
        )
    if request.operation == "against":
        if sources is None:
            raise SetupError("counterexample nomination requires verified archive access")
        return nominate(graph, sources, selected, instance, request)
    if builder is None:
        raise SetupError("lesson generation requires an explicitly selected lesson model")
    if request.operation == "build":
        return builder.build(instance, request.notes, rejudge=request.rejudge)
    return builder.rebuild(instance, request.lesson or "")


def nominate(
    graph: LessonGraph,
    sources: SourceLookup,
    policy: LessonPolicy,
    instance: str,
    request: LessonRequest,
) -> dict[str, Any]:
    """A learner nominates current source-backed evidence, not a delivery or a lesson verdict."""
    identity = request.lesson or ""
    lesson = graph.get(instance, identity)
    note = graph.sources(instance, (request.note or "",))[0]
    records = tuple(note["source_ids"])
    sources.records(instance, records)
    nomination = LessonAgainst(
        note=request.note or "",
        records=records,
        condition=request.condition or "",
        reason=request.reason or "",
        judged_by=instance,
    )
    graph.add_against(instance, identity, nomination, lesson["revision"], note["token"])
    return LessonReader(graph, policy).read(instance, identity)
