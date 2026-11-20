"""Validated public Claude/spoken-row policy, independent of private configuration."""

import re
from importlib.resources import files
from pathlib import Path
from typing import Self

from pydantic import Field, ValidationError, model_validator

from anamnema.setup_config import Nonempty, SetupError, StrictModel


class ClaudeSpeech(StrictModel):
    """Native Claude schema and known injected-message grammar."""

    type_key: Nonempty
    message_key: Nonempty
    content_key: Nonempty
    sidechain_key: Nonempty
    meta_key: Nonempty
    assistant_type: Nonempty
    harness_scan_characters: int = Field(gt=0)
    text_block_type: Nonempty
    user_types: tuple[str, ...]
    artifact_marker_pattern: str
    peer_prefix: str
    injected_prefixes: tuple[str, ...]
    tool_placeholders: tuple[str, ...]


class SpeechPolicy(StrictModel):
    """Only configurable input filtering; no infrastructure or model connection."""

    schema_version: int = Field(ge=1, le=1)
    claude: ClaudeSpeech
    owner_noise_prefixes: tuple[str, ...]
    owner_envelopes: tuple[str, ...]
    owner_room_turn: str
    owner_room_speaker: str
    agent_envelopes: tuple[str, ...]
    marked_lines: tuple[Nonempty, ...]
    marked_pattern: str
    fence_pattern: str
    quote_pattern: str
    secret_word_pattern: str
    secret_symbol_pattern: str
    secret_minimum_characters: int = Field(gt=0)
    secret_mask: Nonempty

    @model_validator(mode="after")
    def regex_grammar(self) -> Self:
        """Reject invalid or incompatible configured patterns before reading content."""
        patterns = [
            *self.owner_envelopes,
            *self.agent_envelopes,
            self.owner_room_turn,
            self.claude.artifact_marker_pattern,
            self.marked_pattern,
            self.fence_pattern,
            self.quote_pattern,
            self.secret_word_pattern,
            self.secret_symbol_pattern,
        ]
        try:
            for pattern in patterns:
                re.compile(pattern)
            if "who" not in re.compile(self.owner_room_turn).groupindex:
                raise ValueError("room pattern requires speaker group")
            if "marker" not in re.compile(self.marked_pattern).groupindex:
                raise ValueError("marked pattern requires marker group")
        except re.error as exc:
            raise ValueError("invalid filtering pattern") from exc
        return self


def speech_policy(path: Path | None = None) -> SpeechPolicy:
    """Read a complete public policy without private config or secret access."""
    try:
        raw = (
            path.read_text(encoding="utf-8")
            if path is not None
            else files("anamnema").joinpath("config/speech.json").read_text(encoding="utf-8")
        )
        return SpeechPolicy.model_validate_json(raw)
    except (OSError, UnicodeError, ValidationError) as exc:
        raise SetupError("speech configuration cannot be read or validated") from exc
