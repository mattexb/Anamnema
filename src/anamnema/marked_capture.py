"""Source-derived manual markers, separate from model extraction and private incident stores."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from importlib.resources import files
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import Field, ValidationError

from anamnema.archive_models import Record, Snapshot, digest
from anamnema.setup_config import Nonempty, SetupError, StrictModel
from anamnema.speech import Row, marked_matches, scrub
from anamnema.speech_policy import SpeechPolicy
from anamnema.substrates.claude_speech import ClaudeSpeechReader


class TurnPromptPolicy(StrictModel):
    """Native tool-result blocks do not start owner turns, even alongside text."""

    block_type_field: Nonempty
    tool_result_type: Nonempty


class CapturePolicy(StrictModel):
    """The marker catalogue must agree with extraction's exclusions before any writes."""

    schema_version: Literal[1]
    session_field: Nonempty
    turn_prompt: TurnPromptPolicy
    kinds: dict[Nonempty, Nonempty]
    failure_kinds: tuple[Nonempty, ...]
    principle_kinds: tuple[Nonempty, ...]
    event_time_kinds: tuple[Nonempty, ...]
    max_marks: Annotated[int, Field(gt=0)]
    max_text_characters: Annotated[int, Field(gt=0)]

    def check(self, speech: SpeechPolicy) -> None:
        """Refuse a split catalogue or incomplete named capture grammar."""
        if set(self.kinds) != set(speech.marked_lines):
            raise SetupError("capture and extraction marker catalogues differ")
        if not {"marker", "text", "entity"}.issubset(re.compile(speech.marked_pattern).groupindex):
            raise SetupError("capture grammar needs marker, text and entity groups")
        if not set((*self.failure_kinds, *self.principle_kinds, *self.event_time_kinds)).issubset(
            self.kinds.values()
        ):
            raise SetupError("capture roles name an unconfigured kind")
        if set(self.failure_kinds) & set(self.principle_kinds):
            raise SetupError("capture failure/principle roles must be distinct")


@dataclass(frozen=True)
class CaptureMark:
    """One genuine marked line with its exact immutable native source."""

    instance: str
    session: str
    substrate: str
    source_id: str
    timestamp: str
    index: int
    marker: str
    text: str
    entity: str

    @property
    def id(self) -> str:
        """Full scoped identities preserve repeats without relying on native UUID uniqueness."""
        return digest(["marked", self.instance, self.source_id, self.index])


def capture_policy(path: Path | None = None) -> CapturePolicy:
    """Load a complete nonsecret capture policy without opening infrastructure."""
    try:
        raw = (
            path.read_text(encoding="utf-8")
            if path
            else files("anamnema").joinpath("config/capture.json").read_text(encoding="utf-8")
        )
        return CapturePolicy.model_validate_json(raw)
    except (OSError, ValidationError) as exc:
        raise SetupError(f"invalid capture policy ({type(exc).__name__})") from exc


def capture_semantics(speech: SpeechPolicy) -> dict[str, Any]:
    """Identity includes only fields used to attribute, select and sanitize agent marks."""
    return {
        **speech.model_dump(
            mode="json",
            include={
                "marked_lines",
                "marked_pattern",
                "fence_pattern",
                "quote_pattern",
                "secret_word_pattern",
                "secret_symbol_pattern",
                "secret_minimum_characters",
                "secret_mask",
            },
        ),
        "claude": speech.claude.model_dump(
            mode="json",
            include={
                "type_key",
                "message_key",
                "content_key",
                "sidechain_key",
                "meta_key",
                "assistant_type",
                "text_block_type",
                "artifact_marker_pattern",
                "tool_placeholders",
            },
        ),
    }


def record_marks(
    record: Record, policy: CapturePolicy, speech: SpeechPolicy
) -> tuple[CaptureMark, ...]:
    """Derive immutable fields again from verified sources before any graph write."""
    if record.source.get(policy.session_field, record.session) != record.session:
        raise SetupError("capture native session differs from its envelope")
    if record.kind != "conversation":
        return ()
    row = ClaudeSpeechReader(speech).translate(record, record.source_line)
    if not isinstance(row, Row) or row.speaker != "agent":
        return ()
    marks: list[CaptureMark] = []
    for index, (_, match) in enumerate(marked_matches(row.text, speech)):
        text = scrub(match["text"], speech)[0].strip()
        if not text or len(text) > policy.max_text_characters:
            raise SetupError("captured text is empty or exceeds configured bound")
        try:
            if datetime.fromisoformat(record.timestamp).tzinfo is None:
                raise ValueError("timezone missing")
        except ValueError as exc:
            raise SetupError("capture source time must be valid and timezone-aware") from exc
        marks.append(
            CaptureMark(
                record.instance,
                record.session,
                record.substrate,
                record.identity,
                record.timestamp,
                index,
                match["marker"],
                text,
                " ".join((match["entity"] or "").split()),
            )
        )
    return tuple(marks)


def captured_marks(
    snapshot: Snapshot,
    policy: CapturePolicy,
    speech: SpeechPolicy,
    *,
    whole: bool = False,
    last_turn: bool = False,
) -> tuple[CaptureMark, ...]:
    """Use the current open stretch by default; whole-file backstops retain stable IDs."""
    policy.check(speech)
    if whole and last_turn:
        raise SetupError("whole and last-turn capture are mutually exclusive")
    records: list[Record] = []
    ownership: tuple[str, str, str] | None = None
    for record in snapshot.records:
        if record.source.get(policy.session_field, record.session) != record.session:
            raise SetupError("capture native session differs from its envelope")
        scope = record.instance, record.session, record.substrate
        if ownership is not None and ownership != scope:
            raise SetupError("capture requires one instance, session and substrate")
        ownership = scope
        if record.kind == "summary" and not whole:
            records.clear()
        elif record.kind == "conversation":
            if last_turn and _owner_prompt(record, policy, speech):
                records.clear()
            records.append(record)
    marks: list[CaptureMark] = []
    seen: set[str] = set()
    for record in records:
        if record.identity in seen:
            continue
        seen.add(record.identity)
        marks.extend(record_marks(record, policy, speech))
        if len(marks) > policy.max_marks:
            raise SetupError("capture mark count exceeds configured bound; no writes made")
    return tuple(marks)


def _owner_prompt(record: Record, policy: CapturePolicy, speech: SpeechPolicy) -> bool:
    """Native non-meta user records delimit turns even when their text is not speech."""
    schema = speech.claude
    source = record.source
    if (
        source.get(schema.type_key) not in schema.user_types
        or source.get(schema.sidechain_key)
        or source.get(schema.meta_key)
    ):
        return False
    message = source.get(schema.message_key)
    content = message.get(schema.content_key) if isinstance(message, dict) else message
    return (
        isinstance(content, str)
        or isinstance(content, list)
        and not any(
            isinstance(block, dict)
            and block.get(policy.turn_prompt.block_type_field)
            == policy.turn_prompt.tool_result_type
            for block in content
        )
    )


def is_capture_boundary(
    record: Record, policy: CapturePolicy, speech: SpeechPolicy, *, last_turn: bool
) -> bool:
    """Native turn and compaction boundaries are selection, not admission of evidence."""
    return record.kind == "summary" or (
        last_turn and record.kind == "conversation" and _owner_prompt(record, policy, speech)
    )
