"""Explicit public repeat-judge policy, separate from extraction model settings."""

from importlib.resources import files
from pathlib import Path
from typing import Annotated, Self

from pydantic import Field, JsonValue, ValidationError, model_validator

from anamnema.archive_models import canonical
from anamnema.setup_config import Nonempty, SetupError, StrictModel


class MergePolicy(StrictModel):
    """Bound candidate work and structured judgments; no private model command."""

    enabled: bool
    similarity: Annotated[float, Field(ge=0, le=1, allow_inf_nan=False)]
    candidates: Annotated[int, Field(gt=0)]
    batch_pairs: Annotated[int, Field(gt=0)]
    note_characters: Annotated[int, Field(gt=0)]
    answer_tokens: Annotated[int, Field(gt=0)]
    instructions: Nonempty
    model_options: dict[str, JsonValue]

    @model_validator(mode="after")
    def reserved_options(self) -> Self:
        """Call identity, schema and budget cannot be overwritten by model options."""
        if set(self.model_options) & {
            "model",
            "messages",
            "response_format",
            "max_tokens",
            "stream",
        }:
            raise ValueError("merge model options contain reserved request keys")
        canonical(self.model_options)
        return self


def merge_policy(path: Path | None = None) -> MergePolicy:
    """Read complete project policy without opening a model, keyring or database."""
    try:
        raw = (
            path.read_text(encoding="utf-8")
            if path
            else files("anamnema").joinpath("config/merge.json").read_text(encoding="utf-8")
        )
        return MergePolicy.model_validate_json(raw)
    except (OSError, UnicodeError, ValidationError) as exc:
        raise SetupError("merge configuration cannot be read or validated") from exc
