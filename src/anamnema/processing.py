"""Shared read-only closed-stretch preparation over attributed archive records."""

from __future__ import annotations

from dataclasses import dataclass

from pydantic import JsonValue

from anamnema.archive_models import Record, Snapshot, digest
from anamnema.setup_config import SetupError


@dataclass(frozen=True)
class Window:
    """One closed stretch, linked to its exact ordered source records."""

    boundary: Record
    records: tuple[Record, ...]

    def report(self) -> dict[str, JsonValue]:
        """Return provenance and counts without disclosing conversation content."""
        return {
            "key": digest([self.boundary.identity, [row.identity for row in self.records]]),
            "instance": self.boundary.instance,
            "session": self.boundary.session,
            "substrate": self.boundary.substrate,
            "boundary": self.boundary.identity,
            "boundary_kind": self.boundary.kind,
            "boundary_line": self.boundary.source_line,
            "records": [
                {
                    "id": row.identity,
                    "line": row.source_line,
                    "payload_digest": digest(row.source),
                }
                for row in self.records
            ],
        }


@dataclass(frozen=True)
class Preparation:
    """Closed stretches plus the open tail and exact source-repeat count."""

    windows: tuple[Window, ...]
    pending: tuple[Record, ...]
    repeats: int

    def report(self, snapshot: Snapshot) -> dict[str, JsonValue]:
        """Describe preparation honestly; no archive/model acceptance is implied."""
        return {
            "mode": "dry-run",
            "archive_verified": False,
            "notes_generated": False,
            "source_sha256": snapshot.sha256,
            "closed_stretches": [window.report() for window in self.windows],
            "pending_records": len(self.pending),
            "exact_source_repeats": self.repeats,
            "ignored": snapshot.ignored,
            "blank_lines": snapshot.blanks,
        }


def prepare(snapshot: Snapshot) -> Preparation:
    """Split one common source sequence without guessing order from timestamps.

    Args:
        snapshot: A stable source capture from the shared substrate reader.

    Returns:
        Closed stretches and records still awaiting a boundary.

    Raises:
        SetupError: Ownership is mixed or a closing marker references another row.
    """
    windows: list[Window] = []
    pending: list[Record] = []
    seen: set[str] = set()
    ownership: tuple[str, str, str] | None = None
    last: Record | None = None
    repeats = 0
    for row in snapshot.records:
        scope = row.instance, row.substrate, row.session
        if ownership is not None and ownership != scope:
            raise SetupError("processing requires one instance, substrate and session")
        ownership = scope
        if row.identity in seen:
            repeats += 1
            continue
        seen.add(row.identity)
        if row.kind == "conversation":
            pending.append(row)
            last = row
        else:
            if row.kind == "closing" and (
                last is None
                or row.source.get("after") != last.identity
                or row.source.get("at") != last.timestamp
            ):
                raise SetupError("closing marker does not reference the preceding conversation")
            windows.append(Window(row, tuple(pending)))
            pending = []
            last = None
    return Preparation(tuple(windows), tuple(pending), repeats)
