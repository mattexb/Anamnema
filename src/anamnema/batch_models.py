"""Strict resumable batch state: immutable plans, ordered stages and retained attempts."""

import re
from importlib.resources import files
from pathlib import Path
from typing import Annotated, Literal, Self

from pydantic import Field, ValidationError, model_validator

from anamnema.archive_models import digest
from anamnema.setup_config import Nonempty, SetupError, StrictModel

ContentDigest = Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
FailureCategory = Literal[
    "operation_failed", "interrupted", "timeout", "invalid_output", "input_changed"
]
Metrics = dict[str, int | float | bool]


class BatchPlan(StrictModel):
    """The driver supplies an immutable prepared input, not a moving native transcript tail."""

    instance: Nonempty
    session: Nonempty
    input_path: Path
    input_digest: ContentDigest
    profile_digest: ContentDigest
    stages: tuple[Nonempty, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def explicit_plan(self) -> Self:
        if (
            not self.input_path.is_absolute()
            or not self.instance.strip()
            or not self.session.strip()
        ):
            raise ValueError("batch owner/session and absolute prepared input are required")
        if len(self.stages) != len(set(self.stages)) or any(
            re.fullmatch(r"[a-z][a-z0-9_-]*", stage) is None for stage in self.stages
        ):
            raise ValueError("batch stage names must be distinct plain identifiers")
        return self

    @property
    def identity(self) -> str:
        return digest(self.model_dump(mode="json"))


class BatchAttempt(StrictModel):
    """Interrupted and failed attempts remain visible after a later successful replay."""

    status: Literal["running", "failed", "success"]
    category: FailureCategory | None = None
    metrics: Metrics = Field(default_factory=dict)

    @model_validator(mode="after")
    def coherent_failure(self) -> Self:
        if (self.status == "failed") != (self.category is not None):
            raise ValueError("only failed batch attempts carry a required failure category")
        return self


class BatchStage(StrictModel):
    name: Nonempty
    attempts: tuple[BatchAttempt, ...] = ()

    @model_validator(mode="after")
    def earlier_attempts_failed(self) -> Self:
        if any(attempt.status != "failed" for attempt in self.attempts[:-1]):
            raise ValueError("only the latest batch attempt may be running or successful")
        return self

    @property
    def complete(self) -> bool:
        return bool(self.attempts and self.attempts[-1].status == "success")


class BatchState(StrictModel):
    version: Literal[1] = 1
    plan: BatchPlan
    stages: tuple[BatchStage, ...]

    @model_validator(mode="after")
    def sequential(self) -> Self:
        if tuple(stage.name for stage in self.stages) != self.plan.stages:
            raise ValueError("batch stages differ from the pinned plan")
        blocked = False
        for stage in self.stages:
            if blocked and stage.attempts:
                raise ValueError("a later batch stage ran before an earlier stage succeeded")
            blocked |= not stage.complete
        return self

    @property
    def complete(self) -> bool:
        return all(stage.complete for stage in self.stages)


class BatchPolicy(StrictModel):
    max_input_bytes: int = Field(gt=0)
    read_chunk_bytes: int = Field(gt=0)
    max_state_bytes: int = Field(gt=0)
    max_stages: int = Field(gt=0)
    max_attempts: int = Field(gt=0)
    max_metrics: int = Field(gt=0)
    max_metric_characters: int = Field(gt=0)
    max_temporary_files: int = Field(gt=0)
    directory_mode: int
    file_mode: int
    temporary_prefix: Nonempty
    state_file: Nonempty
    lock_file: Nonempty
    worker_lock_file: Nonempty

    @model_validator(mode="after")
    def safe_names(self) -> Self:
        if self.directory_mode != 0o700 or self.file_mode != 0o600:
            raise ValueError("batch state must be owner-only")
        if re.fullmatch(r"\.[A-Za-z0-9_-]+", self.temporary_prefix) is None:
            raise ValueError("batch temporary prefix must be a reserved local name")
        names = (self.state_file, self.lock_file, self.worker_lock_file)
        if len(names) != len(set(names)) or any(
            Path(name).name != name
            or name in {".", ".."}
            or "\\" in name
            or name.startswith(self.temporary_prefix)
            for name in names
        ):
            raise ValueError("batch journal/lock names must be distinct safe basenames")
        return self


def batch_policy(path: Path | None = None) -> BatchPolicy:
    try:
        raw = (
            path.read_text(encoding="utf-8")
            if path
            else files("anamnema").joinpath("config/batch.json").read_text(encoding="utf-8")
        )
        return BatchPolicy.model_validate_json(raw)
    except (OSError, UnicodeError, ValidationError) as exc:
        raise SetupError("batch policy cannot be read or validated") from exc
