"""Prepare, execute and inspect the local three-stage window pipeline; not nightly finishing."""

import argparse
from pathlib import Path
from typing import Any

from anamnema.batch_config import window_batch_config
from anamnema.batch_journal import BatchJournal
from anamnema.batch_models import BatchState, batch_policy
from anamnema.batch_prepare import PreparedInputs, preparation_policy
from anamnema.batch_runner import BatchRunner
from anamnema.batch_runtime import window_runtime
from anamnema.ingest_config import archive_settings
from anamnema.processing import prepare
from anamnema.setup_config import SetupError
from anamnema.substrates.claude import ClaudeAdapter


def add_window_batch(parser: argparse.ArgumentParser) -> None:
    actions = parser.add_subparsers(dest="batch_action", required=True)
    for name in ("prepare", "run", "status"):
        command = actions.add_parser(name)
        command.add_argument("--deployment", type=Path, required=True)
        if name == "prepare":
            command.add_argument("--file", type=Path, required=True)
            command.add_argument("--session", required=True)
            command.add_argument(
                "--closed", action="store_true", help="explicitly attest the session ended"
            )
        elif name == "run":
            command.add_argument("--input", required=True, help="prepared window identity")
            command.add_argument("--resume", help="exact existing job identity")
        else:
            command.add_argument("--job", required=True)
        if name != "status":
            mode = command.add_mutually_exclusive_group()
            mode.add_argument("--apply", action="store_true")
            mode.add_argument("--dry-run", action="store_true")


def run_window_batch(args: argparse.Namespace) -> dict[str, Any]:
    config = window_batch_config(args.deployment)
    policy = batch_policy(config.journal_policy)
    if args.batch_action == "status":
        journal = BatchJournal.open_existing(config.jobs_root, config.instance, args.job, policy)
        return _report(journal.inspect())
    inputs = PreparedInputs(config.inputs_root, preparation_policy(config.preparation_policy))
    if args.batch_action == "prepare":
        snapshot = ClaudeAdapter(archive_settings(config.archive_config)).read(
            args.file, config.instance, args.session, closed=args.closed
        )
        prepared = prepare(snapshot)
        paths = [inputs.save(window) for window in prepared.windows] if args.apply else []
        return {
            "mode": "apply" if args.apply else "dry-run",
            "closed_windows": len(prepared.windows),
            "pending_records": len(prepared.pending),
            "inputs": [path.parent.name for path in paths],
            "models_called": False,
            "archive_written": False,
        }
    manifest = inputs.manifest(config.instance, args.input)
    if not args.apply:
        return {
            "mode": "dry-run",
            "input": manifest.identity,
            "scope": "archive-extract-store",
            "infrastructure_accessed": False,
            "nightly_complete": False,
        }
    with window_runtime(config, args.input) as core:
        journal = BatchJournal(config.jobs_root, core.plan(), policy, identity=args.resume)
        try:
            result = BatchRunner(journal, core.operations()).run()
        except SetupError as exc:
            raise SetupError(f"window batch {journal.plan.identity} failed: {exc}") from exc
        retired = core.archive.retire_payload(journal)
        return {**_report(result), "raw_payload_removed": retired}


def _report(state: BatchState) -> dict[str, Any]:
    return {
        "job": state.plan.identity,
        "instance": state.plan.instance,
        "session": state.plan.session,
        "scope": "archive-extract-store",
        "complete": state.complete,
        "nightly_complete": False,
        "stages": [stage.model_dump(mode="json") for stage in state.stages],
    }
