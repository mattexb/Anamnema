"""Read-only preparation invariants, without a model or private infrastructure."""

import dataclasses
import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest

from anamnema import archive_access
from anamnema.archive_models import Record
from anamnema.archive_mongo import MongoArchive, import_snapshot
from anamnema.cli import main
from anamnema.ingest_config import archive_settings
from anamnema.processing import prepare
from anamnema.setup_config import SetupError
from anamnema.substrates.claude import ClaudeAdapter


def capture(tmp_path: Path, rows: list[dict], *, closed: bool = False):
    path = tmp_path / "source.jsonl"
    path.write_text("\n\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")
    return ClaudeAdapter(archive_settings()).read(path, "assistant", "session", closed=closed)


def conversation(text: str, **fields) -> dict:
    return {
        "type": "user",
        "timestamp": "2026-01-01T00:00:00Z",
        "uuid": "reused-native-id",
        "message": {"content": [{"type": "text", "text": text}]},
        **fields,
    }


def summary() -> dict:
    return conversation("Summary is not dialogue", isCompactSummary=True)


def test_equal_timestamps_follow_source_order_and_keep_the_final_spoken_row(tmp_path: Path):
    snapshot = capture(
        tmp_path,
        [conversation("first"), summary(), conversation("second"), conversation("third")],
        closed=True,
    )
    result = prepare(snapshot)
    assert len(result.windows) == 2
    first, last = result.windows
    assert first.boundary.kind == "summary"
    assert last.boundary.kind == "closing"
    assert [row.source_line for row in first.records] == [1]
    assert [row.source_line for row in last.records] == [5, 7]
    assert last.records[-1].source == conversation("third")
    assert not result.pending
    assert len({row.identity for window in result.windows for row in window.records}) == 3


def test_open_tail_is_not_processed_and_needs_no_inferred_closure(tmp_path: Path):
    snapshot = capture(tmp_path, [conversation("before"), summary(), conversation("still open")])
    result = prepare(snapshot)
    assert len(result.windows) == 1
    assert len(result.pending) == 1
    assert result.pending[0].source == conversation("still open")
    assert result.report(snapshot)["pending_records"] == 1


def test_no_summary_and_no_explicit_close_means_no_closed_stretch(tmp_path: Path):
    result = prepare(capture(tmp_path, [conversation("open")]))
    assert result.windows == ()
    assert len(result.pending) == 1


def test_exact_repeats_count_once_but_native_uuid_variants_survive(tmp_path: Path):
    snapshot = capture(
        tmp_path,
        [conversation("first"), conversation("first"), conversation("different")],
        closed=True,
    )
    result = prepare(snapshot)
    assert result.repeats == 1
    assert [row.source_line for row in result.windows[0].records] == [1, 5]
    assert len(result.windows[0].records) == 2


@pytest.mark.parametrize("field", ["instance", "substrate", "session"])
def test_mixed_ownership_fails_instead_of_combining_records(tmp_path: Path, field: str):
    snapshot = capture(tmp_path, [conversation("one"), conversation("two")], closed=True)
    rows = list(snapshot.records)
    rows[1] = dataclasses.replace(rows[1], **{field: "another"})
    with pytest.raises(SetupError, match="one instance"):
        prepare(dataclasses.replace(snapshot, records=tuple(rows)))


@pytest.mark.parametrize("field", ["after", "at"])
def test_invalid_closing_reference_is_not_accepted(tmp_path: Path, field: str):
    snapshot = capture(tmp_path, [conversation("one")], closed=True)
    last = snapshot.records[-1]
    bad = dataclasses.replace(last, source={**last.source, field: "wrong"})
    with pytest.raises(SetupError, match="preceding conversation"):
        prepare(dataclasses.replace(snapshot, records=(*snapshot.records[:-1], bad)))


def test_a_boundary_resets_the_preceding_row_reference(tmp_path: Path):
    snapshot = capture(tmp_path, [conversation("one"), summary()])
    original = snapshot.records[0]
    closing = Record(
        original.instance,
        original.substrate,
        original.session,
        "closing",
        original.timestamp,
        original.source_line,
        {"after": original.identity, "at": original.timestamp},
    )
    with pytest.raises(SetupError, match="preceding conversation"):
        prepare(dataclasses.replace(snapshot, records=(*snapshot.records, closing)))


def test_empty_and_summary_only_sources_are_explicit(tmp_path: Path):
    empty = capture(tmp_path, [])
    assert prepare(empty).report(empty)["closed_stretches"] == []
    only = capture(tmp_path, [summary()], closed=True)
    result = prepare(only)
    assert len(result.windows) == 1
    assert result.windows[0].records == ()
    assert result.windows[0].boundary.kind == "summary"


def test_report_contains_provenance_not_tool_content_or_human_speech_claims(tmp_path: Path):
    raw = conversation("unique-private-words")
    raw["message"]["content"].append({"type": "tool_result", "content": "tool-only-words"})
    snapshot = capture(tmp_path, [raw], closed=True)
    before = snapshot.path.read_bytes()
    result = prepare(snapshot)
    report = result.report(snapshot)
    serialized = json.dumps(report)
    assert "unique-private-words" not in serialized
    assert "tool-only-words" not in serialized
    assert report["archive_verified"] is False
    assert report["notes_generated"] is False
    assert result.windows[0].records[0].source == raw
    assert snapshot.path.read_bytes() == before
    assert report["source_sha256"] == hashlib.sha256(before).hexdigest()


def test_window_identity_changes_when_content_changes(tmp_path: Path):
    first = capture(tmp_path, [conversation("one")], closed=True)
    second = capture(tmp_path, [conversation("two")], closed=True)
    assert prepare(first).windows[0].report()["key"] != prepare(second).windows[0].report()["key"]


def test_process_default_is_read_only_and_apply_fails_before_any_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture,
):
    def forbidden(*args, **kwargs):
        pytest.fail("processing preview must not access infrastructure")

    monkeypatch.setattr(archive_access, "keyring_uri", forbidden)
    monkeypatch.setattr(archive_access, "connect", forbidden)
    snapshot = capture(tmp_path, [conversation("one")], closed=True)
    original = snapshot.path.read_bytes()
    args = ["process", "--file", str(snapshot.path), "--instance", "a", "--session", "s"]
    assert main([*args, "--closed"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["mode"] == "dry-run"
    assert len(report["closed_stretches"]) == 1
    assert snapshot.path.read_bytes() == original
    assert main([*args, "--apply", "--archive-config", str(tmp_path / "missing.json")]) == 2
    assert "requires --model-file" in capsys.readouterr().err
    assert snapshot.path.read_bytes() == original


def test_malformed_source_reports_failure_without_partial_success(tmp_path: Path, capsys):
    path = tmp_path / "bad.jsonl"
    path.write_text("\nnot json\n", encoding="utf-8")
    assert main(["process", "--file", str(path), "--instance", "a", "--session", "s"]) == 2
    report = capsys.readouterr()
    assert report.out == ""
    assert "physical line 2" in report.err
    assert path.read_text(encoding="utf-8") == "\nnot json\n"


def test_imported_and_verified_sources_keep_their_window_references(
    tmp_path: Path, disposable_mongo
):
    _, client = disposable_mongo
    settings = archive_settings()
    snapshot = capture(
        tmp_path, [conversation("before"), summary(), conversation("after")], closed=True
    )
    archive = MongoArchive(client["preparation_sources"], settings)
    imported = import_snapshot(snapshot, archive)
    assert imported["verified"] is True
    prepared = prepare(snapshot)
    references = [row for window in prepared.windows for row in window.records]
    archive.verify(references)
    collection = archive.database[settings.collections["conversation"]]
    assert [collection.find_one({"_id": row.identity})["source_line"] for row in references] == [
        1,
        5,
    ]
    assert collection.count_documents({}) == 2
    assert import_snapshot(snapshot, archive)["inserted"] == 0


def test_installed_process_preview_outside_checkout_keeps_files_unchanged(tmp_path: Path):
    snapshot = capture(tmp_path, [conversation("local-only-text")], closed=True)
    before = snapshot.path.read_bytes()
    command = [
        sys.executable,
        "-I",
        "-m",
        "anamnema",
        "process",
        "--file",
        str(snapshot.path),
        "--instance",
        "assistant",
        "--session",
        "session",
        "--closed",
    ]
    result = subprocess.run(  # noqa: S603 - own installed interpreter and synthetic file
        command, cwd=tmp_path, capture_output=True, text=True, encoding="utf-8", check=False
    )
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert len(report["closed_stretches"]) == 1
    assert report["archive_verified"] is False
    assert report["notes_generated"] is False
    assert "local-only-text" not in result.stdout
    rejected = subprocess.run(  # noqa: S603 - same own command, reserved operation
        [*command, "--apply", "--archive-config", str(tmp_path / "missing.json")],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    assert rejected.returncode == 2
    assert rejected.stdout == ""
    assert "requires --model-file" in rejected.stderr
    assert snapshot.path.read_bytes() == before
