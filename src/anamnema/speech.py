"""Shared attributed speech shape, filtering and password-shaped masking."""

from __future__ import annotations

import re
from collections.abc import Iterator
from dataclasses import dataclass, replace
from typing import Literal

from anamnema.archive_models import Record
from anamnema.memhub_parsing import sanitize_model_input
from anamnema.speech_policy import SpeechPolicy


@dataclass(frozen=True)
class Row:
    """Kept words with their original record; never a tool or injected owner turn."""

    n: int
    speaker: Literal["owner", "agent"]
    text: str
    source: Record


def own_words(text: str, policy: SpeechPolicy) -> str:
    """Retain an owner's words, not pasted code or other speakers' room turns."""
    for pattern in policy.owner_envelopes:
        text = re.sub(pattern, "", text)
    turns = list(re.finditer(policy.owner_room_turn, text, re.MULTILINE))
    if not turns:
        return text.strip()
    parts = [text[: turns[0].start()]]
    for index, turn in enumerate(turns):
        end = turns[index + 1].start() if index + 1 < len(turns) else len(text)
        if turn["who"] == policy.owner_room_speaker:
            parts.append(text[turn.end() : end])
    return "\n".join(part.strip() for part in parts if part.strip())


def marked_matches(text: str, policy: SpeechPolicy) -> Iterator[tuple[int, re.Match[str]]]:
    """One detector serves capture and exclusion; quoted/fenced examples are not said."""
    fenced = False
    for number, line in enumerate(text.split("\n")):
        if re.match(policy.fence_pattern, line):
            fenced = not fenced
            continue
        match = re.match(policy.marked_pattern, line)
        marked = match is not None and match["marker"] in policy.marked_lines
        if not fenced and not re.match(policy.quote_pattern, line) and marked and match:
            yield number, match


def without_marked(text: str, policy: SpeechPolicy) -> str:
    """Remove genuine self-note lines, but preserve fenced/quoted examples."""
    omitted = {number for number, _ in marked_matches(text, policy)}
    return "\n".join(line for n, line in enumerate(text.split("\n")) if n not in omitted).strip()


def scrub(text: str, policy: SpeechPolicy) -> tuple[str, int]:
    """Keep both upstream credential sanitization and source password-shape masking."""
    cleaned = sanitize_model_input(text)

    def mask(match: re.Match[str]) -> str:
        word = match.group()
        mixed = re.search(r"[a-z]", word) and re.search(r"[A-Z]", word) and re.search(r"\d", word)
        shaped = re.search(policy.secret_symbol_pattern, word)
        if mixed and (shaped or len(word) >= policy.secret_minimum_characters):
            return policy.secret_mask
        return word

    result = re.sub(policy.secret_word_pattern, mask, cleaned)
    return result, int(result != text)


def model_row(row: Row, policy: SpeechPolicy) -> tuple[Row | None, int]:
    """Filter and sanitize a native reader's attributed row without changing source."""
    if row.speaker == "owner":
        if row.text.lstrip().startswith(policy.owner_noise_prefixes):
            return None, 0
        text = own_words(row.text, policy)
    else:
        text = without_marked(row.text, policy)
        for pattern in policy.agent_envelopes:
            text = re.sub(pattern, "", text)
    text, masked = scrub(text.strip(), policy)
    return (replace(row, text=text) if text else None), masked
