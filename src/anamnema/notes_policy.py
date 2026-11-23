"""Configured chunking, decision binding and extraction-request contract."""

import re
from importlib.resources import files
from pathlib import Path
from typing import Self

from pydantic import Field, JsonValue, ValidationError, model_validator

from anamnema.archive_models import canonical
from anamnema.setup_config import Nonempty, SetupError, StrictModel


class Names(StrictModel):
    """Public speaker labels, filled by an operator rather than private defaults."""

    owner: Nonempty
    agent: Nonempty
    sent: Nonempty


class NotesPolicy(StrictModel):
    """All tunable chunk/schema/request settings are packaged, fingerprintable data."""

    schema_version: int = Field(ge=1, le=1)
    names: Names
    chunk_characters: int = Field(gt=0)
    chunk_tokens: int = Field(gt=0)
    letters_per_token: int = Field(gt=0)
    letter_pattern: str
    space_pattern: str
    numbered_pattern: str
    disagree_pattern: str
    agree_pattern: str
    numbers_pattern: str
    minimum_items: int = Field(gt=0)
    max_notes: int = Field(gt=0)
    note_characters: int = Field(gt=0)
    minimum_note_characters: int = Field(gt=0)
    max_rows_per_note: int = Field(gt=0)
    nearest_rows: int = Field(gt=0)
    answer_characters_per_token: int = Field(gt=0)
    note_overhead_tokens: int = Field(ge=0)
    answer_overhead_tokens: int = Field(ge=0)
    model_options: dict[str, JsonValue]
    note_word_pattern: str
    owner_decision_pattern: str

    @model_validator(mode="after")
    def grammar(self) -> Self:
        """Validate external grammar and refuse request fields that could replace prompts."""
        try:
            for pattern in (
                self.letter_pattern,
                self.space_pattern,
                self.numbered_pattern,
                self.disagree_pattern,
                self.agree_pattern,
                self.numbers_pattern,
                self.note_word_pattern,
                self.owner_decision_pattern.replace("{owner}", "Owner"),
            ):
                re.compile(pattern)
        except re.error as exc:
            raise ValueError("invalid chunk/binding pattern") from exc
        if self.minimum_note_characters > self.note_characters:
            raise ValueError("note bounds conflict")
        for pattern in (self.numbered_pattern, self.disagree_pattern, self.agree_pattern):
            if re.compile(pattern).groups != 1:
                raise ValueError("binding patterns require one captured group")
        canonical(self.model_options)
        if has_model_credentials(self.model_options):
            raise ValueError("model credentials belong in the project keyring, not request options")
        if {
            "model",
            "messages",
            "max_tokens",
            "response_format",
            "stream",
        } & self.model_options.keys():
            raise ValueError("model options cannot replace controlled request fields")
        return self


def has_model_credentials(value: JsonValue) -> bool:
    """Shared recursive refusal of credential-bearing model-option keys."""
    if isinstance(value, dict):
        forbidden = {
            "api_key",
            "apikey",
            "password",
            "authorization",
            "token",
            "secret",
            "headers",
            "extra_headers",
        }
        return any(
            key.lower().replace("-", "_") in forbidden or has_model_credentials(item)
            for key, item in value.items()
        )
    if isinstance(value, list):
        return any(has_model_credentials(item) for item in value)
    return False


def notes_policy(path: Path | None = None) -> NotesPolicy:
    """Load complete public settings without source-project configuration."""
    try:
        raw = (
            path.read_text(encoding="utf-8")
            if path is not None
            else files("anamnema").joinpath("config/notes.json").read_text(encoding="utf-8")
        )
        return NotesPolicy.model_validate_json(raw)
    except (OSError, UnicodeError, ValidationError) as exc:
        raise SetupError("notes configuration cannot be read or validated") from exc
