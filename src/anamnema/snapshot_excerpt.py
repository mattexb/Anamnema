"""Reviewed UTF-8 fitting and newest-first bounded excerpt packing, Hooks 8d23cfe."""

import string
import sys

from anamnema.setup_config import SetupError


def cut_mark(mark: str, ceiling: int) -> str:
    """Validate real format fields and endpoint sizes, not escaped field-like literals."""
    try:
        fields = {name for _, name, _, _ in string.Formatter().parse(mark) if name is not None}
        if fields != {"left"}:
            raise SetupError("entry_cut_mark requires exactly the left field")
        bounds = [len(mark.format(left=n).encode("utf-8")) for n in (0, sys.maxsize)]
        if max(bounds) >= ceiling:
            raise SetupError("entry_cut_mark does not fit under max_dialogue_bytes")
    except (KeyError, IndexError, ValueError, TypeError) as exc:
        raise SetupError("entry_cut_mark is not a usable format") from exc
    return mark


def fit_entry(entry: str, ceiling: int, mark: str) -> str:
    """Measure the actual final marker each pass; cuts never split a UTF-8 character."""
    data = entry.encode("utf-8")
    if len(data) <= ceiling:
        return entry
    kept = max(0, ceiling - len(mark.format(left=len(data)).encode("utf-8")))
    while kept >= 0:
        head = data[:kept].decode("utf-8", errors="ignore")
        used = len(head.encode("utf-8"))
        tail = mark.format(left=len(data) - used)
        overshoot = used + len(tail.encode("utf-8")) - ceiling
        if overshoot <= 0:
            return head + tail
        kept = used - overshoot
    raise SetupError("entry_cut_mark does not fit under max_dialogue_bytes")


def chunk_entries(entries: list[str], ceiling: int, count: int, mark: str) -> list[str]:
    """Count joining newlines; do not emit empty files for empty or oversized dialogue."""
    cut_mark(mark, ceiling)
    chunks: list[list[str]] = []
    size = 0
    for entry in reversed(entries):
        fitted = fit_entry(entry, ceiling, mark)
        length = len(fitted.encode("utf-8"))
        separator = int(bool(chunks and chunks[-1]))
        if not chunks or size + separator + length > ceiling:
            if len(chunks) == count:
                break
            chunks.append([])
            size = separator = 0
        chunks[-1].append(fitted)
        size += separator + length
    return ["\n".join(chunk) for chunk in chunks]
