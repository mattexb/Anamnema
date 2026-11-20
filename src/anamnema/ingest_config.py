"""Reviewed archive/parser configuration, independent of private settings."""

from importlib.resources import files
from pathlib import Path
from typing import Literal, Self

from pydantic import Field, ValidationError, model_validator

from anamnema.setup_config import Nonempty, SetupError, StrictModel


class NativeSchema(StrictModel):
    """External native field names and preservation policy stay configurable."""

    record_type: Nonempty
    timestamp: Nonempty
    native_id: Nonempty
    summary_flag: Nonempty
    conversation_types: tuple[str, ...]
    summary_types: tuple[str, ...]
    encoding: Literal["utf-8"]


class ArchiveSettings(StrictModel):
    """Bounded reads/writes and a project-owned keyring selector."""

    schema_version: Literal[1]
    substrate: Literal["claude"]
    database: Nonempty
    collections: dict[Literal["conversation", "summary", "closing", "receipt"], str]
    batch_size: int = Field(gt=0)
    max_file_bytes: int = Field(gt=0)
    max_document_bytes: int = Field(gt=0)
    compression_preset: int = Field(ge=0, le=9)
    timeout_ms: int = Field(gt=0)
    keyring_entry: Nonempty
    keyring_field: Nonempty
    keyring_timeout_seconds: float = Field(gt=0)
    bus_env: Nonempty
    bus_fallback: Nonempty
    reader: NativeSchema

    @model_validator(mode="after")
    def complete_collections(self) -> Self:
        """Require distinct declared destinations for every storage kind."""
        expected = {"conversation", "summary", "closing", "receipt"}
        names = list(self.collections.values())
        if set(self.collections) != expected or any(not name.strip() for name in names):
            raise ValueError("all archive collections must be named")
        if len(set(names)) != len(names):
            raise ValueError("archive collection names must be distinct")
        return self


def archive_settings(path: Path | None = None) -> ArchiveSettings:
    """Load packaged policy only; no private configuration or secret access."""
    try:
        return ArchiveSettings.model_validate_json(
            path.read_text(encoding="utf-8")
            if path is not None
            else files("anamnema").joinpath("config/archive.json").read_text(encoding="utf-8")
        )
    except (OSError, ValidationError) as exc:
        raise SetupError("archive configuration cannot be read or validated") from exc
