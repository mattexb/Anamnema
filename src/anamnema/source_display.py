"""Source-derived speaker-turn context and citation-first budgeting over captured order."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from pydantic import JsonValue

from anamnema.archive_models import Record
from anamnema.source_policy import SourcePolicy
from anamnema.speech import Row
from anamnema.substrates.speech_base import SpeechReader
from anamnema.window_store import WindowStore

Lookup = Callable[[str, tuple[str, ...]], tuple[Record, ...]]


class SourceDisplay:
    """A substrate reader is injected; the shared context algorithm never guesses native roles."""

    def __init__(self, windows: WindowStore, reader: SpeechReader) -> None:
        self.windows, self.reader = windows, reader
        self.policy = windows.policy

    def read(
        self, instance: str, note: dict[str, Any], cited: tuple[Record, ...], lookup: Lookup
    ) -> dict[str, JsonValue]:
        """Show bounded context or an explicit unavailable reason, never a guessed window."""
        if not note.get("window"):
            if (
                note.get("capture_mode") == "marked"
                and note.get("substrate") == self.windows.archive.settings.substrate
            ):
                return self.cited_only(cited)
            return unavailable("note has no captured conversation window")
        if note.get("substrate") != self.windows.archive.settings.substrate:
            return unavailable("no source reader configured for this substrate")
        reference = self.windows.load(instance, note["session"], note["substrate"], note["window"])
        if reference is None:
            return unavailable("captured window references are missing")
        positions = {identity: index for index, identity in enumerate(reference.records)}
        if any(record.identity not in positions for record in cited):
            return unavailable("a cited record is outside the note's original window")
        anchors = sorted(positions[record.identity] for record in cited)
        if len(anchors) > self.policy.context_records:
            return unavailable("citation count exceeds the configured context scan bound")
        selected = nearby(len(reference.records), anchors, self.policy.context_records)
        originals = lookup(instance, tuple(reference.records[index] for index in selected))
        speech = self.reader.read_records(zip(selected, originals, strict=True))
        wanted, complete = context_span(
            speech.rows, selected, set(anchors), self.policy.source_turns, len(reference.records)
        )
        output, clipped = budget_rows(wanted, self.policy)
        translated = {row.source.identity for row in speech.rows}
        untranslated = [record.identity for record in cited if record.identity not in translated]
        context: dict[str, JsonValue] = {
            "status": "available" if complete and not clipped and not untranslated else "partial",
            "window": reference.key,
            "scan_limited": len(selected) < len(reference.records),
            "budget_reached": clipped,
            "untranslated_citations": [identity for identity in untranslated],
            "masked_rows": speech.masked,
            "dropped": {name: count for name, count in speech.dropped.items()},
        }
        return {"rows": output, "context": context}

    def cited_only(self, cited: tuple[Record, ...]) -> dict[str, JsonValue]:
        """Show proven cited words for a manual note without inventing neighbouring context."""
        if len(cited) > self.policy.context_records:
            return unavailable("citation count exceeds the configured context scan bound")
        speech = self.reader.read_records(enumerate(cited), preserve_marked=True)
        output, clipped = budget_rows([(row, True, 0) for row in speech.rows], self.policy)
        return {
            "rows": output,
            "context": {
                "status": "partial",
                "source_only": True,
                "reason": "manual capture has no closed conversation window",
                "budget_reached": clipped,
                "masked_rows": speech.masked,
                "dropped": {name: count for name, count in speech.dropped.items()},
            },
        }


def unavailable(reason: str) -> dict[str, JsonValue]:
    """Raw originals can still be opened even when neighbouring context cannot be proved."""
    return {"rows": [], "context": {"status": "unavailable", "reason": reason}}


def nearby(length: int, anchors: list[int], limit: int) -> list[int]:
    """Bound scanning nearest citations first, with captured sequence as a stable tie-breaker."""
    selected = set(anchors)
    for distance in range(1, limit + 1):
        remaining = limit - len(selected)
        if remaining <= 0 or len(selected) == length:
            break
        candidates = {
            index
            for anchor in anchors
            for index in (anchor - distance, anchor + distance)
            if 0 <= index < length
        }
        selected.update(sorted(candidates - selected)[:remaining])
    return sorted(selected)


def context_span(
    rows: tuple[Row, ...], selected: list[int], anchors: set[int], turns: int, length: int
) -> tuple[list[tuple[Row, bool, int]], bool]:
    """Count speaker stretches only across known contiguous source ranges."""
    blocks: list[list[int]] = []
    for index in selected:
        if not blocks or index != blocks[-1][-1] + 1:
            blocks.append([])
        blocks[-1].append(index)
    kept: set[int] = set()
    complete = True
    for block in blocks:
        part = [row for row in rows if block[0] <= row.n <= block[-1]]
        cited = [index for index, row in enumerate(part) if row.n in anchors]
        if not cited:
            continue
        first, back = turn_edge(part, cited[0], -1, turns)
        last, forward = turn_edge(part, cited[-1], 1, turns)
        complete = complete and (back or block[0] == 0) and (forward or block[-1] == length - 1)
        kept.update(row.n for row in part[first : last + 1])
    places = [index for index, row in enumerate(rows) if row.n in anchors]
    if not places:
        return [], False
    return [
        (row, row.n in anchors, min(abs(index - place) for place in places))
        for index, row in enumerate(rows)
        if row.n in kept
    ], complete


def turn_edge(rows: list[Row], place: int, step: int, turns: int) -> tuple[int, bool]:
    """The source's speaker-stretch boundary, with an explicit known/truncated edge."""
    seen, index = 0, place
    while 0 <= index + step < len(rows):
        if rows[index + step].speaker != rows[index].speaker:
            seen += 1
            if seen > turns:
                return index, True
        index += step
    return index, False


def budget_rows(
    rows: list[tuple[Row, bool, int]], policy: SourcePolicy
) -> tuple[list[JsonValue], bool]:
    """Spend text budget at citations first; a clipped row says so and sources stay untouched."""
    budget = policy.source_tokens * policy.characters_per_token
    kept: list[tuple[int, dict[str, JsonValue]]] = []
    clipped = False
    for row, cited, _ in sorted(rows, key=lambda item: (item[2], item[0].n)):
        text = row.text[:budget]
        if not text:
            clipped = True
            break
        cut = len(text) < len(row.text)
        kept.append(
            (
                row.n,
                {
                    "record": row.source.identity,
                    "speaker": row.speaker,
                    "at": row.source.timestamp,
                    "cited": cited,
                    "text": text,
                    "clipped": cut,
                },
            )
        )
        budget -= len(text)
        if cut:
            clipped = True
            break
    return [value for _, value in sorted(kept, key=lambda item: item[0])], clipped
