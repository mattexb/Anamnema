"""Locally validated extraction output with explicit source and citation problems."""

from __future__ import annotations

import re
from typing import Literal

from pydantic import JsonValue, ValidationError

from anamnema.chunks import Chunk
from anamnema.notes_policy import NotesPolicy
from anamnema.setup_config import Nonempty, SetupError, StrictModel
from anamnema.speech import Row, scrub
from anamnema.speech_policy import SpeechPolicy

Kind = Literal["decision", "rule", "outcome", "cause", "design", "open", "person", "fact"]
KINDS = ("decision", "rule", "outcome", "cause", "design", "open", "person", "fact")


class Concept(StrictModel):
    """The model's proposed note, not an independently verified fact."""

    text: Nonempty
    rows: tuple[int, ...]
    kind: Kind
    ruled_by: int


class Response(StrictModel):
    """Strictly typed answer before any source-reference interpretation."""

    concepts: tuple[Concept, ...]


def validate_response(content: str, policy: NotesPolicy, speech: SpeechPolicy) -> Response:
    """Refuse malformed/oversized output and sanitize text before persistence."""
    try:
        response = Response.model_validate_json(content)
        if not 1 <= len(response.concepts) <= policy.max_notes:
            raise ValueError("note count")
        sanitized: list[Concept] = []
        for concept in response.concepts:
            text, _ = scrub(concept.text, speech)
            if not policy.minimum_note_characters <= len(text) <= policy.note_characters:
                raise ValueError("note size")
            if len(concept.rows) > policy.max_rows_per_note:
                raise ValueError("citation count")
            sanitized.append(concept.model_copy(update={"text": text}))
        return Response(concepts=tuple(sanitized))
    except (ValidationError, ValueError) as exc:
        raise SetupError("model answer violates the configured notes schema") from exc


def source_notes(response: Response, chunk: Chunk, policy: NotesPolicy) -> list[JsonValue]:
    """Retain bad-citation flags; nearest evidence is explicitly only a candidate."""
    return [_note(concept, chunk, policy) for concept in response.concepts]


def _note(concept: Concept, chunk: Chunk, policy: NotesPolicy) -> dict[str, JsonValue]:
    by_number = {row.n: row for row in chunk.rows}
    problems = [f"row {n} not in chunk" for n in concept.rows if n not in by_number]
    if not concept.rows:
        problems.append("no cited rows")
    owner_pattern = policy.owner_decision_pattern.replace("{owner}", re.escape(policy.names.owner))
    decided = concept.kind in ("decision", "rule") or re.search(owner_pattern, concept.text, re.I)
    ruling = by_number.get(concept.ruled_by)
    owner = ruling if decided and ruling is not None and ruling.speaker == "owner" else None
    if decided and owner is None:
        problems.append("decision/rule not bound to an owner row inside the chunk")
    pointed = [by_number[n] for n in concept.rows if n in by_number]
    fallback = not pointed
    if fallback:
        pointed = _nearest(concept.text, chunk.rows, policy)
        problems.append("nearest-row candidate evidence; citations do not establish support")
    note: dict[str, JsonValue] = {
        "text": concept.text,
        "kind": concept.kind,
        "rows": list(concept.rows),
        "ruled_by": concept.ruled_by,
        "owner_source": owner.source.identity if owner else None,
        "source_ids": [identity for identity in sorted({row.source.identity for row in pointed})],
        "said_at": min((row.source.timestamp for row in pointed), default=None),
        "candidate_evidence": fallback,
        "problems": [problem for problem in problems],
    }
    return note


def _nearest(text: str, rows: tuple[Row, ...], policy: NotesPolicy) -> list[Row]:
    mine = set(re.findall(policy.note_word_pattern, text.lower()))
    ranked = sorted(
        rows,
        key=lambda row: (
            -len(mine & set(re.findall(policy.note_word_pattern, row.text.lower()))),
            row.n,
        ),
    )
    return ranked[: policy.nearest_rows]
