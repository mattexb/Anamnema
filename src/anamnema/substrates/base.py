"""Reusable source-reading loop; a substrate extends classification, not storage."""

from __future__ import annotations

import hashlib
import os
from abc import ABC, abstractmethod
from pathlib import Path

from pydantic import JsonValue, TypeAdapter, ValidationError

from anamnema.archive_models import Kind, Record, Snapshot, canonical
from anamnema.ingest_config import ArchiveSettings
from anamnema.setup_config import SetupError

JSON_OBJECT = TypeAdapter(dict[str, JsonValue])


class SubstrateAdapter(ABC):
    """Shared bounded reader; native event translation is a later M5 capability."""

    def __init__(self, settings: ArchiveSettings) -> None:
        self.settings = settings

    @abstractmethod
    def classify(self, source: dict[str, JsonValue]) -> Kind | None:
        """Choose conversation/summary or discard native bookkeeping."""

    def read(self, path: Path, instance: str, session: str, *, closed: bool = False) -> Snapshot:
        """Adapt the existing numbered-JSONL preservation loop without modifying input."""
        if not instance.strip() or not session.strip():
            raise SetupError("instance and session must be explicit and nonempty")
        if path.is_symlink() or not path.is_file():
            raise SetupError("transcript must be an existing regular file, not a symlink")
        try:
            return self._read(path, instance, session, closed)
        except (OSError, UnicodeError) as exc:
            raise SetupError(f"transcript cannot be read ({type(exc).__name__})") from exc

    def _read(self, path: Path, instance: str, session: str, closed: bool) -> Snapshot:
        records: list[Record] = []
        ignored = blanks = total = 0
        hashed = hashlib.sha256()
        with path.open("rb") as stream:
            before = os.fstat(stream.fileno())
            if before.st_size > self.settings.max_file_bytes:
                raise SetupError("transcript exceeds configured read limit")
            lines = iter(lambda: stream.readline(self.settings.max_file_bytes - total + 1), b"")
            for number, raw in enumerate(lines, start=1):
                total += len(raw)
                if total > self.settings.max_file_bytes:
                    raise SetupError("transcript grew beyond configured read limit")
                hashed.update(raw)
                if not raw.strip():
                    blanks += 1
                    continue
                try:
                    source = JSON_OBJECT.validate_json(raw.decode(self.settings.reader.encoding))
                    canonical(source)
                    kind = self.classify(source)
                except (ValidationError, ValueError, UnicodeError) as exc:
                    raise SetupError(
                        f"invalid transcript record at physical line {number}"
                    ) from exc
                if kind is None:
                    ignored += 1
                    continue
                timestamp = source.get(self.settings.reader.timestamp)
                if not isinstance(timestamp, str) or not timestamp:
                    raise SetupError(f"missing source timestamp at physical line {number}")
                records.append(
                    Record(
                        instance, self.settings.substrate, session, kind, timestamp, number, source
                    )
                )
            after = os.fstat(stream.fileno())
            named = path.stat()
        if _stamp(before) != _stamp(after) or _stamp(named) != _stamp(after) or path.is_symlink():
            raise SetupError("transcript changed while being read; retry from a stable snapshot")
        if closed and records and records[-1].kind != "summary":
            last = records[-1]
            records.append(
                Record(
                    instance,
                    last.substrate,
                    session,
                    "closing",
                    last.timestamp,
                    last.source_line,
                    {"after": last.identity, "at": last.timestamp},
                )
            )
        return Snapshot(path, hashed.hexdigest(), tuple(records), ignored, blanks, closed)


def _stamp(value: os.stat_result) -> tuple[int, int, int, int]:
    """Check both the opened file and its current pathname for replacement/growth."""
    return value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns
