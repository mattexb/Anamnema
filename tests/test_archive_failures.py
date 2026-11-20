"""Bounded faults cannot certify a partial or corrupt archive."""

import json
import lzma
import uuid
from pathlib import Path

import pytest
from pymongo.errors import OperationFailure
from pymongo.write_concern import WriteConcern

from anamnema.archive_codec import decode_source, encode_record
from anamnema.archive_models import Record
from anamnema.archive_mongo import MongoArchive, import_snapshot
from anamnema.ingest_config import archive_settings
from anamnema.setup_config import SetupError
from anamnema.substrates.claude import ClaudeAdapter


def snapshot(tmp_path: Path):
    path = tmp_path / "source.jsonl"
    path.write_text(
        json.dumps({"type": "user", "timestamp": "2026-01-01", "flag": True}), encoding="utf-8"
    )
    return ClaudeAdapter(archive_settings()).read(path, "agent", "session")


def archive_for(disposable_mongo):
    return MongoArchive(disposable_mongo[1]["test_" + uuid.uuid4().hex], archive_settings())


def test_json_integer_outside_bson_range_is_preserved() -> None:
    source = {"type": "user", "timestamp": "2026-01-01", "wide": 2**100, "flag": True}
    record = Record("agent", "claude", "session", "conversation", "2026-01-01", 1, source)
    encoded = encode_record(record, archive_settings())
    assert "source_lzma" in encoded and decode_source(encoded, archive_settings()) == source


@pytest.mark.parametrize(
    "data", [b"garbage", lzma.compress(b"not json"), lzma.compress(b"{}")[:-2]]
)
def test_invalid_compression_fails(data: bytes) -> None:
    with pytest.raises(SetupError, match="cannot be recovered"):
        decode_source({"source_lzma": data}, archive_settings())


def test_decompression_limit_and_oversized_failure() -> None:
    limited = archive_settings().model_copy(update={"max_file_bytes": 2})
    with pytest.raises(SetupError):
        decode_source({"source_lzma": lzma.compress(b'{"x":123}')}, limited)
    record = Record("agent", "claude", "session", "conversation", "date", 1, {"x": 1})
    with pytest.raises(SetupError, match="oversized"):
        encode_record(record, archive_settings().model_copy(update={"max_document_bytes": 1}))


def test_native_file_changed_while_reading_is_refused(tmp_path: Path) -> None:
    path = snapshot(tmp_path).path

    class Changed(ClaudeAdapter):
        def classify(self, source):
            with path.open("a", encoding="utf-8") as stream:
                stream.write("\n")
            return super().classify(source)

    with pytest.raises(SetupError, match="changed while"):
        Changed(archive_settings()).read(path, "agent", "session")


@pytest.mark.parametrize(
    "field,value,message",
    [
        ("instance", "different", "ownership"),
        ("source_line", False, "source line"),
        ("source.flag", 1, "source differs"),
    ],
)
def test_corrupt_provenance_or_scalar_types_fail(
    tmp_path: Path, disposable_mongo, field, value, message
) -> None:
    archive = archive_for(disposable_mongo)
    read = snapshot(tmp_path)
    archive.write(list(read.records))
    archive.database[archive.settings.collections["conversation"]].update_one(
        {"_id": read.records[0].identity}, {"$set": {field: value}}
    )
    with pytest.raises(SetupError, match=message):
        import_snapshot(read, archive)
    assert archive.database[archive.settings.collections["receipt"]].count_documents({}) == 0


def test_first_written_line_is_independently_verified(tmp_path: Path, disposable_mongo) -> None:
    archive = archive_for(disposable_mongo)
    read = snapshot(tmp_path)
    written = archive.write(list(read.records))
    archive.database[archive.settings.collections["conversation"]].update_one(
        {"_id": read.records[0].identity}, {"$set": {"source_line": 99}}
    )
    with pytest.raises(SetupError, match="new archive source line"):
        archive.verify(list(read.records), written)


def test_missing_readback_and_unacknowledged_writes_fail(tmp_path: Path, disposable_mongo) -> None:
    archive = archive_for(disposable_mongo)
    with pytest.raises(SetupError, match="incomplete"):
        archive.verify(list(snapshot(tmp_path).records))
    with pytest.raises(SetupError, match="acknowledged"):
        MongoArchive(
            archive.database.with_options(write_concern=WriteConcern(w=0)), archive.settings
        )


@pytest.mark.parametrize("action", ["write", "verify", "complete"])
def test_driver_failures_are_redacted(tmp_path: Path, disposable_mongo, action: str) -> None:
    archive = archive_for(disposable_mongo)
    read = snapshot(tmp_path)

    class FailedDatabase:
        def __getitem__(self, name):
            return self

        def bulk_write(self, *args, **kwargs):
            raise OperationFailure("synthetic-secret-never-print")

        find = bulk_write
        update_one = bulk_write

    archive.database = FailedDatabase()
    with pytest.raises(SetupError) as error:
        if action == "complete":
            archive.complete(read, list(read.records))
        else:
            getattr(archive, action)(list(read.records))
    assert "OperationFailure" in str(error.value) and "synthetic-secret" not in str(error.value)
