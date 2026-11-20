"""Source-linked chunks with the existing character and conservative token limits."""

import re
from dataclasses import dataclass

from anamnema.binding import Binding, describe
from anamnema.notes_policy import NotesPolicy
from anamnema.setup_config import SetupError
from anamnema.speech import Row


@dataclass(frozen=True)
class Chunk:
    """One bounded model input with the exact spoken rows that produced it."""

    index: int
    rows: tuple[Row, ...]
    text: str


def estimated_tokens(text: str, policy: NotesPolicy) -> int:
    """Estimate only; this is not a model tokenizer or final context acceptance."""
    letters = len(re.findall(policy.letter_pattern, text))
    spaces = len(re.findall(policy.space_pattern, text))
    return letters // policy.letters_per_token + len(text) - letters - spaces


def chunks(
    rows: tuple[Row, ...], found: dict[int, Binding], policy: NotesPolicy
) -> tuple[Chunk, ...]:
    """Chunk labelled rows; never silently truncate a row or send an oversized one."""
    out: list[Chunk] = []
    current: list[Row] = []
    lines: list[str] = []
    size = cost = 0
    for row in rows:
        label = policy.names.owner if row.speaker == "owner" else policy.names.agent
        line = f"[#{row.n} {label}] {row.text}"
        if row.n in found:
            line += "\n" + describe(found[row.n], policy.names)
        line_cost = estimated_tokens(line, policy)
        if len(line) > policy.chunk_characters or line_cost > policy.chunk_tokens:
            raise SetupError(f"spoken row {row.n} exceeds configured chunk bounds; no truncation")
        if current and (
            size + len(line) > policy.chunk_characters or cost + line_cost > policy.chunk_tokens
        ):
            out.append(Chunk(len(out), tuple(current), "\n".join(lines)))
            current, lines, size, cost = [], [], 0, 0
        current.append(row)
        lines.append(line)
        size += len(line) + 1
        cost += line_cost + 1
    if current:
        out.append(Chunk(len(out), tuple(current), "\n".join(lines)))
    return tuple(out)
