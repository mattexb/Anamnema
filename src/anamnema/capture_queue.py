"""Durable native capture requests stay independent from required-read obligations."""

import re
import time
import uuid
from collections.abc import Callable, Generator
from contextlib import contextmanager
from pathlib import Path
from typing import Annotated, Literal, Self, get_args

from pydantic import Field, JsonValue, model_validator

from anamnema.archive_models import digest
from anamnema.capture_queue_config import CaptureDeployment, CaptureQueuePolicy
from anamnema.capture_selection import CaptureSelection
from anamnema.private_files import PrivateFiles, PrivateStateBusy
from anamnema.setup_config import Nonempty, SetupError, StrictModel

CaptureFailure = Literal[
    "unflushed",
    "configuration_changed",
    "capture_failed",
    "deadline",
    "spawn_failed",
    "invalid_transcript",
]
CaptureDigest = Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]


class CaptureJob(StrictModel):
    """Expected marker hashes are not provisional notes or archived source identities."""

    id: Annotated[str, Field(pattern=r"^[a-f0-9]{32}$")]
    session: Nonempty
    transcript: Path
    event: Literal["stop", "precompact", "end"]
    expected: tuple[CaptureDigest, ...]
    config_digest: CaptureDigest
    selection: CaptureSelection
    attempts: Annotated[int, Field(ge=0)] = 0
    error: CaptureFailure | None = None
    not_before: Annotated[float, Field(ge=0, allow_inf_nan=False)] = 0.0

    @model_validator(mode="after")
    def stable_coordinates(self) -> Self:
        if not self.transcript.is_absolute() or any(c in self.session for c in ("\n", "\r")):
            raise ValueError(
                "capture coordinates require an absolute transcript and single-line session"
            )
        return self


class CaptureResult(StrictModel):
    """Bounded operational summaries never contain transcript text or credential values."""

    job: Nonempty
    summary: dict[str, JsonValue]


class CaptureQueueState(StrictModel):
    """A completed note write is safe to replay if the completion record was interrupted."""

    instance: Nonempty
    pending: tuple[CaptureJob, ...] = ()
    failed: tuple[CaptureJob, ...] = ()
    history: tuple[CaptureResult, ...] = ()


class CaptureQueue:
    """Journal locks are short; a separate worker lock can span network execution."""

    def __init__(self, deployment: CaptureDeployment, policy: CaptureQueuePolicy) -> None:
        self.deployment, self.policy = deployment, policy
        self.files = PrivateFiles(policy.directory_mode, policy.file_mode, policy.temporary_prefix)
        self.folder = deployment.state_root / digest(deployment.instance)

    def _prepare(self) -> None:
        self.files.directory(self.deployment.state_root)
        self.files.directory(self.folder)

    def _load(self) -> CaptureQueueState:
        path = self.folder / self.policy.state_file
        try:
            state = (
                CaptureQueueState.model_validate_json(
                    self.files.read(path, self.policy.max_state_bytes)
                )
                if path.exists() or path.is_symlink()
                else CaptureQueueState(instance=self.deployment.instance)
            )
        except (OSError, ValueError) as exc:
            raise SetupError(f"capture queue cannot be read ({type(exc).__name__})") from exc
        if state.instance != self.deployment.instance:
            raise SetupError("capture queue belongs to another assistant")
        if (
            len(state.pending) > self.policy.max_pending
            or len(state.failed) > self.policy.max_failed
            or len(state.history) > self.policy.max_history
            or len({job.id for job in (*state.pending, *state.failed)})
            != len(state.pending) + len(state.failed)
        ):
            raise SetupError("capture queue is invalid or exceeds configured bounds")
        for job in (*state.pending, *state.failed):
            self._ownership(job)
        return state

    def _ownership(self, job: CaptureJob) -> None:
        if (
            job.selection.instance != self.deployment.instance
            or job.selection.session != job.session
            or job.selection.transcript != job.transcript
            or job.selection.mode != ("turn" if job.event == "stop" else "stretch")
        ):
            raise SetupError("capture selection ownership or event differs from request")
        if not any(
            job.transcript.is_relative_to(root) for root in self.deployment.transcript_roots
        ):
            raise SetupError("capture transcript is outside configured roots")

    def _coordinates(self, job: CaptureJob) -> None:
        self._ownership(job)
        self.deployment.check_transcript(job.transcript)

    def _save(self, state: CaptureQueueState) -> None:
        data = state.model_dump_json().encode("utf-8")
        if len(data) > self.policy.max_state_bytes:
            raise SetupError("capture queue exceeds configured byte bound; prior journal retained")
        self.files.save(self.folder / self.policy.state_file, data)

    def inspect(self) -> CaptureQueueState:
        """Status is read-only, including before this instance has ever scheduled work."""
        for path in (self.deployment.state_root, self.folder):
            if path.is_symlink():
                raise SetupError("capture queue path cannot be a symlink")
            if not path.exists():
                return CaptureQueueState(instance=self.deployment.instance)
            self.files.existing_directory(path)
        return self._load()

    def _change(self, update: Callable[[CaptureQueueState], CaptureQueueState]) -> None:
        self._prepare()
        deadline = time.monotonic() + self.policy.lock_wait_seconds
        while True:
            try:
                with self.files.locked(self.folder / self.policy.lock_file):
                    self._cleanup_temporary()
                    self._save(update(self._load()))
                return
            except PrivateStateBusy:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise
                time.sleep(min(self.policy.lock_poll_seconds, remaining))

    def _cleanup_temporary(self) -> None:
        """Only unreferenced reserved atomic-write files are removable under the journal lock."""
        self.files.cleanup_temporary(self.folder, self.policy.max_temporary_files)

    def enqueue(self, job: CaptureJob) -> str:
        """No pending request is silently dropped to make room for a newer event."""
        self._coordinates(job)

        def update(state: CaptureQueueState) -> CaptureQueueState:
            if any(existing.id == job.id for existing in (*state.pending, *state.failed)):
                raise SetupError("capture queue repeats a job identity")
            if len(state.pending) >= self.policy.max_pending:
                raise SetupError("capture queue is full; retry or drain pending work")
            return state.model_copy(update={"pending": (*state.pending, job)})

        self._change(update)
        return job.id

    def failed(self, job: CaptureJob, category: CaptureFailure) -> None:
        """A failure keeps exact pending input and increments its visible attempt count."""
        if category not in get_args(CaptureFailure):
            raise SetupError("capture error category must not contain payload text")

        def update(state: CaptureQueueState) -> CaptureQueueState:
            self._same_job(state, job)
            current = next(item for item in state.pending if item.id == job.id)
            changed = current.model_copy(
                update={
                    "attempts": current.attempts + 1,
                    "error": category,
                    "not_before": time.time() + self.policy.retry_seconds,
                }
            )
            terminal = (
                category == "configuration_changed" or changed.attempts >= self.policy.max_attempts
            )
            if terminal and len(state.failed) >= self.policy.max_failed:
                raise SetupError(
                    "capture terminal-failure quota is full; exact request retained pending"
                )
            pending = tuple(item for item in state.pending if item.id != job.id)
            return state.model_copy(
                update={
                    "pending": pending if terminal else (*pending, changed),
                    "failed": (*state.failed, changed) if terminal else state.failed,
                }
            )

        self._change(update)

    def complete(self, job: CaptureJob, summary: dict[str, JsonValue]) -> None:
        """Commit success before forgetting pending work; immutable source IDs make retry safe."""

        def update(state: CaptureQueueState) -> CaptureQueueState:
            self._same_job(state, job)
            history = (*state.history, CaptureResult(job=job.id, summary=summary))
            return state.model_copy(
                update={
                    "pending": tuple(item for item in state.pending if item.id != job.id),
                    "history": history[-self.policy.max_history :],
                }
            )

        self._change(update)

    def _same_job(self, state: CaptureQueueState, job: CaptureJob) -> None:
        current = next((item for item in state.pending if item.id == job.id), None)
        if current is None or current.model_dump(
            exclude={"attempts", "error", "not_before"}
        ) != job.model_dump(exclude={"attempts", "error", "not_before"}):
            raise SetupError("capture request changed or is no longer pending")

    def retry(self, identity: str, reason: str, config_digest: str | None = None) -> str:
        """Explicit rebinding clones a failed request; the original remains in the failed list."""
        if not reason.strip() or len(reason) > self.policy.max_reason_characters:
            raise SetupError("capture retry requires a bounded reason")
        if config_digest is not None and re.fullmatch(r"[a-f0-9]{64}", config_digest) is None:
            raise SetupError("invalid capture retry configuration identity")
        new_id = uuid.uuid4().hex if config_digest is not None else identity

        def update(state: CaptureQueueState) -> CaptureQueueState:
            previous = next((item for item in state.failed if item.id == identity), None)
            if previous is None:
                raise SetupError("capture retry must name a terminal failed request")
            if len(state.pending) >= self.policy.max_pending:
                raise SetupError("capture queue is full; retry not applied")
            retried = previous.model_copy(
                update={
                    "id": new_id,
                    "attempts": 0,
                    "error": None,
                    "not_before": 0.0,
                    "config_digest": config_digest or previous.config_digest,
                }
            )
            history = (
                *state.history,
                CaptureResult(
                    job=new_id,
                    summary={
                        "operation": "retry",
                        "from_job": identity,
                        "reason": reason,
                        "config_rebound": config_digest is not None,
                    },
                ),
            )
            return state.model_copy(
                update={
                    "pending": (*state.pending, retried),
                    "failed": state.failed
                    if config_digest is not None
                    else tuple(item for item in state.failed if item.id != identity),
                    "history": history[-self.policy.max_history :],
                }
            )

        self._change(update)
        return new_id

    def retire(self, expected: CaptureJob, archive_digest: str, reason: str) -> bool:
        """Remove only the unchanged failed snapshot after its archive has been verified."""
        self._ownership(expected)
        if (
            expected.error is None
            or re.fullmatch(r"[a-f0-9]{64}", archive_digest) is None
            or not reason.strip()
            or len(reason) > self.policy.max_reason_characters
        ):
            raise SetupError("capture archival needs a failed request, archive digest and reason")
        retired = False

        def update(state: CaptureQueueState) -> CaptureQueueState:
            nonlocal retired
            current = next((item for item in state.failed if item.id == expected.id), None)
            if current is None:
                return state
            if current != expected:
                raise SetupError("failed request changed while archiving; local input retained")
            history = (
                *state.history,
                CaptureResult(
                    job=expected.id,
                    summary={
                        "operation": "archive_failure",
                        "archive": archive_digest,
                        "reason": reason,
                    },
                ),
            )
            retired = True
            return state.model_copy(
                update={
                    "failed": tuple(item for item in state.failed if item.id != expected.id),
                    "history": history[-self.policy.max_history :],
                }
            )

        self._change(update)
        return retired

    @contextmanager
    def worker(self) -> Generator[None]:
        """OS-owned lock releases on process death; pending records remain unchanged."""
        self._prepare()
        with self.files.locked(self.folder / self.policy.worker_lock_file):
            yield


def new_capture_job(
    session: str,
    transcript: Path,
    event: Literal["stop", "precompact", "end"],
    expected: tuple[str, ...],
    config_digest: str,
    selection: CaptureSelection,
) -> CaptureJob:
    """Repeated native events may enqueue twice, but never mint different archived note IDs."""
    return CaptureJob(
        id=uuid.uuid4().hex,
        session=session,
        transcript=transcript,
        event=event,
        expected=expected,
        config_digest=config_digest,
        selection=selection,
    )
