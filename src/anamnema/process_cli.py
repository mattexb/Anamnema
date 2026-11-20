"""Read-only preparation by default; explicit verified extraction when requested."""

import argparse

from pydantic import JsonValue

from anamnema.archive_models import digest
from anamnema.binding import bindings
from anamnema.chunks import Chunk, chunks, estimated_tokens
from anamnema.ingest_config import archive_settings
from anamnema.notes_policy import notes_policy
from anamnema.process_apply import apply
from anamnema.processing import Window, prepare
from anamnema.setup_config import SetupError
from anamnema.speech_policy import speech_policy
from anamnema.substrates.claude import ClaudeAdapter
from anamnema.substrates.claude_speech import ClaudeSpeechReader


def run_process(args: argparse.Namespace) -> dict[str, JsonValue]:
    """Prepare common closed stretches; only explicit apply accesses infrastructure."""
    if args.apply and args.model_file is None:
        raise SetupError("process --apply requires --model-file; dry-run never calls a model")
    policy = speech_policy(args.speech_config)
    notes = notes_policy(args.notes_config)
    archive = archive_settings(args.archive_config)
    adapter = ClaudeAdapter(archive)
    snapshot = adapter.read(args.file, args.instance, args.session, closed=args.closed)
    prepared = prepare(snapshot)
    report = prepared.report(snapshot)
    reader = ClaudeSpeechReader(policy)
    speech: list[JsonValue] = []
    inputs: list[tuple[Window, tuple[Chunk, ...]]] = []
    for window in prepared.windows:
        result = reader.read(window)
        found = bindings(result.rows, notes)
        parts = chunks(result.rows, found, notes)
        inputs.append((window, parts))
        speech.append(
            {
                "window": window.report()["key"],
                "eligible_rows": len(result.rows),
                "rows_masked": result.masked,
                "bindings": len(found),
                "chunks": [
                    {
                        "index": part.index,
                        "characters": len(part.text),
                        "estimated_tokens": estimated_tokens(part.text, notes),
                        "rows": [row.n for row in part.rows],
                    }
                    for part in parts
                ],
                "dropped": dict(result.dropped),
                "rows": [
                    {
                        "n": row.n,
                        "speaker": row.speaker,
                        "source": row.source.identity,
                        "line": row.source.source_line,
                    }
                    for row in result.rows
                ],
            }
        )
    report["speech"] = speech
    report["speech_policy_sha256"] = digest(policy.model_dump(mode="json"))
    report["notes_policy_sha256"] = digest(notes.model_dump(mode="json"))
    if args.apply:
        report.update(apply(args, snapshot, inputs, archive, notes, policy))
    return report
