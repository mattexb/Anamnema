"""Explicit lesson generation selection and source-independent historical inspection."""

import argparse
from typing import Any

from anamnema.extraction_config import extraction_config
from anamnema.graph_store import GraphStore
from anamnema.ingest_config import ArchiveSettings
from anamnema.lesson_builder import LessonBuilder
from anamnema.lesson_graph import LessonGraph
from anamnema.lesson_operations import LessonRequest, lesson_operation
from anamnema.lesson_policy import lesson_policy
from anamnema.lesson_writer import LessonWriter
from anamnema.local_model import local_model
from anamnema.memory_sources import SourceLookup
from anamnema.search_services import CPUEncoder
from anamnema.setup_config import SetupConfig

LESSON_COMMANDS = ("lesson", "build-lessons", "lesson-against")


def add_lesson_arguments(name: str, parser: argparse.ArgumentParser) -> None:
    """Separate current/historical reads from initial aggregation and explicit rebuilds."""
    if name == "lesson":
        parser.add_argument("--lesson", required=True)
        parser.add_argument("--historical", action="store_true")
        parser.add_argument("--cwd", help="activity scope for lesson admission")
    elif name == "lesson-against":
        for flag in ("lesson", "note", "condition", "reason"):
            parser.add_argument(f"--{flag}", required=True)
    else:
        parser.add_argument("--rejudge", action="store_true")
        choice = parser.add_mutually_exclusive_group(required=True)
        choice.add_argument("--note", dest="lesson_notes", action="append")
        choice.add_argument("--lesson", help="rebuild an existing lesson")


def selected_lessons(
    args: argparse.Namespace,
    setup: SetupConfig,
    archive: ArchiveSettings,
    graph: GraphStore,
    encoder: CPUEncoder,
    sources: SourceLookup,
) -> LessonBuilder | None:
    """A model file is explicit; no automatic private runner or service selection."""
    model = getattr(args, "lesson_model_file", None)
    if model is None:
        return None
    provider = local_model(setup, archive, extraction_config(args.extraction_config), model)
    speech = sources.display.reader.policy if sources.display else None
    writer = LessonWriter(provider, lesson_policy(args.lessons_config), speech)
    return LessonBuilder(LessonGraph(graph), writer, encoder, sources)


def run_lessons(
    args: argparse.Namespace, builder: LessonBuilder | None, graph: GraphStore
) -> dict[str, Any]:
    """Route CLI mutations through the same typed operation as authenticated HTTP."""
    request = LessonRequest(
        operation="rebuild" if args.lesson else "build",
        lesson=args.lesson,
        notes=tuple(args.lesson_notes or ()),
        rejudge=args.rejudge,
    )
    graph.ensure_schema()
    return lesson_operation(LessonGraph(graph), builder, args.instance, request)


def run_against(
    args: argparse.Namespace, graph: GraphStore, sources: SourceLookup
) -> dict[str, Any]:
    """Record one learner's bounded nomination and return its source-backed vote."""
    return lesson_operation(
        LessonGraph(graph),
        None,
        args.instance,
        LessonRequest(
            operation="against",
            lesson=args.lesson,
            note=args.note,
            condition=args.condition,
            reason=args.reason,
        ),
        lesson_policy(args.lessons_config),
        sources,
    )
