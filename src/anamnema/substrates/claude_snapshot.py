"""Claude compaction boundaries/model selection, reusing public attributed speech filtering."""

from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from pydantic import ValidationError

from anamnema.archive_models import Record, canonical
from anamnema.continuity_state import source_text
from anamnema.setup_config import SetupError
from anamnema.snapshot_policy import SnapshotProducerPolicy, SnapshotWorkerConfig
from anamnema.speech_policy import SpeechPolicy
from anamnema.substrates.base import JSON_OBJECT
from anamnema.substrates.claude_speech import ClaudeSpeechReader


@dataclass(frozen=True)
class SnapshotDialogue:
    """Eligible dialogue since the latest boundary; never archived as invented native rows."""

    entries: list[str]
    model: str | None


@dataclass(frozen=True)
class ClaudeSnapshotEvent:
    """Only this native adapter interprets PreCompact routing fields."""

    session: str
    transcript: Path
    trigger: str


def precompact_event(
    payload: object, worker: SnapshotWorkerConfig, policy: SnapshotProducerPolicy
) -> ClaudeSnapshotEvent:
    """Native cwd must match the explicit deployment; stdin cannot choose a worker."""
    if not isinstance(payload, dict):
        raise SetupError("Claude PreCompact input must be an object")
    data = cast(dict[str, Any], payload)
    fields = policy.hook_fields
    if data.get(fields["event"]) != policy.hook_event:
        raise SetupError("Claude hook event does not match PreCompact")
    used: dict[str, str] = {}
    for key in ("session", "transcript", "trigger", "cwd"):
        value = data.get(fields[key])
        if not isinstance(value, str) or not value.strip():
            raise SetupError(f"Claude PreCompact requires {key}")
        used[key] = value
    if not Path(used["cwd"]).is_absolute() or not Path(used["transcript"]).is_absolute():
        raise SetupError("Claude PreCompact paths must be absolute")
    if Path(used["cwd"]).resolve() != worker.cwd.resolve():
        raise SetupError("Claude PreCompact cwd differs from worker deployment")
    return ClaudeSnapshotEvent(used["session"], Path(used["transcript"]), used["trigger"])


def snapshot_dialogue(
    path: Path, instance: str, session: str, policy: SnapshotProducerPolicy, speech: SpeechPolicy
) -> SnapshotDialogue:
    """Reject unstable/malformed/foreign transcripts; keep genuine explicit self-corrections."""
    text = source_text(path, policy.max_transcript_bytes)
    reader = ClaudeSpeechReader(speech.model_copy(update={"marked_lines": ()}))
    records: list[tuple[int, Record]] = []
    model: str | None = None
    native, schema = policy.native, speech.claude
    for number, line in enumerate(text.split("\n"), start=1):
        line = line.lstrip("\x00").strip()
        if not line:
            continue
        try:
            source = JSON_OBJECT.validate_json(line, strict=True)
            canonical(source)
        except (ValidationError, ValueError) as exc:
            raise SetupError(f"invalid snapshot transcript at physical line {number}") from exc
        if source.get(native["session"], session) != session:
            raise SetupError("snapshot transcript contains another session")
        if source.get(native["subtype"]) == native["boundary"]:
            records.clear()
            continue
        message = source.get(schema.message_key)
        if (
            source.get(schema.type_key) == schema.assistant_type
            and not source.get(schema.sidechain_key)
            and not source.get(schema.meta_key)
            and isinstance(message, dict)
        ):
            candidate = message.get(native["model"])
            if isinstance(candidate, str) and candidate and candidate != native["synthetic"]:
                model = candidate
        if source.get(native["summary"]):
            continue
        timestamp = source.get(native["timestamp"], "")
        if not isinstance(timestamp, str):
            raise SetupError("snapshot transcript timestamp must be text")
        records.append(
            (number, Record(instance, "claude", session, "conversation", timestamp, number, source))
        )
    rows = reader.read_records(records).rows
    entries = [f"=== {row.source.timestamp} {row.speaker}\n{row.text}\n" for row in rows]
    return SnapshotDialogue(entries, model)
