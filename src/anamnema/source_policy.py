"""Captured-order window references and citation-centred display policy."""

from importlib.resources import files
from pathlib import Path
from typing import Annotated, Literal

from pydantic import Field, ValidationError

from anamnema.setup_config import SetupError, StrictModel

Positive = Annotated[int, Field(gt=0)]


class SourcePolicy(StrictModel):
    """Own immutable reference schema and configurable source-display limits."""

    schema_version: Literal[1]
    windows_collection: Annotated[str, Field(pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")]
    max_window_records: Positive
    max_manifest_bytes: Positive
    context_records: Positive
    source_turns: Annotated[int, Field(ge=0)]
    source_tokens: Positive
    characters_per_token: Positive


def source_policy(path: Path | None = None) -> SourcePolicy:
    """Load a complete explicit policy or its neutral packaged defaults."""
    try:
        text = (
            path.read_text(encoding="utf-8")
            if path
            else files("anamnema").joinpath("config/source.json").read_text(encoding="utf-8")
        )
        return SourcePolicy.model_validate_json(text)
    except (OSError, UnicodeError, ValidationError) as exc:
        raise SetupError(f"invalid source policy ({type(exc).__name__})") from exc
