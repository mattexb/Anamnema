"""Explicit native capture deployment and a bounded independent queue policy."""

import re
from importlib.resources import files
from pathlib import Path
from typing import Literal, Self

from pydantic import Field, ValidationError, model_validator

from anamnema.setup_config import Nonempty, SetupError, StrictModel


class CaptureDeployment(StrictModel):
    """The deployment, never a hook payload, chooses owner and service configuration."""

    instance: Nonempty
    state_root: Path
    cwd: Path
    transcript_roots: tuple[Path, ...]
    config: Path
    archive_config: Path | None = None
    memory_config: Path | None = None
    search_config: Path | None = None
    speech_config: Path | None = None
    capture_config: Path | None = None

    @model_validator(mode="after")
    def explicit_paths(self) -> Self:
        paths = [self.state_root, self.cwd, self.config, *self.transcript_roots]
        paths += [
            p
            for p in (
                self.archive_config,
                self.memory_config,
                self.search_config,
                self.speech_config,
                self.capture_config,
            )
            if p is not None
        ]
        if (
            not self.transcript_roots
            or any(not p.is_absolute() for p in paths)
            or any(p == p.parent for p in (self.state_root, *self.transcript_roots))
        ):
            raise ValueError("capture paths must be explicit absolute non-root paths")
        if any(any(c in str(p) for c in ("\n", "\r")) for p in paths):
            raise ValueError("capture paths must fit single lines")
        if any(p.is_symlink() for path in paths for p in (path, *path.parents)):
            raise ValueError("capture paths cannot have symlink ancestors")
        return self

    def check_transcript(self, path: Path) -> None:
        """Recheck permitted coordinates before every read, not just when scheduling."""
        if not path.is_absolute() or not any(
            path.is_relative_to(root) for root in self.transcript_roots
        ):
            raise SetupError("capture transcript is outside configured roots")
        if any(part.is_symlink() for part in (path, *path.parents)):
            raise SetupError("capture transcript cannot have symlink ancestors")


class CaptureQueuePolicy(StrictModel):
    """Native fields, local resource bounds and permissions remain deployable data."""

    max_input_bytes: int = Field(gt=0)
    hook_timeout_seconds: float = Field(gt=0, allow_inf_nan=False)
    hook_budget_fraction: float = Field(gt=0, lt=1, allow_inf_nan=False)
    max_state_bytes: int = Field(gt=0)
    max_pending: int = Field(gt=0)
    max_history: int = Field(gt=0)
    max_failed: int = Field(gt=0)
    max_attempts: int = Field(gt=0)
    max_reason_characters: int = Field(gt=0)
    retry_seconds: float = Field(gt=0, allow_inf_nan=False)
    max_temporary_files: int = Field(gt=0)
    max_message_characters: int = Field(gt=0)
    wait_seconds: float = Field(gt=0, allow_inf_nan=False)
    poll_seconds: float = Field(gt=0, allow_inf_nan=False)
    lock_wait_seconds: float = Field(gt=0, allow_inf_nan=False)
    lock_poll_seconds: float = Field(gt=0, allow_inf_nan=False)
    job_timeout_seconds: float = Field(gt=0, allow_inf_nan=False)
    max_worker_seconds: float = Field(gt=0, allow_inf_nan=False)
    max_output_bytes: int = Field(gt=0)
    read_chunk_bytes: int = Field(gt=0)
    directory_mode: int
    file_mode: int
    temporary_prefix: Nonempty
    state_file: Nonempty
    lock_file: Nonempty
    worker_lock_file: Nonempty
    native_events: dict[Literal["stop", "precompact", "end"], Nonempty]
    native_fields: dict[Literal["event", "session", "transcript", "message", "cwd"], Nonempty]

    @model_validator(mode="after")
    def safe_policy(self) -> Self:
        if self.directory_mode != 0o700 or self.file_mode != 0o600:
            raise ValueError("capture queue files must be owner-only")
        if re.fullmatch(r"\.[A-Za-z0-9_-]+", self.temporary_prefix) is None:
            raise ValueError("capture temporary prefix must be a reserved local name")
        names = (self.state_file, self.lock_file, self.worker_lock_file)
        if len(set(names)) != len(names) or any(
            Path(n).name != n
            or "\\" in n
            or n in (".", "..")
            or n.startswith(self.temporary_prefix)
            for n in names
        ):
            raise ValueError("capture journal/lock names must be distinct safe basenames")
        if (
            set(self.native_events) != {"stop", "precompact", "end"}
            or set(self.native_fields) != {"event", "session", "transcript", "message", "cwd"}
            or self.poll_seconds > self.wait_seconds
            or self.lock_poll_seconds > self.lock_wait_seconds
        ):
            raise ValueError("capture native protocol or polling bounds are incomplete")
        return self

    @property
    def timeout_seconds(self) -> float:
        """The shared bounded-process transport uses the per-job invocation deadline."""
        return self.job_timeout_seconds


def capture_queue_policy(path: Path | None = None) -> CaptureQueuePolicy:
    """Read a complete public queue policy without opening stores or user settings."""
    try:
        text = (
            path.read_text(encoding="utf-8")
            if path
            else files("anamnema").joinpath("config/capture_queue.json").read_text(encoding="utf-8")
        )
        return CaptureQueuePolicy.model_validate_json(text)
    except (OSError, UnicodeError, ValidationError) as exc:
        raise SetupError(f"invalid capture queue policy ({type(exc).__name__})") from exc


def capture_deployment(path: Path) -> CaptureDeployment:
    """No native payload supplies an alternate credential selector or owning assistant."""
    try:
        return CaptureDeployment.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValidationError) as exc:
        raise SetupError(f"invalid capture deployment ({type(exc).__name__})") from exc
