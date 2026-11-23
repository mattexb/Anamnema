"""Public policy for source-backed subject labels, without private commands or names."""

from importlib.resources import files
from pathlib import Path
from typing import Annotated, Self

from pydantic import Field, JsonValue, ValidationError, model_validator

from anamnema.archive_models import canonical
from anamnema.notes_policy import has_model_credentials
from anamnema.setup_config import Nonempty, SetupError, StrictModel


class NamingPolicy(StrictModel):
    """Input, label and model bounds are independent of deployment addresses."""

    batch: Annotated[int, Field(gt=0)]
    source_rows: Annotated[int, Field(gt=0)]
    row_characters: Annotated[int, Field(gt=0)]
    subject_characters: Annotated[int, Field(gt=0)]
    subject_margin: Annotated[float, Field(ge=0, le=1, allow_inf_nan=False)]
    version: Nonempty
    answer_tokens: Annotated[int, Field(gt=0)]
    instructions: Nonempty
    model_options: dict[str, JsonValue]

    @model_validator(mode="after")
    def controlled_request(self) -> Self:
        """Model credentials and controlled schema/budget fields cannot enter request options."""
        canonical(self.model_options)
        if has_model_credentials(self.model_options):
            raise ValueError("naming model credentials belong in the project keyring")
        if set(self.model_options) & {
            "model",
            "messages",
            "stream",
            "response_format",
            "max_tokens",
        }:
            raise ValueError("naming options cannot replace controlled request fields")
        return self


def naming_policy(path: Path | None = None) -> NamingPolicy:
    """Read complete nonsecret policy without opening a model or store."""
    try:
        raw = (
            path.read_text(encoding="utf-8")
            if path
            else files("anamnema").joinpath("config/naming.json").read_text(encoding="utf-8")
        )
        return NamingPolicy.model_validate_json(raw)
    except (OSError, UnicodeError, ValidationError) as exc:
        raise SetupError("naming configuration cannot be read or validated") from exc
