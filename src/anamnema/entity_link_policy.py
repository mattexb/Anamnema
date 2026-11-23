"""Neutralized automatic entity-linking semantics, separate from any graph mutation."""

from importlib.resources import files
from pathlib import Path
from typing import Self

from pydantic import Field, JsonValue, ValidationError, model_validator

from anamnema.archive_models import canonical
from anamnema.notes_policy import has_model_credentials
from anamnema.setup_config import Nonempty, SetupError, StrictModel


class EntityLinkPolicy(StrictModel):
    batch: int = Field(gt=0)
    max_input_bytes: int = Field(gt=0)
    max_roster_entities: int = Field(gt=0)
    max_name_characters: int = Field(gt=0)
    answer_tokens: int = Field(gt=0)
    unlinked_kinds: tuple[Nonempty, ...]
    proposal_types: tuple[Nonempty, ...]
    version: Nonempty
    instructions: Nonempty
    model_options: dict[str, JsonValue]

    @model_validator(mode="after")
    def controlled(self) -> Self:
        canonical(self.model_options)
        if has_model_credentials(self.model_options) or set(self.model_options) & {
            "model",
            "messages",
            "stream",
            "response_format",
            "max_tokens",
        }:
            raise ValueError(
                "entity-link options cannot replace controlled fields or contain credentials"
            )
        if not self.proposal_types or len(set(self.proposal_types)) != len(self.proposal_types):
            raise ValueError("entity proposal types must be distinct and nonempty")
        return self


def entity_link_policy(path: Path | None = None) -> EntityLinkPolicy:
    try:
        raw = (
            path.read_text(encoding="utf-8")
            if path
            else files("anamnema")
            .joinpath("config/entity_linking.json")
            .read_text(encoding="utf-8")
        )
        return EntityLinkPolicy.model_validate_json(raw)
    except (OSError, UnicodeError, ValidationError) as exc:
        raise SetupError("entity-link policy cannot be read or validated") from exc
