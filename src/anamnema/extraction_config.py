"""Bounded local extraction execution and result-collection settings."""

import re
from importlib.resources import files
from pathlib import Path
from typing import Self

from pydantic import Field, ValidationError, field_validator, model_validator

from anamnema.setup_config import Nonempty, SetupError, StrictModel


class ExtractionConfig(StrictModel):
    """Deployment values separate from result-shaping model/prompt policy."""

    schema_version: int = Field(ge=1, le=1)
    timeout_seconds: float = Field(gt=0, allow_inf_nan=False)
    slow_chunk_seconds: float = Field(gt=0, allow_inf_nan=False)
    pause_every: int = Field(ge=0)
    pause_seconds: float = Field(ge=0, allow_inf_nan=False)
    max_response_bytes: int = Field(gt=0)
    max_request_bytes: int = Field(gt=0)
    max_model_bytes: int = Field(gt=0)
    hash_block_bytes: int = Field(gt=0)
    chunks_collection: Nonempty
    attempts_collection: Nonempty
    props_path: Nonempty
    chat_path: Nonempty
    template_path: Nonempty
    tokenize_path: Nonempty
    model_keyring_entry: Nonempty | None
    model_keyring_field: Nonempty | None

    @field_validator("chunks_collection", "attempts_collection")
    @classmethod
    def collection_identifier(cls, value: str) -> str:
        """Public result collections use simple identifiers, never command/system names."""
        if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", value) is None:
            raise ValueError("invalid result collection identifier")
        return value

    @field_validator("props_path", "chat_path", "template_path", "tokenize_path")
    @classmethod
    def local_route(cls, value: str) -> str:
        """Routes cannot replace the validated local connection target."""
        if not value.startswith("/") or value.startswith("//") or "\r" in value or "\n" in value:
            raise ValueError("invalid local model route")
        return value

    @model_validator(mode="after")
    def separate_collections(self) -> Self:
        """Execution state and immutable successful results have distinct owners."""
        if self.chunks_collection == self.attempts_collection:
            raise ValueError("extraction collections must be distinct")
        if (self.model_keyring_entry is None) != (self.model_keyring_field is None):
            raise ValueError("model credential entry and field must be selected together")
        return self


def extraction_config(path: Path | None = None) -> ExtractionConfig:
    """Read complete public settings, never a private model runner configuration."""
    try:
        raw = (
            path.read_text(encoding="utf-8")
            if path
            else files("anamnema").joinpath("config/extraction.json").read_text(encoding="utf-8")
        )
        return ExtractionConfig.model_validate_json(raw)
    except (OSError, UnicodeError, ValidationError) as exc:
        raise SetupError("extraction configuration cannot be read or validated") from exc
