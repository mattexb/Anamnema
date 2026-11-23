"""Explicit nonsecret local window-batch deployment; no native or private host discovery."""

from pathlib import Path
from typing import Self

from pydantic import ValidationError, model_validator

from anamnema.batch_models import batch_policy
from anamnema.continuity_state import source_text
from anamnema.setup_config import Nonempty, SetupError, StrictModel


class WindowBatchConfig(StrictModel):
    instance: Nonempty
    inputs_root: Path
    jobs_root: Path
    model_file: Path
    setup_config: Path | None = None
    archive_config: Path | None = None
    extraction_config: Path | None = None
    notes_config: Path | None = None
    speech_config: Path | None = None
    memory_config: Path | None = None
    search_config: Path | None = None
    source_config: Path | None = None
    merge_config: Path | None = None
    merge_model_file: Path | None = None
    preparation_policy: Path | None = None
    journal_policy: Path | None = None

    @model_validator(mode="after")
    def explicit_paths(self) -> Self:
        for key in type(self).model_fields:
            value = getattr(self, key)
            if isinstance(value, Path) and (not value.is_absolute() or value == value.parent):
                raise ValueError("batch paths must be explicit absolute non-root paths")
        if self.inputs_root == self.jobs_root:
            raise ValueError("batch inputs and journals must have separate roots")
        return self


def window_batch_config(path: Path) -> WindowBatchConfig:
    try:
        return WindowBatchConfig.model_validate_json(
            source_text(path, batch_policy().max_state_bytes)
        )
    except (OSError, UnicodeError, ValidationError) as exc:
        raise SetupError("window batch deployment cannot be read or validated") from exc
