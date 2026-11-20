"""Immutable normalized archive records and deterministic typed JSON identity."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pydantic import JsonValue

Kind = Literal["conversation", "summary", "closing"]


def canonical(value: JsonValue) -> bytes:
    """Keep JSON scalar types distinct when comparing sources and identities."""
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")


def digest(value: JsonValue) -> str:
    """Content-derived identity, never based on an absolute machine path."""
    return hashlib.sha256(canonical(value)).hexdigest()


@dataclass(frozen=True)
class Record:
    """One original source object plus explicit public ownership/provenance."""

    instance: str
    substrate: str
    session: str
    kind: Kind
    timestamp: str
    source_line: int
    source: dict[str, JsonValue]

    @property
    def identity(self) -> str:
        """Native UUID reuse is valid; source content and time identify the variant."""
        return digest(
            [
                self.instance,
                self.substrate,
                self.session,
                self.kind,
                self.timestamp,
                digest(self.source),
            ]
        )

    def core(self) -> dict[str, JsonValue]:
        """Stable fields that must match independently read archive content."""
        return {
            "_id": self.identity,
            "instance": self.instance,
            "substrate": self.substrate,
            "session": self.session,
            "kind": self.kind,
            "timestamp": self.timestamp,
            "payload_digest": digest(self.source),
        }


@dataclass(frozen=True)
class Snapshot:
    """Captured file state; no storage connection or source modification."""

    path: Path
    sha256: str
    records: tuple[Record, ...]
    ignored: int
    blanks: int
    closed: bool

    def summary(self) -> dict[str, JsonValue]:
        """A non-content report suitable for terminals and diagnostics."""
        counts: dict[str, JsonValue] = {
            kind: sum(record.kind == kind for record in self.records)
            for kind in ("conversation", "summary", "closing")
        }
        return {
            "sha256": self.sha256,
            "counts": counts,
            "ignored": self.ignored,
            "blank_lines": self.blanks,
            "closed_requested": self.closed,
        }
