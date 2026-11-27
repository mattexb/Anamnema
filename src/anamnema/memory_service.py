"""Authenticated HTTP facade over the same core used by the command line."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any

from fastapi import FastAPI
from pydantic import Field

from anamnema.action_lessons import ActionLessons
from anamnema.card_operations import CardRequest, EntityRequest, card_operation, entity_operation
from anamnema.graph_corrections import history
from anamnema.lesson_graph import LessonGraph
from anamnema.lesson_operations import LessonRequest, lesson_operation
from anamnema.memory_engine import MemoryEngine
from anamnema.memory_models import Correction, Facet
from anamnema.merge_graph import MergeGraph
from anamnema.service_config import ServiceSettings
from anamnema.service_http import JsonService
from anamnema.setup_config import Nonempty, StrictModel


class SearchRequest(StrictModel):
    """One bounded on-demand search; assistant identity comes from the bearer."""

    question: Nonempty
    facets: tuple[Facet, ...] = ()
    at: datetime | None = None
    page: Annotated[int, Field(gt=0)] = 1


class NoteRequest(StrictModel):
    """An id is always resolved inside the authenticated assistant's namespace."""

    note: Nonempty


class SourceRequest(StrictModel):
    """Compact context is the default; raw originals require an explicit request."""

    note: Nonempty
    raw: bool = False


class AdviceRequest(StrictModel):
    """One action about to run; its lessons are chosen by fit, never by a fixed count."""

    action: Nonempty
    cwd: str | None = None


class UnmergeRequest(StrictModel):
    """A source-backed occurrence repair needs an accountable explicit reason."""

    note: Nonempty
    reason: Nonempty


def create_memory_app(
    engine: MemoryEngine, settings: ServiceSettings, tokens: dict[str, str]
) -> FastAPI:
    """No anonymous route and no trust in proxy-provided identity headers."""
    service = JsonService(settings, tokens, "Anamnema memory")

    def search(instance: str, request: SearchRequest) -> dict[str, Any]:
        return engine.search(instance, request.question, request.facets, request.at, request.page)

    service.post(settings.search_path, SearchRequest, search, "Search this assistant's notes.")
    service.post(
        settings.note_path,
        NoteRequest,
        lambda instance, body: engine.store.note(instance, body.note),
        "Read a note, excluding vectors.",
    )
    service.post(
        settings.source_path,
        SourceRequest,
        lambda instance, body: engine.sources.open_note(
            engine.store, instance, body.note, body.raw
        ),
        "Open the exact original conversation records cited by a note.",
    )
    service.post(
        settings.correction_path,
        Correction,
        engine.check,
        "Record a source-backed check exactly once; explicit deletion retains its history.",
    )
    service.post(
        settings.history_path,
        NoteRequest,
        lambda instance, body: {"checks": history(engine.store, instance, body.note)},
        "Read this assistant's correction history, including deleted notes.",
    )
    service.post(
        settings.entity_path,
        EntityRequest,
        lambda instance, body: entity_operation(engine.card_graph, instance, body),
        "Inspect or explicitly decide owned entity state, retaining reasons.",
    )
    service.post(
        settings.card_path,
        CardRequest,
        lambda instance, body: card_operation(engine.card_graph, engine.cards, instance, body),
        "Read current source-backed statements or explicitly request their rebuild.",
    )
    service.post(
        settings.unmerge_path,
        UnmergeRequest,
        lambda instance, body: MergeGraph(engine.store).unmerge(instance, body.note, body.reason),
        "Restore an owned occurrence as a note, preserving the decision trace.",
    )
    service.post(
        settings.lesson_path,
        LessonRequest,
        lambda instance, body: lesson_operation(
            LessonGraph(engine.store),
            engine.lessons,
            instance,
            body,
            engine.lesson_settings,
            engine.sources,
        ),
        "Read an owned lesson or explicitly generate/rebuild with verified source evidence.",
    )
    service.post(
        settings.advice_path,
        AdviceRequest,
        lambda instance, body: ActionLessons(
            engine.store, engine.encoder, engine.lesson_settings
        ).fitting(instance, body.action, body.cwd),
        "Every admitted owned lesson that fits an action about to run, nearest first.",
    )
    return service.app
