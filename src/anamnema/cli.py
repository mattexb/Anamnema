"""The single Anamnema command; setup currently offers read-only planning."""

from __future__ import annotations

import argparse
import json
import sys
from importlib.resources import files
from pathlib import Path

from anamnema.batch_cli import add_window_batch, run_window_batch
from anamnema.briefing_delivery_cli import add_briefing_delivery, run_briefing_delivery
from anamnema.briefing_refresh import add_briefing_refresh, run_briefing_refresh
from anamnema.capture_event_cli import add_capture_events, run_capture_events
from anamnema.continuity_cli import add_continuity, run_continuity
from anamnema.entity_link_cli import add_link_entities, run_link_entities
from anamnema.import_cli import run_import
from anamnema.install_cli import add_install, run_install
from anamnema.keyring_cli import add_keyring, run_keyring
from anamnema.memory_cli import COMMANDS, add_memory_commands, run_memory
from anamnema.nightly import add_nightly, run_nightly
from anamnema.process_cli import run_process
from anamnema.recall_hook import add_recall_hook, run_recall_hook
from anamnema.setup_answers import save_answers
from anamnema.setup_config import SetupError, load_config
from anamnema.setup_plan import inspect_host, plan
from anamnema.setup_wizard import Questions, WizardCancelled, collect, review


def _parser() -> argparse.ArgumentParser:
    """Declare the single command surface independently of execution."""
    parser = argparse.ArgumentParser(prog="anamnema")
    commands = parser.add_subparsers(dest="command", required=True)
    add_window_batch(
        commands.add_parser("batch-window", help="prepare or run archive/extraction/storage jobs")
    )
    add_briefing_delivery(
        commands.add_parser("briefing", help="inspect or deliver immutable recall pages")
    )
    add_keyring(
        commands.add_parser("keyring", help="store or verify credentials without printing values")
    )
    add_capture_events(
        commands.add_parser("capture-events", help="queue native capture and run its worker")
    )
    add_continuity(
        commands.add_parser("continuity", help="explicit Claude read/snapshot lifecycle")
    )
    add_link_entities(
        commands.add_parser("link-entities", help="link due notes to entities; dry-run default")
    )
    add_nightly(commands.add_parser("nightly", help="run the finishing chain after stored windows"))
    add_install(
        commands.add_parser("install", help="plan or apply the native logged-in-user profile")
    )
    add_recall_hook(
        commands.add_parser("recall-hook", help="UserPromptSubmit recall; native JSON on stdin")
    )
    add_briefing_refresh(
        commands.add_parser(
            "briefing-refresh", help="UserPromptSubmit rebuilt-briefing delivery; JSON on stdin"
        )
    )
    setup = commands.add_parser("setup", help="inspect prerequisites and plan native Debian setup")
    setup.add_argument("--config", type=Path, help="complete nonsecret setup JSON configuration")
    setup.add_argument("--backend", choices=("vulkan", "cpu", "cuda", "api"))
    setup.add_argument(
        "--save", type=Path, help="new nonsecret answer file; wizard confirmation required"
    )
    mode = setup.add_mutually_exclusive_group()
    mode.add_argument("--keyring-guide", action="store_true", help="read the packaged manual guide")
    mode.add_argument(
        "--wizard", action="store_true", help="collect and review nonsecret setup choices"
    )
    mode.add_argument("--dry-run", action="store_true", help="read-only planning (the default)")
    mode.add_argument(
        "--apply", action="store_true", help="reserved; installation is not implemented"
    )
    ingest = commands.add_parser("import", help="copy a Claude transcript without modifying it")
    ingest.add_argument("--file", type=Path, required=True)
    ingest.add_argument(
        "--instance", required=True, help="the assistant instance that owns this copy"
    )
    ingest.add_argument("--session", required=True, help="native session identity, not a file path")
    ingest.add_argument("--config", type=Path, help="project setup/keyring configuration")
    ingest.add_argument(
        "--archive-config", type=Path, help="complete nonsecret archive policy JSON"
    )
    ingest.add_argument(
        "--closed", action="store_true", help="explicitly attest the session is closed"
    )
    import_mode = ingest.add_mutually_exclusive_group()
    import_mode.add_argument(
        "--dry-run", action="store_true", help="parse/report only (the default)"
    )
    import_mode.add_argument(
        "--apply", action="store_true", help="write and verify the project archive"
    )
    process = commands.add_parser(
        "process", help="prepare conversations or explicitly extract notes"
    )
    process.add_argument("--file", type=Path, required=True)
    process.add_argument("--instance", required=True, help="assistant instance owning the source")
    process.add_argument("--session", required=True, help="explicit native session identity")
    process.add_argument("--archive-config", type=Path, help="complete nonsecret archive policy")
    process.add_argument("--speech-config", type=Path, help="complete nonsecret speech policy")
    process.add_argument(
        "--notes-config", type=Path, help="complete nonsecret chunk/request policy"
    )
    process.add_argument("--closed", action="store_true", help="attest the session has ended")
    process.add_argument("--config", type=Path, help="complete nonsecret local setup configuration")
    process.add_argument(
        "--extraction-config", type=Path, help="complete execution/collection policy"
    )
    process.add_argument(
        "--model-file", type=Path, help="explicit local model file for verified apply"
    )
    processing_mode = process.add_mutually_exclusive_group()
    processing_mode.add_argument(
        "--dry-run", action="store_true", help="read-only preview (default)"
    )
    processing_mode.add_argument(
        "--apply", action="store_true", help="verify archive, extract and save notes"
    )
    add_memory_commands(commands)
    return parser


def main(argv: list[str] | None = None) -> int:
    """Dispatch the selected workflow; unsupported execution fails visibly."""
    args = _parser().parse_args(argv)
    try:
        if args.command == "batch-window":
            print(json.dumps(run_window_batch(args), indent=2))
            return 0
        if args.command == "briefing":
            print(json.dumps(run_briefing_delivery(args), indent=2))
            return 0
        if args.command == "keyring":
            print(json.dumps(run_keyring(args, sys.stdin)))
            return 0
        if args.command == "capture-events":
            result = run_capture_events(args, sys.stdin)
            print(json.dumps(result))
            return 2 if result.get("failed") else 0
        if args.command == "continuity":
            print(json.dumps(run_continuity(args, sys.stdin)))
            return 0
        if args.command == "link-entities":
            print(json.dumps(run_link_entities(args), indent=2))
            return 0
        if args.command == "nightly":
            record = run_nightly(args)
            print(json.dumps(record, indent=2))
            return 2 if record["failed"] else 0
        if args.command == "recall-hook":
            print(json.dumps(run_recall_hook(args, sys.stdin)))
            return 0
        if args.command == "install":
            print(json.dumps(run_install(args)))
            return 0
        if args.command == "briefing-refresh":
            print(json.dumps(run_briefing_refresh(args, sys.stdin)))
            return 0
        if args.command in COMMANDS:
            result = run_memory(args)
            print(json.dumps(result, indent=2))
            return (
                2
                if args.command in {"brief", "name-notes"}
                and result.get("status") in {"failed", "partial"}
                else 0
            )
        if args.command == "process":
            print(json.dumps(run_process(args), indent=2))
            return 0
        if args.command == "import":
            print(json.dumps(run_import(args), indent=2))
            return 0
        if args.apply:
            raise SetupError("installation is not implemented; use setup --dry-run")
        if args.save and not args.wizard:
            raise SetupError("--save requires --wizard; dry-run never writes an answer file")
        if args.keyring_guide:
            print(files("anamnema").joinpath("guides/keyring.md").read_text(encoding="utf-8"))
            return 0
        config = load_config(args.config)
        if args.backend:
            config = config.model_copy(update={"backend": args.backend})
        saved = False
        if args.wizard:
            questions = Questions(sys.stdin, sys.stderr)
            config = collect(config, questions)
            if review(config, questions, args.save):
                save_answers(args.save, config)
                saved = True
        report = plan(config, inspect_host(config))
        if args.wizard:
            report.update(mode="wizard", answers_saved=saved)
        print(json.dumps(report, indent=2))
        return 0 if report["prerequisites_found"] else 2
    except WizardCancelled, KeyboardInterrupt:
        print(
            json.dumps({"error": f"{args.command} cancelled; verified work, if any, is retained"}),
            file=sys.stderr,
        )
        return 130
    except SetupError as exc:
        print(json.dumps({"error": str(exc)}), file=sys.stderr)
        return 2
