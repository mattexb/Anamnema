"""Private ordered batch journal; worker ownership never spans a short state-file lock."""

import re
from collections.abc import Callable, Generator
from contextlib import contextmanager
from pathlib import Path

from pydantic import ValidationError

from anamnema.archive_models import canonical, digest
from anamnema.batch_input import input_digest
from anamnema.batch_models import (
    BatchAttempt,
    BatchPlan,
    BatchPolicy,
    BatchStage,
    BatchState,
    FailureCategory,
    Metrics,
)
from anamnema.private_files import PrivateFiles
from anamnema.setup_config import SetupError


class BatchJournal:
    """The prepared input/profile/stage identity cannot silently change on resume."""

    def __init__(
        self, root: Path, plan: BatchPlan, policy: BatchPolicy, identity: str | None = None
    ) -> None:
        if not root.is_absolute() or root == root.parent:
            raise SetupError("batch root must be an explicit absolute non-root directory")
        if identity is not None and identity != plan.identity:
            raise SetupError("batch input/profile/stage plan changed; automatic resume refused")
        if len(plan.stages) > policy.max_stages:
            raise SetupError("batch plan exceeds configured stage limit")
        self.root, self.plan, self.policy = root, plan, policy
        self.folder = root / digest(plan.instance) / plan.identity
        self.files = PrivateFiles(policy.directory_mode, policy.file_mode, policy.temporary_prefix)
        self._active: object | None = None

    @classmethod
    def open_existing(
        cls, root: Path, instance: str, identity: str, policy: BatchPolicy
    ) -> BatchJournal:
        """Status recovers the pinned plan without opening inputs, credentials or services."""
        if (
            not root.is_absolute()
            or root == root.parent
            or not instance.strip()
            or re.fullmatch(r"[a-f0-9]{64}", identity) is None
        ):
            raise SetupError("invalid batch status root, owner or job identity")
        files = PrivateFiles(policy.directory_mode, policy.file_mode, policy.temporary_prefix)
        folder = root / digest(instance) / identity
        try:
            for directory in (root, folder.parent, folder):
                files.existing_directory(directory)
            state = BatchState.model_validate_json(
                files.read(folder / policy.state_file, policy.max_state_bytes)
            )
        except SetupError:
            raise
        except (OSError, ValueError, TypeError) as exc:
            raise SetupError(f"batch status is absent or invalid ({type(exc).__name__})") from exc
        if state.plan.instance != instance or state.plan.identity != identity:
            raise SetupError("batch status owner or job identity differs")
        journal = cls(root, state.plan, policy, identity=identity)
        journal.inspect()
        return journal

    def _new(self) -> BatchState:
        return BatchState(
            plan=self.plan, stages=tuple(BatchStage(name=name) for name in self.plan.stages)
        )

    def _validate(self, state: BatchState) -> None:
        if state.plan != self.plan:
            raise SetupError("batch journal belongs to a different input, profile or assistant")
        for stage in state.stages:
            if len(stage.attempts) > self.policy.max_attempts:
                raise SetupError("batch attempt history exceeds configured bound")
            for attempt in stage.attempts:
                if len(attempt.metrics) > self.policy.max_metrics or any(
                    len(name) > self.policy.max_metric_characters
                    or re.fullmatch(r"[a-z][a-z0-9_]*", name) is None
                    for name in attempt.metrics
                ):
                    raise SetupError("batch metrics exceed configured names/count bounds")
        canonical(state.model_dump(mode="json"))

    def _load(self) -> BatchState:
        path = self.folder / self.policy.state_file
        try:
            state = (
                BatchState.model_validate_json(self.files.read(path, self.policy.max_state_bytes))
                if path.exists() or path.is_symlink()
                else self._new()
            )
            self._validate(state)
            return state
        except SetupError:
            raise
        except (OSError, ValueError, TypeError) as exc:
            raise SetupError(
                f"batch journal is unreadable or invalid ({type(exc).__name__})"
            ) from exc

    def inspect(self) -> BatchState:
        """No directory, lock, migration, input read or cleanup occurs on a status query."""
        for directory in (self.root, self.folder.parent, self.folder):
            if directory.is_symlink():
                raise SetupError("batch journal path cannot contain symlinks")
            if not directory.exists():
                return self._new()
            self.files.existing_directory(directory)
        return self._load()

    def _change(self, update: Callable[[BatchState], BatchState]) -> None:
        with self.files.locked(self.folder / self.policy.lock_file):
            self.files.cleanup_temporary(self.folder, self.policy.max_temporary_files)
            try:
                changed = update(self._load())
                data = canonical(changed.model_dump(mode="json"))
                validated = BatchState.model_validate_json(data)
                self._validate(validated)
            except SetupError:
                raise
            except (ValueError, TypeError) as exc:
                raise SetupError("invalid batch transition; prior journal retained") from exc
            if len(data) > self.policy.max_state_bytes:
                raise SetupError(
                    "batch journal exceeds configured byte limit; prior state retained"
                )
            self.files.save(self.folder / self.policy.state_file, data)

    def verify_input(self) -> None:
        """Verify immutable prepared input, never a moving native source."""
        if input_digest(self.plan.input_path, self.policy) != self.plan.input_digest:
            raise SetupError("batch prepared input changed; automatic continuation refused")

    def check_worker(self, token: object) -> None:
        """A transition requires the capability held by this journal's current worker lease."""
        if self._active is not token:
            raise SetupError("batch worker lease has ended")

    def transition(self, token: object, update: Callable[[BatchState], BatchState]) -> None:
        """Validate the lease again under the short journal lock before publishing a transition."""
        self.check_worker(token)

        def guarded(state: BatchState) -> BatchState:
            self.check_worker(token)
            return update(state)

        self._change(guarded)

    @contextmanager
    def worker(self) -> Generator[BatchExecution]:
        """OS locks release on death; a durable running stage becomes an interrupted retry."""
        for directory in (self.root, self.folder.parent, self.folder):
            self.files.directory(directory)
        with self.files.locked(self.folder / self.policy.worker_lock_file):
            self._change(lambda state: state)
            token = object()
            self._active = token
            try:
                yield BatchExecution(self, token)
            finally:
                self._active = None


class BatchExecution:
    """Only a currently admitted worker may advance its first unfinished stage."""

    def __init__(self, journal: BatchJournal, token: object) -> None:
        self.journal, self._token = journal, token
        self._stage: str | None = None

    def _admitted(self) -> None:
        self.journal.check_worker(self._token)

    def begin(self) -> str | None:
        """Record start before execution, retaining an orphaned attempt as interrupted."""
        self._admitted()
        if self._stage is not None:
            raise SetupError("current batch stage has not finished")
        self.journal.verify_input()
        selected: str | None = None

        def update(state: BatchState) -> BatchState:
            nonlocal selected
            stages = list(state.stages)
            for index, stage in enumerate(stages):
                if stage.complete:
                    continue
                attempts = list(stage.attempts)
                if len(attempts) >= self.journal.policy.max_attempts:
                    raise SetupError("batch attempt bound reached; explicit recovery required")
                if attempts and attempts[-1].status == "running":
                    attempts[-1] = attempts[-1].model_copy(
                        update={"status": "failed", "category": "interrupted"}
                    )
                attempts.append(BatchAttempt(status="running"))
                stages[index] = stage.model_copy(update={"attempts": tuple(attempts)})
                selected = stage.name
                break
            return state.model_copy(update={"stages": tuple(stages)})

        self.journal.transition(self._token, update)
        self._stage = selected
        return selected

    def succeed(self, stage: str, metrics: Metrics) -> None:
        """Only a verified result and unchanged input let a later stage become eligible."""
        self._admitted()
        self.journal.verify_input()
        try:
            attempt = BatchAttempt(status="success", metrics=metrics)
        except ValidationError:
            raise SetupError("batch result must contain numeric counters only") from None
        self._finish(stage, attempt)

    def fail(self, stage: str, category: FailureCategory, metrics: Metrics | None = None) -> None:
        """Preserve numeric partial progress, never raw model/error/credential text."""
        self._admitted()
        try:
            attempt = BatchAttempt(status="failed", category=category, metrics=metrics or {})
        except ValidationError:
            raise SetupError("invalid batch failure category or numeric counters") from None
        self._finish(stage, attempt)

    def _finish(self, stage: str, attempt: BatchAttempt) -> None:
        self._admitted()
        if self._stage != stage:
            raise SetupError("batch result does not match this worker's running stage")

        def update(state: BatchState) -> BatchState:
            stages = list(state.stages)
            for index, current in enumerate(stages):
                if current.name == stage:
                    if not current.attempts or current.attempts[-1].status != "running":
                        raise SetupError("batch stage is no longer running")
                    stages[index] = current.model_copy(
                        update={"attempts": (*current.attempts[:-1], attempt)}
                    )
                    return state.model_copy(update={"stages": tuple(stages)})
            raise SetupError("batch result names an unknown stage")

        self.journal.transition(self._token, update)
        self._stage = None
