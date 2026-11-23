"""Card rating and writer bounds, without private people, projects or runner commands."""

from importlib.resources import files
from pathlib import Path
from typing import Annotated, Self

from pydantic import Field, JsonValue, ValidationError, model_validator

from anamnema.archive_models import canonical
from anamnema.memory_config import Positive, Unit
from anamnema.setup_config import Nonempty, SetupError, StrictModel


class CardPolicy(StrictModel):
    """Every generation, pagination and priority knob is explicit in the public policy."""

    statements: Annotated[int, Field(gt=0)]
    notes: Annotated[int, Field(gt=0)]
    front: Annotated[int, Field(gt=0)]
    page_size: Annotated[int, Field(gt=0)]
    who: Annotated[int, Field(ge=0)]
    age_half_days: Positive
    age_floor: Unit
    open_weight: Unit
    open_cap: Annotated[int, Field(ge=0)]
    newest_share: Unit
    expanded_entities: tuple[Nonempty, ...]
    expanded_factor: Annotated[int, Field(ge=1)]
    instructions: Nonempty
    answer_tokens: Annotated[int, Field(gt=0)]
    model_options: dict[str, JsonValue]

    @model_validator(mode="after")
    def coherent_limits(self) -> Self:
        """Identity lines cannot exceed page one; model options cannot replace the contract."""
        if self.who > self.front or self.front > self.statements:
            raise ValueError("card identity/front/statement limits disagree")
        reserved = {"model", "messages", "response_format", "max_tokens", "stream"}
        if set(self.model_options) & reserved:
            raise ValueError("card options contain reserved request keys")
        canonical(self.model_options)
        return self

    def factor(self, name: str) -> int:
        """Extra capacity is an operator choice, not a hardcoded identity privilege."""
        return self.expanded_factor if name in self.expanded_entities else 1


def card_policy(path: Path | None = None) -> CardPolicy:
    """Load complete nonsecret card policy without making a model call."""
    try:
        raw = (
            path.read_text(encoding="utf-8")
            if path
            else files("anamnema").joinpath("config/cards.json").read_text(encoding="utf-8")
        )
        return CardPolicy.model_validate_json(raw)
    except (OSError, UnicodeError, ValidationError) as exc:
        raise SetupError("card policy cannot be read or validated") from exc
