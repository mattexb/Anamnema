"""Explicit deployment paths and complete nonsecret lifecycle policy."""

import re
from importlib.resources import files
from pathlib import Path
from typing import Literal, Self

from pydantic import Field, ValidationError, model_validator

from anamnema.setup_config import Nonempty, SetupError, StrictModel
from anamnema.worker_registry import RegistryConfig


class ContinuityConfig(StrictModel):
    """Native input never chooses its assistant or state root."""

    instance: Nonempty
    state_root: Path
    identity_files: tuple[Path, ...]
    registry: RegistryConfig | None = None

    @model_validator(mode="after")
    def explicit_paths(self) -> Self:
        """Refuse implicit cwd/home resolution and empty identity installations."""
        if not self.state_root.is_absolute() or self.state_root == self.state_root.parent:
            raise ValueError("continuity state root must be an explicit absolute directory")
        if not self.identity_files or any(not p.is_absolute() for p in self.identity_files):
            raise ValueError("identity_files must name explicit absolute files")
        if len(set(self.identity_files)) != len(self.identity_files):
            raise ValueError("identity files must be distinct")
        if not self.instance.strip() or any(c in self.instance for c in ("\r", "\n")):
            raise ValueError("continuity instance must be a nonempty single line")
        return self


class ContinuityPolicy(StrictModel):
    """External event names, bounds, permissions and acknowledgement markers."""

    max_input_bytes: int = Field(gt=0)
    max_content_bytes: int = Field(gt=0)
    max_state_bytes: int = Field(gt=0)
    max_pending: int = Field(gt=0)
    max_read_key_characters: int = Field(gt=0)
    max_spool_items: int = Field(gt=0)
    token_bytes: int = Field(ge=16)
    directory_mode: int = Field(ge=0, le=0o777)
    file_mode: int = Field(ge=0, le=0o777)
    state_file: Nonempty
    lock_file: Nonempty
    spool_directory: Nonempty
    blob_suffix: Nonempty
    temporary_prefix: Nonempty
    session_pattern: Nonempty
    read_marker: Nonempty
    snapshot_marker: Nonempty
    snapshot_ack_marker: Nonempty
    identity_sources: tuple[Nonempty, ...]
    resume_source: Nonempty
    compact_source: Nonempty
    native_events: dict[Literal["start", "stop"], Nonempty]
    native_fields: dict[Literal["session", "event", "source", "message", "active"], Nonempty]

    @model_validator(mode="after")
    def safe_policy(self) -> Self:
        """Managed paths cannot escape state, and modes cannot expose identity content."""
        if self.directory_mode != 0o700 or self.file_mode != 0o600:
            raise ValueError("continuity state requires owner-only directory/file modes")
        if re.fullmatch(r"\.[A-Za-z0-9_-]+", self.temporary_prefix) is None:
            raise ValueError("continuity temporary prefix must be a reserved dot-prefixed name")
        for name in (
            self.state_file,
            self.lock_file,
            self.spool_directory,
            self.blob_suffix,
            self.temporary_prefix,
        ):
            if "/" in name or "\\" in name or name in (".", ".."):
                raise ValueError("continuity filenames must be local basenames")
        if len({self.state_file, self.lock_file, self.spool_directory}) != 3 or any(
            name.startswith(self.temporary_prefix)
            for name in (self.state_file, self.lock_file, self.spool_directory)
        ):
            raise ValueError("continuity state and lock names must be distinct")
        if set(self.native_events) != {"start", "stop"} or set(self.native_fields) != {
            "session",
            "event",
            "source",
            "message",
            "active",
        }:
            raise ValueError("native continuity protocol fields are incomplete")
        re.compile(self.session_pattern)
        return self


def continuity_policy(path: Path | None = None) -> ContinuityPolicy:
    """Load packaged policy without reading any native/private installation settings."""
    try:
        raw = (
            path.read_text(encoding="utf-8")
            if path
            else files("anamnema").joinpath("config/continuity.json").read_text(encoding="utf-8")
        )
        return ContinuityPolicy.model_validate_json(raw)
    except (OSError, ValidationError, re.error) as exc:
        raise SetupError(f"invalid continuity policy ({type(exc).__name__})") from exc


def continuity_config(path: Path) -> ContinuityConfig:
    """Load only the explicit deployment document; paths and identities have no defaults."""
    try:
        return ContinuityConfig.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, ValidationError) as exc:
        raise SetupError(f"invalid continuity configuration ({type(exc).__name__})") from exc
