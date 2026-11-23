"""Explicit lifecycle commands; never register hooks or load live Claude settings."""

import argparse
import json
import sys
from pathlib import Path
from typing import Any, TextIO

from anamnema.archive_access import connect, keyring_uri
from anamnema.archive_mongo import MongoArchive
from anamnema.continuity import Continuity
from anamnema.continuity_config import (
    ContinuityConfig,
    ContinuityPolicy,
    continuity_config,
    continuity_policy,
)
from anamnema.continuity_state import source_text
from anamnema.ingest_config import archive_settings
from anamnema.setup_config import SetupError, load_config
from anamnema.snapshot_archive import MongoSnapshots, snapshot_archive_policy
from anamnema.snapshot_policy import producer_policy, worker_config
from anamnema.snapshot_producer import SnapshotProducer
from anamnema.snapshot_sync import SnapshotSync
from anamnema.speech_policy import speech_policy
from anamnema.substrates.claude_hooks import continuity_event, continuity_output
from anamnema.substrates.claude_snapshot import precompact_event


def add_continuity(parser: argparse.ArgumentParser) -> None:
    """Native lifecycle input belongs on stdin; snapshot staging takes an explicit file."""
    actions = parser.add_subparsers(dest="continuity_action", required=True)
    for action in ("start", "stop", "snapshot", "sync", "require", "precompute", "precompact"):
        command = actions.add_parser(action)
        command.add_argument("--continuity-config", type=Path, required=True)
        command.add_argument("--continuity-policy", type=Path)
        if action == "snapshot":
            command.add_argument("--session", required=True)
            command.add_argument("--file", type=Path, required=True)
        elif action in ("precompute", "precompact"):
            if action == "precompute":
                command.add_argument("--session", required=True)
                command.add_argument("--transcript", type=Path, required=True)
                command.add_argument("--trigger", required=True)
            command.add_argument("--worker-config", type=Path, required=True)
            command.add_argument("--producer-policy", type=Path)
            command.add_argument("--speech-policy", type=Path)
        elif action == "require":
            command.add_argument("--session", required=True)
            command.add_argument("--file", type=Path, required=True)
            command.add_argument("--producer", required=True)
            command.add_argument("--key", required=True)
        elif action == "sync":
            command.add_argument("--session", required=True)
            command.add_argument("--config", type=Path)
            command.add_argument("--archive-config", type=Path)
            command.add_argument("--snapshot-archive-policy", type=Path)
            mode = command.add_mutually_exclusive_group()
            mode.add_argument("--apply", action="store_true")
            mode.add_argument("--dry-run", action="store_true")


def _sync(
    args: argparse.Namespace, config: ContinuityConfig, policy: ContinuityPolicy
) -> dict[str, Any]:
    settings = snapshot_archive_policy(args.snapshot_archive_policy)
    sync = SnapshotSync(config, policy, args.session, settings)
    if not args.apply:
        return sync.preview()
    setup, archive = load_config(args.config), archive_settings(args.archive_config)
    with connect(keyring_uri(setup, archive), archive, setup.services.archive) as client:
        return sync.apply(MongoSnapshots(MongoArchive(client[archive.database], archive), settings))


def _payload(stream: TextIO, limit: int) -> object:
    raw = stream.read(limit + 1)
    if len(raw.encode("utf-8")) > limit:
        raise SetupError("Claude hook input exceeds configured bound")
    return json.loads(raw)


def _produce(args: argparse.Namespace, core: Continuity, stream: TextIO) -> dict[str, Any]:
    policy, worker = producer_policy(args.producer_policy), worker_config(args.worker_config)
    producer = SnapshotProducer(core, policy, worker, speech_policy(args.speech_policy))
    if args.continuity_action == "precompact":
        event = precompact_event(_payload(stream, core.policy.max_input_bytes), worker, policy)
        producer.produce(event.session, event.transcript, event.trigger)
        return {}
    return producer.produce(args.session, args.transcript, args.trigger)


def run_continuity(args: argparse.Namespace, stream: TextIO) -> dict[str, Any]:
    """Log completion without payloads/nonces; fail explicit read gates closed."""
    try:
        policy = continuity_policy(args.continuity_policy)
        config = continuity_config(args.continuity_config)
        core = Continuity(config, policy)
        if args.continuity_action in ("precompute", "precompact"):
            result = _produce(args, core, stream)
        elif args.continuity_action == "require":
            result = {
                "producer": args.producer,
                "key": args.key,
                "context": core.require(args.session, args.producer, args.key, args.file),
            }
        elif args.continuity_action == "sync":
            result = _sync(args, config, policy)
        elif args.continuity_action == "snapshot":
            content = source_text(args.file, policy.max_content_bytes)
            snapshot = core.stage(args.session, content)
            result = {"snapshot": snapshot.model_dump(), "session": args.session}
        else:
            event = continuity_event(
                _payload(stream, policy.max_input_bytes),
                args.continuity_action,
                policy,
                registry=config.registry,
            )
            context = (
                core.start(event.session, event.source)
                if args.continuity_action == "start"
                else core.stop(event.session, event.message)
            )
            result = continuity_output(args.continuity_action, context, policy)
        print("continuity SUCCESS", file=sys.stderr)
        return result
    except (SetupError, OSError, ValueError) as exc:
        reason = f"continuity ERROR: {exc}"
        print(reason, file=sys.stderr)
        if args.continuity_action == "stop":
            return {"decision": "block", "reason": reason}
        if args.continuity_action == "start":
            return {
                "systemMessage": reason,
                "hookSpecificOutput": {
                    "hookEventName": "SessionStart",
                    "additionalContext": reason,
                },
            }
        raise SetupError(reason) from exc
