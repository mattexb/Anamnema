"""Native enqueue and independent foreground worker commands; never register live hooks."""

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any, TextIO

from anamnema.archive_access import connect, keyring_uri
from anamnema.archive_mongo import MongoArchive
from anamnema.capture_archive import (
    MongoCaptureFailures,
    archive_capture_failure,
    capture_archive_policy,
    validate_archive_request,
)
from anamnema.capture_events import configuration_digest, request_from_event
from anamnema.capture_queue import CaptureQueue
from anamnema.capture_queue_config import capture_deployment, capture_queue_policy
from anamnema.capture_worker import drain_capture
from anamnema.ingest_config import archive_settings
from anamnema.private_files import PrivateStateBusy
from anamnema.setup_config import SetupError, load_config


def add_capture_events(parser: argparse.ArgumentParser) -> None:
    """Default actions inspect only; hook registration must explicitly select apply."""
    actions = parser.add_subparsers(dest="capture_action", required=True)
    for action in ("hook", "drain", "status", "retry", "archive-failure"):
        command = actions.add_parser(action)
        command.add_argument("--deployment", type=Path, required=True)
        command.add_argument("--queue-policy", type=Path)
        if action == "hook":
            command.add_argument("--event", choices=("stop", "precompact", "end"), required=True)
        if action == "drain":
            command.add_argument(
                "--watch", action="store_true", help="poll within the worker runtime bound"
            )
        if action in ("retry", "archive-failure"):
            command.add_argument("--job", required=True)
            command.add_argument("--reason", required=True)
        if action == "archive-failure":
            command.add_argument("--archive-policy", type=Path)
        if action == "retry":
            command.add_argument("--accept-config-change", action="store_true")
        if action != "status":
            mode = command.add_mutually_exclusive_group()
            mode.add_argument("--apply", action="store_true")
            mode.add_argument("--dry-run", action="store_true")


def _status(queue: CaptureQueue) -> dict[str, Any]:
    state = queue.inspect()
    return {
        "pending": [
            {"job": job.id, "event": job.event, "attempts": job.attempts, "error": job.error}
            for job in state.pending
        ],
        "history": [item.model_dump(mode="json") for item in state.history],
        "terminal_failed": [
            {"job": job.id, "event": job.event, "attempts": job.attempts, "error": job.error}
            for job in state.failed
        ],
    }


def _hook(args: argparse.Namespace, queue: CaptureQueue, stream: TextIO) -> dict[str, Any]:
    raw = stream.read(queue.policy.max_input_bytes + 1)
    if len(raw.encode("utf-8")) > queue.policy.max_input_bytes:
        raise SetupError("capture hook input exceeds configured bound")
    job = request_from_event(json.loads(raw), args.event, queue.deployment, queue.policy)
    if not args.apply:
        return {
            "mode": "dry-run",
            "queued": False,
            "expected_marks": len(job.expected) if job else 0,
        }
    if job is not None:
        queue.enqueue(job)
        print(f"capture QUEUED {job.id}", file=sys.stderr)
    print("capture hook SUCCESS", file=sys.stderr)
    return {}


def _drain(args: argparse.Namespace, queue: CaptureQueue) -> dict[str, Any]:
    if not args.apply:
        return {"mode": "dry-run", **_status(queue), "stores_opened": False}
    if not args.watch:
        return drain_capture(queue)
    deadline = time.monotonic() + queue.policy.max_worker_seconds
    completed, failed = 0, 0
    busy = 0
    while time.monotonic() < deadline:
        try:
            result = drain_capture(queue, deadline=deadline)
        except PrivateStateBusy:
            busy += 1
            if busy == 1:
                print("capture worker BUSY; retrying within runtime bound", file=sys.stderr)
        else:
            completed += result["completed"]
            failed += result["failed"]
        remaining = deadline - time.monotonic()
        if remaining > 0:
            time.sleep(min(queue.policy.poll_seconds, remaining))
    return {
        "completed": completed,
        "failed": failed,
        "pending": len(queue.inspect().pending),
        "worker_budget_exhausted": True,
        "busy_encounters": busy,
    }


def _archive_failure(args: argparse.Namespace, queue: CaptureQueue) -> dict[str, Any]:
    """Default preview opens no network or secrets; apply retains input until Mongo verifies it."""
    validate_archive_request(args.job, args.reason, queue.policy.max_reason_characters)
    if not args.apply:
        selected = [job for job in queue.inspect().failed if job.id == args.job]
        return {"mode": "dry-run", "terminal_requests": len(selected), "archived": False}
    deployment = queue.deployment
    setup = load_config(deployment.config)
    settings = archive_settings(deployment.archive_config)
    policy = capture_archive_policy(args.archive_policy)
    if policy.collection in settings.collections.values():
        raise SetupError("capture failure collection overlaps a native archive collection")
    client = connect(keyring_uri(setup, settings), settings, setup.services.archive)
    try:
        archive = MongoCaptureFailures(MongoArchive(client[settings.database], settings), policy)
        return archive_capture_failure(queue, archive, args.job, args.reason)
    finally:
        client.close()


def run_capture_events(args: argparse.Namespace, stream: TextIO) -> dict[str, Any]:
    """Native capture errors are visible context, never a required-read Stop gate."""
    try:
        queue = CaptureQueue(
            capture_deployment(args.deployment), capture_queue_policy(args.queue_policy)
        )
        if args.capture_action == "status":
            return _status(queue)
        if args.capture_action == "archive-failure":
            return _archive_failure(args, queue)
        if args.capture_action == "retry":
            if not args.apply:
                return {"mode": "dry-run", "job": args.job, "retried": False}
            rebound = (
                configuration_digest(queue.deployment, queue.policy)
                if args.accept_config_change
                else None
            )
            return {"retried": True, "job": queue.retry(args.job, args.reason, rebound)}
        if args.capture_action == "hook":
            return _hook(args, queue, stream)
        return _drain(args, queue)
    except (SetupError, OSError, ValueError) as exc:
        detail = str(exc) if isinstance(exc, SetupError) else "input validation failed"
        if isinstance(exc, OSError):
            detail = exc.strerror or "local IO failed"
        elif isinstance(exc, json.JSONDecodeError):
            detail = f"invalid native JSON at line {exc.lineno}, column {exc.colno}"
        reason = f"capture {args.capture_action} ERROR ({type(exc).__name__}): {detail}"
        if args.capture_action != "hook" or not args.apply:
            raise SetupError(reason) from exc
        print(reason, file=sys.stderr)
        return {"systemMessage": reason}
