"""Synthetic source preservation and actual Mongo replay/failure proof."""

from __future__ import annotations

import hashlib
import json
import uuid
from pathlib import Path

import pytest

from anamnema.archive_models import digest
from anamnema.archive_mongo import MongoArchive, import_snapshot
from anamnema.ingest_config import archive_settings
from anamnema.setup_config import SetupError
from anamnema.substrates.base import SubstrateAdapter
from anamnema.substrates.claude import ClaudeAdapter


def source(tmp_path: Path, records: list[dict], *, blanks: bool = False) -> Path:
    path = tmp_path / "synthetic.jsonl"
    separator = "\n\n" if blanks else "\n"
    path.write_text(separator.join(json.dumps(row) for row in records) + "\n", encoding="utf-8")
    return path


def dialogue() -> list[dict]:
    return [
        {
            "type": "user",
            "uuid": "same-native-id",
            "timestamp": "2026-01-01T00:00:00Z",
            "message": {"role": "user", "content": "Keep backups for fourteen days."},
        },
        {
            "type": "assistant",
            "uuid": "same-native-id",
            "timestamp": "2026-01-01T00:00:00Z",
            "message": {
                "role": "assistant",
                "content": [
                    {"type": "tool_use", "id": "fake-tool", "input": {"text": "not human speech"}}
                ],
            },
        },
        {
            "type": "user",
            "uuid": "summary",
            "timestamp": "2026-01-01T00:00:01Z",
            "isCompactSummary": True,
            "message": {"content": "A synthetic summary"},
        },
        {
            "type": "assistant",
            "uuid": "tail",
            "timestamp": "2026-01-01T00:00:02Z",
            "message": {"role": "assistant", "content": "The last spoken record survives."},
        },
    ]


def make_archive(disposable_mongo, settings=None):
    _, client = disposable_mongo
    return MongoArchive(client["test_" + uuid.uuid4().hex], settings or archive_settings())


def test_claude_extends_shared_base_and_preserves_physical_lines_and_source(tmp_path: Path) -> None:
    records = dialogue()
    path = source(tmp_path, records, blanks=True)
    original = path.read_bytes()
    adapter = ClaudeAdapter(archive_settings())
    assert isinstance(adapter, SubstrateAdapter)
    snapshot = adapter.read(path, "synthetic-agent", "native-session", closed=True)
    assert [row.source_line for row in snapshot.records] == [1, 3, 5, 7, 7]
    assert [row.kind for row in snapshot.records] == [
        "conversation",
        "conversation",
        "summary",
        "conversation",
        "closing",
    ]
    assert [row.source for row in snapshot.records[:-1]] == records
    assert path.read_bytes() == original and snapshot.sha256 == hashlib.sha256(original).hexdigest()
    assert snapshot.records[0].identity != snapshot.records[1].identity


@pytest.mark.parametrize("invalid", ['{"secret":"synthetic-private-bait"', "[]", '{"x":NaN}'])
def test_invalid_source_fails_with_line_number_without_content(
    tmp_path: Path, invalid: str
) -> None:
    path = tmp_path / "bad.jsonl"
    path.write_text("\n\n" + invalid, encoding="utf-8")
    with pytest.raises(SetupError, match="physical line 3") as error:
        ClaudeAdapter(archive_settings()).read(path, "agent", "session")
    assert "synthetic-private-bait" not in str(error.value)


def test_missing_timestamp_noise_and_explicit_closure(tmp_path: Path) -> None:
    adapter = ClaudeAdapter(archive_settings())
    with pytest.raises(SetupError, match="timestamp"):
        adapter.read(source(tmp_path, [{"type": "user"}]), "agent", "session")
    rows = dialogue()[:3] + [{"type": "progress", "timestamp": "2026-01-01T00:00:02Z"}]
    snapshot = adapter.read(source(tmp_path, rows), "agent", "session", closed=True)
    assert snapshot.ignored == 1 and all(row.kind != "closing" for row in snapshot.records)
    snapshot = adapter.read(source(tmp_path, dialogue()), "agent", "session")
    assert not snapshot.closed and all(row.kind != "closing" for row in snapshot.records)


def test_limits_symlinks_and_missing_identity_refuse_without_source_change(tmp_path: Path) -> None:
    path = source(tmp_path, dialogue())
    small = archive_settings().model_copy(update={"max_file_bytes": 1})
    with pytest.raises(SetupError, match="limit"):
        ClaudeAdapter(small).read(path, "agent", "session")
    linked = tmp_path / "link.jsonl"
    linked.symlink_to(path)
    with pytest.raises(SetupError, match="symlink"):
        ClaudeAdapter(archive_settings()).read(linked, "agent", "session")
    with pytest.raises(SetupError, match="nonempty"):
        ClaudeAdapter(archive_settings()).read(path, "", "session")


def test_real_mongo_readback_replay_and_cross_instance_separation(
    tmp_path: Path, disposable_mongo
) -> None:
    archive = make_archive(disposable_mongo)
    path = source(tmp_path, dialogue(), blanks=True)
    original = path.read_bytes()
    snapshot = ClaudeAdapter(archive.settings).read(path, "agent", "session", closed=True)
    first = import_snapshot(snapshot, archive)
    again = import_snapshot(snapshot, archive)
    assert first["verified"] and first["inserted"] == 5
    assert again["inserted"] == 0 and again["already_present"] == 5
    assert archive.database[archive.settings.collections["receipt"]].count_documents({}) == 1
    other = ClaudeAdapter(archive.settings).read(path, "other-agent", "session", closed=True)
    assert import_snapshot(other, archive)["inserted"] == 5
    assert path.read_bytes() == original
    stored = archive.database[archive.settings.collections["conversation"]].find_one(
        {"_id": snapshot.records[1].identity}
    )
    assert stored["source"] == dialogue()[1] and stored["source_line"] == 3


def test_interrupted_batch_has_no_receipt_and_resumes(tmp_path: Path, disposable_mongo) -> None:
    settings = archive_settings().model_copy(update={"batch_size": 1})
    archive = make_archive(disposable_mongo, settings)
    snapshot = ClaudeAdapter(settings).read(source(tmp_path, dialogue()), "agent", "session")
    original = archive.write
    calls = 0

    def interrupted(records):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise SetupError("simulated interruption")
        return original(records)

    archive.write = interrupted
    with pytest.raises(SetupError, match="interruption"):
        import_snapshot(snapshot, archive)
    assert archive.database[settings.collections["receipt"]].count_documents({}) == 0
    archive.write = original
    report = import_snapshot(snapshot, archive)
    assert report["inserted"] == 3 and report["already_present"] == 1


def test_corrupt_raw_with_unchanged_digest_fails_and_is_not_overwritten(
    tmp_path: Path, disposable_mongo
) -> None:
    archive = make_archive(disposable_mongo)
    snapshot = ClaudeAdapter(archive.settings).read(
        source(tmp_path, dialogue()), "agent", "session"
    )
    archive.write(list(snapshot.records))
    collection = archive.database[archive.settings.collections["conversation"]]
    key = snapshot.records[0].identity
    collection.update_one({"_id": key}, {"$set": {"source.message.content": "corrupt"}})
    with pytest.raises(SetupError, match="source differs"):
        import_snapshot(snapshot, archive)
    assert collection.find_one({"_id": key})["source"]["message"]["content"] == "corrupt"
    assert archive.database[archive.settings.collections["receipt"]].count_documents({}) == 0


def test_lossless_compression_and_exact_repeats(tmp_path: Path, disposable_mongo) -> None:
    rows = dialogue()
    rows[0]["message"]["content"] = "synthetic-long-source " * 1000
    rows.insert(1, rows[0])
    settings = archive_settings().model_copy(update={"max_document_bytes": 3000})
    archive = make_archive(disposable_mongo, settings)
    snapshot = ClaudeAdapter(settings).read(source(tmp_path, rows), "agent", "session")
    report = import_snapshot(snapshot, archive)
    assert report["inserted"] == 4 and report["exact_source_repeats"] == 1
    stored = archive.database[settings.collections["conversation"]].find_one(
        {"_id": snapshot.records[0].identity}
    )
    assert "source_lzma" in stored and stored["source_line"] == 1
    archive.verify(list({row.identity: row for row in snapshot.records}.values()))
    assert digest(rows[0]) == stored["payload_digest"]


def test_source_identity_ignores_file_path_and_blank_line_location(tmp_path: Path) -> None:
    adapter = ClaudeAdapter(archive_settings())
    first = adapter.read(source(tmp_path, dialogue()), "agent", "session")
    second = adapter.read(source(tmp_path, dialogue(), blanks=True), "agent", "session")
    assert [row.identity for row in first.records] == [row.identity for row in second.records]
    assert first.sha256 != second.sha256
