"""Neutralized idempotent upserts and independently checked Mongo read-back."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from typing import Any

from pydantic import JsonValue
from pymongo import UpdateOne
from pymongo.database import Database
from pymongo.errors import PyMongoError

from anamnema.archive_codec import decode_source, encode_record, recover_record
from anamnema.archive_models import Kind, Record, Snapshot, canonical, digest
from anamnema.ingest_config import ArchiveSettings
from anamnema.setup_config import SetupError


class MongoArchive:
    """No deletes, replacements or private defaults; only explicit target databases."""

    def __init__(self, database: Database[dict[str, Any]], settings: ArchiveSettings) -> None:
        if not database.write_concern.acknowledged:
            raise SetupError("archive requires acknowledged writes")
        self.database = database
        self.settings = settings

    def write(self, records: list[Record]) -> set[str]:
        """Set-on-insert preserves earlier provenance and makes replay resumable."""
        grouped: dict[Kind, list[Record]] = defaultdict(list)
        for record in records:
            grouped[record.kind].append(record)
        inserted: set[str] = set()
        try:
            for kind, rows in grouped.items():
                operations = [
                    UpdateOne(
                        {"_id": row.identity},
                        {"$setOnInsert": encode_record(row, self.settings)},
                        upsert=True,
                    )
                    for row in rows
                ]
                result = self.database[self.settings.collections[kind]].bulk_write(operations)
                inserted.update(rows[index].identity for index in (result.upserted_ids or {}))
        except PyMongoError as exc:
            # Driver errors can contain entire records, authentication URIs or failed operations.
            raise SetupError(f"archive write failed ({type(exc).__name__}); safe to rerun") from exc
        return inserted

    def verify(self, records: list[Record], inserted: set[str] | None = None) -> None:
        """Recompute source content, not merely compare counts or stored digest fields."""
        grouped: dict[Kind, list[Record]] = defaultdict(list)
        for record in records:
            grouped[record.kind].append(record)
        try:
            for kind, rows in grouped.items():
                wanted = {row.identity: row for row in rows}
                found = list(
                    self.database[self.settings.collections[kind]].find(
                        {"_id": {"$in": list(wanted)}}
                    )
                )
                if len(found) != len(wanted):
                    raise SetupError("archive read-back is incomplete; no completed receipt")
                for document in found:
                    identity = document.get("_id")
                    expected = wanted.get(identity) if isinstance(identity, str) else None
                    if expected is None or digest(decode_source(document, self.settings)) != digest(
                        expected.source
                    ):
                        raise SetupError("archive source differs; no completed receipt")
                    if canonical({key: document.get(key) for key in expected.core()}) != canonical(
                        expected.core()
                    ):
                        raise SetupError("archive ownership/identity differs; no completed receipt")
                    line = document.get("source_line")
                    if type(line) is not int or line < 1:
                        raise SetupError("archive source line is invalid; no completed receipt")
                    if inserted and identity in inserted and line != expected.source_line:
                        raise SetupError("new archive source line differs; no completed receipt")
        except PyMongoError as exc:
            raise SetupError(
                f"archive read-back failed ({type(exc).__name__}); safe to rerun"
            ) from exc

    def restore(self, instance: str, kind: Kind, identities: tuple[str, ...]) -> tuple[Record, ...]:
        """Recover only an explicit owned bounded identity set, retaining requested order."""
        if (
            not instance
            or not 1 <= len(identities) <= self.settings.batch_size
            or len(set(identities)) != len(identities)
        ):
            raise SetupError("archive restore request exceeds configured count or repeats an id")
        if kind not in self.settings.collections:
            raise SetupError("archive restore record kind is unavailable")
        try:
            documents = self.database[self.settings.collections[kind]].find(
                {"_id": {"$in": list(identities)}, "instance": instance}
            )
            found: dict[str, Record] = {}
            total = 0
            for document in documents:
                record = recover_record(document, self.settings)
                total += len(canonical(record.source))
                if total > self.settings.max_file_bytes:
                    raise SetupError("archive restore exceeds configured byte bound")
                if (
                    record.instance != instance
                    or record.kind != kind
                    or record.identity not in identities
                ):
                    raise SetupError("archive restore ownership/kind differs")
                found[record.identity] = record
        except PyMongoError as exc:
            raise SetupError(f"archive restore failed ({type(exc).__name__})") from exc
        if len(found) != len(identities):
            raise SetupError("archive restore is incomplete or belongs to another assistant")
        return tuple(found[identity] for identity in identities)

    def complete(self, snapshot: Snapshot, records: list[Record]) -> None:
        """A deterministic receipt exists only after all imported records verify."""
        if not records:
            return
        first = records[0]
        body: dict[str, JsonValue] = {
            "schema_version": self.settings.schema_version,
            "instance": first.instance,
            "session": first.session,
            "substrate": first.substrate,
            "source_sha256": snapshot.sha256,
            "closed": snapshot.closed,
            "records": len(records),
            "records_digest": digest([row.identity for row in records]),
        }
        receipt: dict[str, JsonValue] = {"_id": digest(body), **body}
        try:
            collection = self.database[self.settings.collections["receipt"]]
            collection.update_one({"_id": receipt["_id"]}, {"$setOnInsert": receipt}, upsert=True)
            if canonical(collection.find_one({"_id": receipt["_id"]})) != canonical(receipt):
                raise SetupError("archive receipt read-back differs")
        except PyMongoError as exc:
            raise SetupError(
                f"archive receipt failed ({type(exc).__name__}); safe to rerun"
            ) from exc


def import_snapshot(snapshot: Snapshot, archive: MongoArchive) -> dict[str, object]:
    """Use the source's write/read-back method; partial imports are safely replayed."""
    unique: dict[str, Record] = {}
    for row in snapshot.records:
        unique.setdefault(row.identity, row)
    records = list(unique.values())
    inserted = 0
    for batch in chunks(records, archive.settings.batch_size):
        new_ids = archive.write(batch)
        inserted += len(new_ids)
        archive.verify(batch, new_ids)
    archive.complete(snapshot, records)
    return {
        **snapshot.summary(),
        "mode": "apply",
        "verified": True,
        "inserted": inserted,
        "already_present": len(records) - inserted,
        "exact_source_repeats": len(snapshot.records) - len(records),
    }


def chunks(records: list[Record], size: int) -> Iterable[list[Record]]:
    """Bound write and independent read-back requests with the configured batch size."""
    for start in range(0, len(records), size):
        yield records[start : start + size]
