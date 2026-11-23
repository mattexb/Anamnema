"""One command family over the shared memory core; setup remains separately gated."""

from __future__ import annotations

import argparse
from contextlib import ExitStack
from pathlib import Path
from typing import Any, Protocol

import uvicorn
from pydantic import ValidationError

from anamnema.archive_access import connect, keyring_secret, keyring_uri
from anamnema.archive_mongo import MongoArchive
from anamnema.binding import bindings
from anamnema.briefing_cli import add_brief_arguments, brief_request, run_brief
from anamnema.capture_cli import CaptureInput, add_capture_arguments, prepare_capture, store_capture
from anamnema.card_cli import CARD_COMMANDS, add_card_arguments, run_cards, selected_builder
from anamnema.card_policy import card_policy
from anamnema.chunks import chunks
from anamnema.extraction_config import extraction_config
from anamnema.extraction_store import ExtractionStore
from anamnema.graph_corrections import history
from anamnema.graph_store import GraphStore, connect_graph
from anamnema.ingest_config import ArchiveSettings, archive_settings
from anamnema.lesson_cli import (
    LESSON_COMMANDS,
    add_lesson_arguments,
    run_against,
    run_lessons,
    selected_lessons,
)
from anamnema.lesson_graph import LessonGraph
from anamnema.lesson_policy import lesson_policy
from anamnema.lesson_read import LessonReader
from anamnema.local_model import local_model
from anamnema.memory_config import load_memory
from anamnema.memory_engine import MemoryEngine
from anamnema.memory_loader import MemoryLoader
from anamnema.memory_models import Correction, Facet
from anamnema.memory_service import create_memory_app
from anamnema.memory_sources import SourceLookup
from anamnema.merge_graph import MergeGraph
from anamnema.merge_judge import MergeJudge
from anamnema.merge_policy import merge_policy
from anamnema.naming_cli import add_naming_arguments, run_naming
from anamnema.notes_policy import notes_policy
from anamnema.onnx_ranker import ONNXRanker, load_onnx
from anamnema.processing import prepare
from anamnema.repeat_merge import RepeatMerger
from anamnema.reranker_service import create_reranker_app
from anamnema.retrieval import Retriever
from anamnema.retrieval_config import load_retrieval
from anamnema.search_services import CPUEncoder, CPUReranker, LocalJSON, load_search_services
from anamnema.service_config import ServiceSettings, load_service
from anamnema.setup_config import SetupConfig, SetupError, load_config
from anamnema.source_display import SourceDisplay
from anamnema.source_policy import source_policy
from anamnema.speech_policy import speech_policy
from anamnema.substrates.claude import ClaudeAdapter
from anamnema.substrates.claude_speech import ClaudeSpeechReader
from anamnema.window_store import WindowStore

COMMANDS = (
    "brief",
    "name-notes",
    "capture",
    "store",
    "search",
    "source",
    "check",
    "history",
    "unmerge",
    "serve",
    "serve-reranker",
    *CARD_COMMANDS,
    *LESSON_COMMANDS,
)


class CommandParsers(Protocol):
    """Only argparse's public parser-building surface is needed here."""

    def add_parser(self, name: str, **kwargs: Any) -> argparse.ArgumentParser:
        """Create a child command."""
        ...


def add_memory_commands(commands: CommandParsers) -> None:
    """Expose complete help without opening stores, loading models or looking up secrets."""
    descriptions = {
        "brief": "build source-backed recall from an explicit request; dry-run by default",
        "name-notes": "name owned notes from verified sources; dry-run by default",
        "capture": "capture marked self-notes without a notes-model call; dry-run by default",
        "store": "store already-extracted closed Claude stretches; dry-run by default",
        "search": "search your source-linked notes by meaning, words and identifiers",
        "source": "open a note's exact original archive records",
        "check": "apply an accountable source-backed correction from JSON",
        "history": "show a note's correction history, including after deletion",
        "unmerge": "restore a merged occurrence as its own source-linked note",
        "entities": "inspect or explicitly decide your source-linked entity state",
        "card": "read an entity's current source-backed card",
        "build-cards": "rebuild an entity card with an explicitly selected model",
        "lesson": "read an owned current lesson or explicitly inspect its history",
        "build-lessons": "aggregate source-backed notes or rebuild an existing lesson",
        "lesson-against": "nominate a source-backed counterexample without counting delivery",
        "serve": "serve memory on localhost with per-instance bearer authentication",
        "serve-reranker": "serve the packaged CPU ONNX reranker on localhost",
    }
    for name, description in descriptions.items():
        command = commands.add_parser(name, help=description, description=description)
        for option in (
            "config",
            "archive-config",
            "memory-config",
            "search-config",
            "retrieval-config",
            "service-config",
            "source-config",
            "speech-config",
            "cards-config",
            "lessons-config",
            "naming-config",
        ):
            command.add_argument(
                f"--{option}", type=Path, help="complete nonsecret JSON configuration"
            )
        if name not in ("serve", "serve-reranker"):
            command.add_argument(
                "--instance", required=True, help="assistant owning this local operation"
            )
        if name in CARD_COMMANDS:
            add_card_arguments(name, command)
        if name == "name-notes":
            add_naming_arguments(command)
        if name == "brief":
            add_brief_arguments(command)
        if name in LESSON_COMMANDS:
            add_lesson_arguments(name, command)
        if name == "capture":
            add_capture_arguments(command)
        if name in ("check", "serve", "build-lessons"):
            command.add_argument("--lesson-model-file", type=Path, required=name == "build-lessons")
        if name in ("check", "serve", "build-cards"):
            command.add_argument("--card-model-file", type=Path, required=name == "build-cards")
        if name in ("check", "serve", "build-cards", "build-lessons", "name-notes"):
            command.add_argument("--extraction-config", type=Path)
        if name in ("source", "history", "unmerge"):
            command.add_argument("--note", required=True)
        if name == "unmerge":
            command.add_argument("--reason", required=True)
        if name == "source":
            command.add_argument(
                "--raw", action="store_true", help="include exact raw original records"
            )
        if name == "search":
            command.add_argument("question")
            command.add_argument("--page", type=int, default=1)
            command.add_argument(
                "--facet", action="append", default=[], help="exact kind=value identifier"
            )
        if name == "check":
            command.add_argument(
                "--file", type=Path, required=True, help="correction JSON document"
            )
        if name == "serve-reranker":
            command.add_argument("--model-dir", type=Path, required=True)
            command.add_argument("--onnx-config", type=Path)
        if name == "store":
            command.add_argument("--merge-config", type=Path)
            command.add_argument("--merge-model-file", type=Path)
            command.add_argument("--file", type=Path, required=True)
            command.add_argument("--session", required=True)
            command.add_argument(
                "--run", required=True, help="exact successful extraction run fingerprint"
            )
            command.add_argument("--closed", action="store_true")
            for option in ("notes-config", "extraction-config"):
                command.add_argument(f"--{option}", type=Path)
            mode = command.add_mutually_exclusive_group()
            mode.add_argument(
                "--dry-run",
                action="store_true",
                help="report inputs without infrastructure (default)",
            )
            mode.add_argument(
                "--apply", action="store_true", help="verify saved chunks/sources and insert memory"
            )


def _local_preview(args: argparse.Namespace) -> tuple[CaptureInput | None, dict[str, Any] | None]:
    """Source preparation and no-op reports precede every credential/service operation."""
    if args.command == "brief":
        request = brief_request(args)
        if args.apply and args.judge_config is None:
            raise SetupError("brief --apply requires --judge-config")
        if not args.apply:
            return None, {
                "mode": "dry-run",
                "selectors": len(request.selectors),
                "owned_selection_verified": False,
                "model_called": False,
                "files_written": False,
            }
    if args.command == "name-notes" and args.apply and args.model_file is None:
        raise SetupError("naming apply requires --model-file")
    if args.command == "name-notes" and not args.apply:
        return None, {
            "mode": "dry-run",
            "subjects_judged": False,
            "graph_written": False,
            "selection": "pending" if args.pending else "explicit",
            "requested_notes": len(args.note or []),
        }
    capture = prepare_capture(args) if args.command == "capture" else None
    if capture is not None and (not args.apply or not capture.marks):
        return capture, capture.preview(args.apply)
    if args.command == "store" and not args.apply:
        settings = archive_settings(args.archive_config)
        snapshot = ClaudeAdapter(settings).read(
            args.file, args.instance, args.session, closed=args.closed
        )
        return None, {
            "mode": "dry-run",
            "closed_stretches": len(prepare(snapshot).windows),
            "run": args.run,
            "saved_notes_verified": False,
            "graph_written": False,
        }
    return capture, None


def run_memory(args: argparse.Namespace) -> dict[str, Any]:
    """Open only the resources the selected operation needs; close every owned connection."""
    capture, preview = _local_preview(args)
    if preview is not None:
        return preview
    setup, archive = load_config(args.config), archive_settings(args.archive_config)
    policy = load_search_services(args.search_config)
    service = load_service(args.service_config)
    if args.command == "serve-reranker":
        tokens = _tokens(setup, archive, service)
        ranker = ONNXRanker(args.model_dir, load_onnx(args.onnx_config))
        app = create_reranker_app(ranker, service, tokens, policy.rerank_path)
        uvicorn.run(
            app,
            host=setup.services.reranker.host,
            port=setup.services.reranker.port,
            access_log=False,
        )
        return {"status": "success", "service": "reranker-stopped"}
    memory = load_memory(args.memory_config)
    with ExitStack() as resources:
        graph = connect_graph(setup, archive, memory)
        resources.callback(graph.close)
        inspected = _graph_command(args, setup, archive, graph)
        if inspected is not None:
            return inspected
        mongo = connect(keyring_uri(setup, archive), archive, setup.services.archive)
        resources.callback(mongo.close)
        original = MongoArchive(mongo[archive.database], archive)
        windows = WindowStore(original, source_policy(args.source_config))
        display = SourceDisplay(windows, ClaudeSpeechReader(speech_policy(args.speech_config)))
        sources = SourceLookup(original, memory.max_items, memory.max_source_bytes, display)
        if args.command == "source":
            return sources.open_note(graph, args.instance, args.note, args.raw)
        if args.command == "lesson-against":
            return run_against(args, graph, sources)
        if args.command == "brief":
            return run_brief(args, graph, sources)
        token = keyring_secret(setup, archive, policy.encoder_keyring_entry, policy.token_field)
        encoder = CPUEncoder(
            LocalJSON(setup.services.encoder, policy, token, policy.timeout_seconds)
        )
        if args.command == "store":
            graph.ensure_schema()
            return _store(args, original, graph, sources, encoder)
        if capture is not None:
            graph.ensure_schema()
            return store_capture(capture, original, graph, sources, encoder)
        if args.command == "name-notes":
            return run_naming(args, setup, archive, graph, encoder, sources)
        lessons = selected_lessons(args, setup, archive, graph, encoder, sources)
        if args.command == "build-lessons":
            return run_lessons(args, lessons, graph)
        retrieval = load_retrieval(args.retrieval_config)
        reranker = None
        if retrieval.rerank_enabled:
            token = keyring_secret(
                setup, archive, policy.reranker_keyring_entry, policy.token_field
            )
            reranker = CPUReranker(
                LocalJSON(setup.services.reranker, policy, token, policy.rerank_timeout_seconds)
            )
        cards = selected_builder(args, setup, archive, graph)
        engine = MemoryEngine(
            graph,
            sources,
            encoder,
            Retriever(graph, retrieval, reranker),
            cards,
            card_policy(args.cards_config),
            lessons,
            lesson_policy(args.lessons_config),
        )
        tokens = _tokens(setup, archive, service) if args.command == "serve" else {}
        return _dispatch(args, engine, service, tokens)


def _graph_command(
    args: argparse.Namespace, setup: SetupConfig, archive: ArchiveSettings, graph: GraphStore
) -> dict[str, Any] | None:
    if args.command == "history":
        return {"checks": history(graph, args.instance, args.note)}
    if args.command == "lesson":
        return LessonReader(LessonGraph(graph), lesson_policy(args.lessons_config)).read(
            args.instance, args.lesson, historical=args.historical, cwd=args.cwd
        )
    if args.command == "unmerge":
        return MergeGraph(graph).unmerge(args.instance, args.note, args.reason)
    if args.command in CARD_COMMANDS:
        return run_cards(args, setup, archive, graph)
    return None


def _tokens(
    setup: SetupConfig, archive: ArchiveSettings, service: ServiceSettings
) -> dict[str, str]:
    if not service.access:
        raise SetupError("service access needs explicit instance/keyring selectors")
    return {
        entry.instance: keyring_secret(setup, archive, entry.entry, entry.field)
        for entry in service.access
    }


def _dispatch(
    args: argparse.Namespace, engine: MemoryEngine, service: ServiceSettings, tokens: dict[str, str]
) -> dict[str, Any]:
    if args.command == "search":
        facets: list[Facet] = []
        for value in args.facet:
            kind, separator, text = value.partition("=")
            if not separator or not kind or not text:
                raise SetupError("facet must be an explicit kind=value pair")
            facets.append(Facet(kind=kind, value=text))
        return engine.search(args.instance, args.question, tuple(facets), page=args.page)
    if args.command == "check":
        try:
            with args.file.open("rb") as stream:
                raw = stream.read(service.max_request_bytes + 1)
            if len(raw) > service.max_request_bytes:
                raise SetupError("correction file exceeds configured request bound")
            correction = Correction.model_validate_json(raw)
        except (OSError, ValidationError) as exc:
            raise SetupError(
                f"correction file cannot be read/validated ({type(exc).__name__})"
            ) from exc
        return engine.check(args.instance, correction)
    app = create_memory_app(engine, service, tokens)
    uvicorn.run(app, host=service.bind.host, port=service.bind.port, access_log=False)
    return {"status": "success", "service": "memory-stopped"}


def _store(
    args: argparse.Namespace,
    archive: MongoArchive,
    graph: GraphStore,
    sources: SourceLookup,
    encoder: CPUEncoder,
) -> dict[str, Any]:
    notes, speech = notes_policy(args.notes_config), speech_policy(args.speech_config)
    snapshot = ClaudeAdapter(archive.settings).read(
        args.file, args.instance, args.session, closed=args.closed
    )
    archive.verify(list({record.identity: record for record in snapshot.records}.values()))
    prepared = prepare(snapshot)
    if not prepared.windows:
        raise SetupError("no closed stretches to store; an open tail is not a completed input")
    extraction = ExtractionStore(
        archive.database, extraction_config(args.extraction_config), notes, speech
    )
    loader = MemoryLoader(graph, sources, extraction, encoder, _merger(args, graph))
    reader = ClaudeSpeechReader(speech)
    results: list[dict[str, int]] = []
    for window in prepared.windows:
        rows = reader.read(window).rows
        parts = chunks(rows, bindings(rows, notes), notes)
        results.append(loader.window(window, parts, args.run))
    return {"mode": "apply", "windows": results}


def _merger(args: argparse.Namespace, graph: GraphStore) -> RepeatMerger | None:
    policy = merge_policy(args.merge_config)
    if not policy.enabled:
        return None
    if args.merge_model_file is None:
        raise SetupError("enabled repeat merge requires --merge-model-file")
    setup, archive = load_config(args.config), archive_settings(args.archive_config)
    config = extraction_config(args.extraction_config)
    provider = local_model(setup, archive, config, args.merge_model_file)
    return RepeatMerger(graph, policy, MergeJudge(provider, policy))
