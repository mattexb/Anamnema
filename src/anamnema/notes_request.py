"""Tested prompt rendering, request schema and complete extraction reuse identity."""

from functools import cache
from importlib.resources import files

from pydantic import JsonValue

from anamnema.archive_models import digest
from anamnema.chunks import Chunk
from anamnema.notes_policy import NotesPolicy
from anamnema.notes_response import KINDS
from anamnema.setup_config import SetupError
from anamnema.speech_policy import SpeechPolicy


@cache
def load_template() -> str:
    """Freeze the reviewed prompt per process, as the source implementation does."""
    try:
        return (
            files("anamnema")
            .joinpath("config/notes_prompt.txt")
            .read_text(encoding="utf-8")
            .removesuffix("\n")
        )
    except (OSError, UnicodeError) as exc:
        raise SetupError("packaged notes prompt cannot be read") from exc


def render(policy: NotesPolicy, text: str) -> str:
    """Render the reviewed prompt without private speaker names."""
    # The reviewed source template has no final newline; package text files do.
    return load_template().format(
        owner=policy.names.owner,
        agent=policy.names.agent,
        sent=policy.names.sent,
        max_notes=policy.max_notes,
        note_characters=policy.note_characters,
        slice=text,
    )


def request_body(chunk: Chunk, policy: NotesPolicy, model: str) -> dict[str, JsonValue]:
    """Use the reviewed model options and budget/schema, controlled outside options."""
    per_note = (
        policy.note_characters // policy.answer_characters_per_token + policy.note_overhead_tokens
    )
    return {
        "model": model,
        "messages": [{"role": "user", "content": render(policy, chunk.text)}],
        **policy.model_options,
        "stream": False,
        "max_tokens": policy.max_notes * per_note + policy.answer_overhead_tokens,
        "response_format": {"type": "json_schema", "json_schema": {"schema": schema(policy)}},
    }


def schema(policy: NotesPolicy) -> dict[str, JsonValue]:
    """Mirror local validation while retaining citation issues for explicit review."""
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "concepts": {
                "type": "array",
                "minItems": 1,
                "maxItems": policy.max_notes,
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "text": {
                            "type": "string",
                            "minLength": policy.minimum_note_characters,
                            "maxLength": policy.note_characters,
                        },
                        "rows": {
                            "type": "array",
                            "items": {"type": "integer"},
                            "maxItems": policy.max_rows_per_note,
                        },
                        "kind": {"type": "string", "enum": list(KINDS)},
                        "ruled_by": {"type": "integer"},
                    },
                    "required": ["text", "rows", "kind", "ruled_by"],
                },
            }
        },
        "required": ["concepts"],
    }


def run_identity(model: dict[str, JsonValue], policy: NotesPolicy, speech: SpeechPolicy) -> str:
    """Fingerprint all result-shaping inputs, retaining the source request-options fix."""
    return digest(
        {
            "model": model,
            "prompt": render(policy, ""),
            "request": request_body(Chunk(0, (), ""), policy, str(model["alias"])),
            "notes_policy": policy.model_dump(mode="json"),
            "speech_policy": speech.model_dump(mode="json"),
            "processing_schema": 1,
        }
    )
