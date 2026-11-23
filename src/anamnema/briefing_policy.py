"""Public briefing bounds and neutralized source instructions, separate from deployment."""

import math
from importlib.resources import files
from pathlib import Path
from typing import Self

from pydantic import Field, ValidationError, model_validator

from anamnema.setup_config import Nonempty, SetupError, StrictModel


class BriefingPolicy(StrictModel):
    """Read/page/stack budgets remain estimates and cannot silently drop input evidence."""

    batch_bytes: int = Field(gt=0)
    read_bytes: int = Field(gt=0)
    parallel_reads: int = Field(gt=0)
    source_rows: int = Field(gt=0)
    row_characters: int = Field(gt=0)
    summary_tokens: int = Field(gt=0)
    bytes_per_token: float = Field(gt=0, allow_inf_nan=False)
    size_margin: float = Field(ge=0, le=1, allow_inf_nan=False)
    summary_tolerance: float = Field(ge=0, le=1, allow_inf_nan=False)
    broad_share: float = Field(gt=0, le=1, allow_inf_nan=False)
    passed_over_share: float = Field(ge=0, le=1, allow_inf_nan=False)
    stack_soft_share: float = Field(gt=0, le=1, allow_inf_nan=False)
    stack_hard_share: float = Field(gt=0, le=1, allow_inf_nan=False)
    context_tokens: int = Field(gt=0)
    instructions: Nonempty
    combine_instructions: Nonempty
    condense_instructions: Nonempty

    @model_validator(mode="after")
    def ordered_limits(self) -> Self:
        """A batch fits the build budget and the soft stack limit cannot exceed the hard one."""
        if self.batch_bytes > self.read_bytes or self.stack_soft_share > self.stack_hard_share:
            raise ValueError("briefing read or stack limits disagree")
        return self

    def estimate(self, text: str) -> int:
        """Estimate UTF-8 size with the source density/margin, not an actual tokenizer count."""
        return math.ceil(len(text.encode("utf-8")) / self.bytes_per_token * (1 + self.size_margin))

    @property
    def ceiling(self) -> int:
        """The source accepts a configured tolerance above its target page size."""
        return math.floor(self.summary_tokens * (1 + self.summary_tolerance))


def briefing_policy(path: Path | None = None) -> BriefingPolicy:
    """Load the complete public policy without resolving private commands or native settings."""
    try:
        raw = (
            path.read_text(encoding="utf-8")
            if path
            else files("anamnema").joinpath("config/briefing.json").read_text(encoding="utf-8")
        )
        return BriefingPolicy.model_validate_json(raw)
    except (OSError, UnicodeError, ValidationError) as exc:
        raise SetupError("briefing policy cannot be read or validated") from exc
