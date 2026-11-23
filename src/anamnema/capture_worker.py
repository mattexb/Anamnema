"""Bounded foreground queue execution; scheduling never implies successful capture."""

import sys
import time
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue

from anamnema.archive_models import digest
from anamnema.capture_cli import CaptureInput
from anamnema.capture_events import configuration_digest, expectations_present, prepare_job
from anamnema.capture_queue import CaptureFailure, CaptureJob, CaptureQueue
from anamnema.capture_queue_config import CaptureDeployment, CaptureQueuePolicy
from anamnema.setup_config import SetupError
from anamnema.snapshot_worker import run_worker


class CapturePending(SetupError):
    """An actionable bounded failure retains the exact queued request for retry."""

    category: CaptureFailure

    def __init__(self, category: CaptureFailure) -> None:
        self.category = category
        super().__init__(f"capture request remains pending ({category})")


class CaptureChildReport(BaseModel):
    """Validate only the child's count/proof contract; never retain surplus output fields."""

    model_config = ConfigDict(strict=True, extra="ignore")
    mode: Literal["apply"]
    marks: int = Field(ge=0)
    notes_model_calls: int = Field(ge=0, le=0)
    source_marks_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    sources_verified: bool
    inserted: int = Field(default=0, ge=0)
    already_present: int = Field(default=0, ge=0)
    not_materialized: int = Field(default=0, ge=0)
    placed_again: int = Field(default=0, ge=0)
    topics_created: int = Field(default=0, ge=0)
    topic_joins: int = Field(default=0, ge=0)


def _ready(
    job: CaptureJob, deployment: CaptureDeployment, policy: CaptureQueuePolicy, deadline: float
) -> CaptureInput:
    until = min(deadline, time.monotonic() + policy.wait_seconds)
    category: CaptureFailure = "unflushed"
    while True:
        if configuration_digest(deployment, policy) != job.config_digest:
            raise CapturePending("configuration_changed")
        try:
            prepared = prepare_job(job, deployment)
            if expectations_present(job, prepared):
                return prepared
            category = "unflushed"
        except SetupError, OSError, ValueError:
            category = "invalid_transcript"
        remaining = until - time.monotonic()
        if remaining <= 0:
            raise CapturePending(category)
        time.sleep(min(policy.poll_seconds, remaining))


def _command(job: CaptureJob, deployment: CaptureDeployment) -> list[str]:
    command = [
        sys.executable,
        "-m",
        "anamnema",
        "capture",
        "--apply",
        "--instance",
        deployment.instance,
        "--file",
        str(job.transcript),
        "--session",
        job.session,
        "--config",
        str(deployment.config),
    ]
    command.extend(["--selection", job.selection.model_dump_json()])
    for field in (
        "archive_config",
        "memory_config",
        "search_config",
        "speech_config",
        "capture_config",
    ):
        value = getattr(deployment, field)
        if value is not None:
            command.extend(["--" + field.replace("_", "-"), str(value)])
    return command


def execute_job(
    job: CaptureJob, deployment: CaptureDeployment, policy: CaptureQueuePolicy, deadline: float
) -> dict[str, JsonValue]:
    """Use a bounded installed CLI child; never count a changed source selection as complete."""
    prepared = _ready(job, deployment, policy, deadline)
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise CapturePending("deadline")
    bounds = policy.model_copy(
        update={"job_timeout_seconds": min(policy.job_timeout_seconds, remaining)}
    )
    try:
        output = run_worker(_command(job, deployment), deployment, bounds, "capture worker")
        result = CaptureChildReport.model_validate_json(output)
    except (SetupError, OSError, ValueError) as exc:
        raise CapturePending("capture_failed") from exc
    if configuration_digest(deployment, policy) != job.config_digest:
        raise CapturePending("configuration_changed")
    if (
        result.source_marks_digest != digest([mark.id for mark in prepared.marks])
        or result.marks != len(prepared.marks)
        or prepared.marks
        and result.sources_verified is not True
    ):
        raise CapturePending("capture_failed")
    return result.model_dump(mode="json", exclude={"mode", "sources_verified"})


def drain_capture(
    queue: CaptureQueue, *, deadline: float | None = None, seen: set[str] | None = None
) -> dict[str, Any]:
    """One foreground worker skips failed jobs for this pass instead of poisoning later work."""
    done, failed = 0, 0
    attempted: set[str] = set() if seen is None else seen
    deadline = min(
        deadline if deadline is not None else float("inf"),
        time.monotonic() + queue.policy.max_worker_seconds,
    )
    with queue.worker():
        while time.monotonic() < deadline:
            pending = [
                job
                for job in queue.inspect().pending
                if job.id not in attempted and job.not_before <= time.time()
            ]
            if not pending:
                break
            job = pending[0]
            attempted.add(job.id)
            try:
                result = execute_job(job, queue.deployment, queue.policy, deadline)
            except CapturePending as exc:
                queue.failed(job, exc.category)
                failed += 1
            else:
                queue.complete(job, result)
                done += 1
    return {
        "completed": done,
        "failed": failed,
        "pending": len(queue.inspect().pending),
        "worker_budget_exhausted": time.monotonic() >= deadline,
    }
