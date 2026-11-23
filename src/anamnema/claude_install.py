"""Register, update and remove Anamnema's Claude hooks in one explicitly named settings file.

The receipt records exactly which matcher groups the installer owns, so a later apply or
remove touches those and nothing else. Its intent is journalled before the settings file
changes: an interruption between the two is resolved on the next run by finding which of the
two recorded sets the settings file actually holds.
"""

import json
import os
import stat
import tempfile
from importlib.resources import files
from pathlib import Path
from typing import Literal, Self, cast

from pydantic import Field, JsonValue, ValidationError, model_validator

from anamnema.hook_merge import HookBinding, merge_hooks
from anamnema.private_files import PrivateFiles
from anamnema.setup_config import SetupError, StrictModel


class ClaudeHookPolicy(StrictModel):
    start_timeout_seconds: float = Field(gt=0, allow_inf_nan=False)
    stop_timeout_seconds: float = Field(gt=0, allow_inf_nan=False)
    precompact_margin_seconds: float = Field(gt=0, allow_inf_nan=False)
    max_settings_bytes: int = Field(gt=0)
    max_receipt_bytes: int = Field(gt=0)
    settings_file_mode: int = Field(ge=0o400, le=0o644)


class ContinuityHooks(StrictModel):
    """The installed command and the deployment files every continuity hook passes."""

    executable: Path
    continuity_config: Path
    worker_config: Path
    producer_timeout_seconds: float = Field(gt=0, allow_inf_nan=False)

    @model_validator(mode="after")
    def explicit_paths(self) -> Self:
        paths = (self.executable, self.continuity_config, self.worker_config)
        if any(not path.is_absolute() for path in paths):
            raise ValueError("continuity hook paths must be absolute")
        return self


class HookReceipt(StrictModel):
    version: Literal[1]
    settings: str
    phase: Literal["writing", "applied"]
    previous: tuple[HookBinding, ...]
    desired: tuple[HookBinding, ...]


def claude_hook_policy(path: Path | None = None) -> ClaudeHookPolicy:
    try:
        text = (
            path.read_text(encoding="utf-8")
            if path
            else files("anamnema").joinpath("config/claude_hooks.json").read_text(encoding="utf-8")
        )
        return ClaudeHookPolicy.model_validate_json(text)
    except (OSError, UnicodeError, ValidationError) as exc:
        raise SetupError(f"invalid Claude hook policy ({type(exc).__name__})") from exc


def continuity_bindings(
    hooks: ContinuityHooks, policy: ClaudeHookPolicy
) -> tuple[HookBinding, ...]:
    """SessionStart, Stop and PreCompact; PreCompact outlives the snapshot worker's deadline."""
    common = ("continuity",)
    config = ("--continuity-config", str(hooks.continuity_config))
    worker = ("--worker-config", str(hooks.worker_config))
    exe = str(hooks.executable)
    return (
        HookBinding(
            event="SessionStart",
            command=(exe, *common, "start", *config),
            timeout_seconds=policy.start_timeout_seconds,
        ),
        HookBinding(
            event="Stop",
            command=(exe, *common, "stop", *config),
            timeout_seconds=policy.stop_timeout_seconds,
        ),
        HookBinding(
            event="PreCompact",
            command=(exe, *common, "precompact", *config, *worker),
            timeout_seconds=hooks.producer_timeout_seconds + policy.precompact_margin_seconds,
        ),
    )


class ClaudeHookInstaller:
    """Owns only the groups in its receipt; foreign hooks and every other setting stay as found."""

    def __init__(self, settings: Path, state_directory: Path, policy: ClaudeHookPolicy) -> None:
        if not settings.is_absolute() or not state_directory.is_absolute():
            raise SetupError("Claude settings and hook state paths must be absolute")
        self.settings, self.policy = settings, policy
        self.state = state_directory
        self.files = PrivateFiles(0o700, 0o600, ".claude-hooks-")
        self.receipt = state_directory / "claude_hooks.json"

    def plan(self, desired: tuple[HookBinding, ...]) -> dict[str, JsonValue]:
        """Read-only: what apply would change, refusing exactly where apply would refuse."""
        document, _raw = self._read_settings()
        merged = merge_hooks(document, self._owned(document), desired)
        events: list[JsonValue] = [*sorted({str(b.event) for b in desired})]
        return {"changed": merged.changed, "events": events}

    def apply(self, desired: tuple[HookBinding, ...]) -> dict[str, JsonValue]:
        self.files.directory(self.state)
        with self.files.locked(self.state / ".claude-hooks.lock"):
            document, raw = self._read_settings()
            owned = self._owned(document)
            merged = merge_hooks(document, owned, desired)
            if merged.changed:
                self._journal("writing", owned, desired)
                self._write_settings(merged.settings, raw)
            self._journal("applied", desired, desired)
            return {"changed": merged.changed, "hooks": len(desired)}

    def remove(self) -> dict[str, JsonValue]:
        """Take out exactly the owned groups, then the receipt; a second remove is a no-op."""
        if not self.receipt.exists():
            return {"removed": 0}
        with self.files.locked(self.state / ".claude-hooks.lock"):
            document, raw = self._read_settings()
            owned = self._owned(document)
            merged = merge_hooks(document, owned, ())
            if merged.changed:
                self._journal("writing", owned, ())
                self._write_settings(merged.settings, raw)
            self.receipt.unlink()
            self._sync(self.state)
            return {"removed": len(owned)}

    def _owned(self, document: dict[str, JsonValue]) -> tuple[HookBinding, ...]:
        """The set the settings file holds now; an interrupted write resolves to one side."""
        receipt = self._load()
        if receipt is None:
            return ()
        if receipt.phase == "applied":
            return receipt.desired
        for candidate in (receipt.desired, receipt.previous):
            try:
                merge_hooks(document, candidate, ())
            except SetupError:
                continue
            return candidate
        raise SetupError("Claude settings hold neither recorded hook set; repair by hand")

    def _load(self) -> HookReceipt | None:
        try:
            raw = self.files.read(self.receipt, self.policy.max_receipt_bytes)
        except FileNotFoundError:
            return None
        try:
            receipt = HookReceipt.model_validate_json(raw)
        except ValidationError as exc:
            raise SetupError("Claude hook receipt is invalid") from exc
        if receipt.settings != str(self.settings):
            raise SetupError("Claude hook receipt belongs to another settings file")
        return receipt

    def _journal(
        self,
        phase: Literal["writing", "applied"],
        previous: tuple[HookBinding, ...],
        desired: tuple[HookBinding, ...],
    ) -> None:
        receipt = HookReceipt(
            version=1, settings=str(self.settings), phase=phase, previous=previous, desired=desired
        )
        data = receipt.model_dump_json().encode("utf-8")
        if len(data) > self.policy.max_receipt_bytes:
            raise SetupError("Claude hook receipt exceeds configured bound")
        self.files.save(self.receipt, data)

    def _read_settings(self) -> tuple[dict[str, JsonValue], bytes | None]:
        """A missing file is empty settings; a link, foreign or oversized file is refused."""
        if any(p.is_symlink() for p in (self.settings, *self.settings.parents)):
            raise SetupError("Claude settings path cannot contain symlinks")
        try:
            descriptor = os.open(self.settings, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        except FileNotFoundError:
            return {}, None
        except NotADirectoryError as exc:
            raise SetupError("Claude settings directory is not a directory") from exc
        with os.fdopen(descriptor, "rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
                raise SetupError("Claude settings is not a regular file owned by this user")
            raw = stream.read(self.policy.max_settings_bytes + 1)
        if len(raw) > self.policy.max_settings_bytes:
            raise SetupError("Claude settings exceeds configured bound")
        try:
            document: object = json.loads(raw.decode("utf-8")) if raw.strip() else {}
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise SetupError("Claude settings is not valid JSON; no change made") from exc
        if not isinstance(document, dict):
            raise SetupError("Claude settings is not a JSON object")
        return cast(dict[str, JsonValue], document), raw

    def _settings_directory(self) -> None:
        """Claude Code makes ~/.claude on its first run, so a fresh install before any login has
        none (VM1015, 2026-11-23). Create that one private level only; an existing directory is
        never touched, and a missing grandparent (a wrong path) is still refused."""
        parent = self.settings.parent
        if parent.is_dir():
            return
        if not parent.parent.is_dir():
            raise SetupError("Claude settings location does not exist")
        try:
            os.mkdir(parent, 0o700)
        except FileExistsError as exc:
            raise SetupError("Claude settings directory is not a directory") from exc
        self._sync(parent.parent)

    def _write_settings(self, document: dict[str, JsonValue], expected: bytes | None) -> None:
        """Atomic replace keeping the file's mode; refuse if someone wrote it since the read."""
        self._settings_directory()
        _current, now = self._read_settings()
        if now != expected:
            raise SetupError("Claude settings changed during install; nothing written, retry")
        mode = (
            stat.S_IMODE(self.settings.stat().st_mode)
            if expected is not None
            else self.policy.settings_file_mode
        )
        content = (json.dumps(document, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
        handle, temporary = tempfile.mkstemp(dir=self.settings.parent, prefix=".anamnema-")
        try:
            with os.fdopen(handle, "wb") as stream:
                os.fchmod(stream.fileno(), mode)
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.settings)
            self._sync(self.settings.parent)
        finally:
            Path(temporary).unlink(missing_ok=True)

    @staticmethod
    def _sync(directory: Path) -> None:
        descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
