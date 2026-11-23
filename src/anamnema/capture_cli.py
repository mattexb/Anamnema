"""Prepare manual capture before credentials; apply reuses verified archive and graph services."""

import argparse
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from anamnema.archive_models import Snapshot, digest
from anamnema.archive_mongo import MongoArchive, import_snapshot
from anamnema.capture_selection import CaptureSelection, select_capture
from anamnema.capture_writer import CaptureWriter
from anamnema.graph_store import GraphStore
from anamnema.ingest_config import archive_settings
from anamnema.marked_capture import CaptureMark, CapturePolicy, capture_policy, captured_marks
from anamnema.memory_config import load_memory
from anamnema.memory_sources import SourceLookup
from anamnema.search_services import CPUEncoder
from anamnema.setup_config import SetupError
from anamnema.speech_policy import SpeechPolicy, speech_policy
from anamnema.substrates.claude import ClaudeAdapter


@dataclass(frozen=True)
class CaptureInput:
    """One stable source capture and its fully validated, content-free preview counts."""

    snapshot: Snapshot
    marks: tuple[CaptureMark, ...]
    policy: CapturePolicy
    speech: SpeechPolicy
    whole: bool
    last_turn: bool = False
    selection: CaptureSelection | None = None

    def preview(self, apply: bool) -> dict[str, Any]:
        """No archive verification or storage is implied by preparation, even on an empty apply."""
        return {
            "mode": "apply" if apply else "dry-run",
            "marks": len(self.marks),
            "whole": self.whole,
            "last_turn": self.last_turn,
            "selection_pinned": self.selection is not None,
            "sources_verified": False,
            "graph_written": False,
            "notes_model_calls": 0,
            "source_sha256": self.snapshot.sha256,
            "source_marks_digest": digest([mark.id for mark in self.marks]),
        }


def add_capture_arguments(command: argparse.ArgumentParser) -> None:
    """Capture owns its source selection; no model-file option is needed."""
    command.add_argument("--file", type=Path, required=True)
    command.add_argument("--session", required=True)
    command.add_argument("--capture-config", type=Path)
    scope = command.add_mutually_exclusive_group()
    scope.add_argument("--whole", action="store_true", help="include earlier closed stretches")
    scope.add_argument("--last-turn", action="store_true", help="select the latest owner turn")
    scope.add_argument("--selection", help="pinned range JSON containing source coordinates only")
    mode = command.add_mutually_exclusive_group()
    mode.add_argument(
        "--dry-run", action="store_true", help="preview without credentials or services"
    )
    mode.add_argument(
        "--apply", action="store_true", help="verify sources, capture and place notes"
    )


def prepare_capture(args: argparse.Namespace) -> CaptureInput:
    """Invalid input/catalogues cannot trigger keyring, network or encoder calls."""
    policy, speech = capture_policy(args.capture_config), speech_policy(args.speech_config)
    memory = load_memory(args.memory_config)
    if policy.max_text_characters > memory.max_note_characters:
        raise SetupError("capture text bound exceeds graph bound")
    snapshot = ClaudeAdapter(archive_settings(args.archive_config)).read(
        args.file, args.instance, args.session
    )
    selected = getattr(args, "selection", None)
    selection = None
    if selected is not None:
        if args.whole or args.last_turn:
            raise SetupError("pinned capture selection cannot mix with moving selection modes")
        try:
            selection = CaptureSelection.model_validate_json(selected)
        except ValueError as exc:
            raise SetupError("invalid pinned capture selection") from exc
        if selection.instance != args.instance or selection.session != args.session:
            raise SetupError("capture selection belongs to another assistant/session")
        snapshot = select_capture(snapshot, selection, policy, speech)
    marks = captured_marks(
        snapshot,
        policy,
        speech,
        whole=args.whole or selection is not None,
        last_turn=args.last_turn,
    )
    return CaptureInput(snapshot, marks, policy, speech, args.whole, args.last_turn, selection)


def store_capture(
    prepared: CaptureInput,
    archive: MongoArchive,
    graph: GraphStore,
    sources: SourceLookup,
    encoder: CPUEncoder,
) -> dict[str, Any]:
    """Keep originals before manual notes; model extraction is never invoked by capture."""
    if not prepared.marks:
        return prepared.preview(True)
    import_snapshot(prepared.snapshot, archive)
    writer = CaptureWriter(graph, sources, encoder, prepared.policy, prepared.speech)
    result = writer.apply(prepared.marks[0].instance, prepared.marks)
    return {
        **result,
        "mode": "apply",
        "sources_verified": True,
        "graph_written": bool(
            result["inserted"] or result["topic_joins"] or result["topics_created"]
        ),
        "whole": prepared.whole,
        "last_turn": prepared.last_turn,
        "selection_pinned": prepared.selection is not None,
        "source_sha256": prepared.snapshot.sha256,
        "source_marks_digest": digest([mark.id for mark in prepared.marks]),
    }
