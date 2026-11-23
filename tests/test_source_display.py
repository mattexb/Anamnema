"""Real source/window persistence, captured order, attribution and bounded context."""

import uuid
from dataclasses import replace

import pytest

from anamnema.archive_models import Record, Snapshot, canonical
from anamnema.archive_mongo import MongoArchive, import_snapshot
from anamnema.ingest_config import archive_settings
from anamnema.memory_sources import SourceLookup
from anamnema.processing import Window
from anamnema.setup_config import SetupError
from anamnema.source_display import SourceDisplay, nearby
from anamnema.source_policy import source_policy
from anamnema.speech_policy import speech_policy
from anamnema.substrates.claude_speech import ClaudeSpeechReader
from anamnema.window_store import WindowStore


@pytest.fixture
def context_archive(disposable_mongo, tmp_path):
    _, client = disposable_mongo
    instance = "source-" + uuid.uuid4().hex
    archive = MongoArchive(client["context_" + uuid.uuid4().hex], archive_settings())
    specifications = [
        ("user", "Owner opening context."),
        ("assistant", "Agent first answer."),
        ("assistant", "Agent second part."),
        ("user", "Owner question requiring a decision."),
        ("user", "Hidden meta does not become owner speech."),
        ("assistant", [{"type": "thinking", "thinking": "Not spoken dialogue."}]),
        ("assistant", [{"type": "tool_use", "name": "fixture", "input": {}}]),
        ("user", [{"type": "tool_result", "content": "Not a human turn."}]),
        ("assistant", "Agent cited response with the actual decision."),
        ("user", "Owner response."),
        ("user", "Owner additional response in the same turn."),
        ("assistant", "Agent follow-up."),
        ("user", "Owner final statement."),
        ("assistant", "Agent extra response."),
        ("user", "Owner final closure."),
    ]
    records = tuple(
        Record(
            instance,
            "claude",
            "same-time-session",
            "conversation",
            "2026-11-20T08:00:00Z",
            index + 1,
            {
                "type": role,
                "timestamp": "2026-11-20T08:00:00Z",
                "message": text,
                "uuid": f"native-{index}",
                **({"isMeta": True} if index == 4 else {}),
            },
        )
        for index, (role, text) in enumerate(specifications)
    )
    previous = tuple(
        replace(row, source_line=index + 1) for index, row in enumerate(reversed(records))
    )
    import_snapshot(
        Snapshot(tmp_path / "previous-capture.jsonl", "previous", previous, 0, 0, False), archive
    )
    boundary = Record(
        instance,
        "claude",
        "same-time-session",
        "closing",
        records[-1].timestamp,
        len(records),
        {"after": records[-1].identity, "at": records[-1].timestamp},
    )
    window = Window(boundary, records)
    import_snapshot(
        Snapshot(tmp_path / "synthetic.jsonl", "fixture", (*records, boundary), 0, 0, True), archive
    )
    windows = WindowStore(archive, source_policy().model_copy(update={"source_turns": 1}))
    windows.save(window)
    lookup = SourceLookup(archive, 512, 8388608)
    display = SourceDisplay(windows, ClaudeSpeechReader(speech_policy()))
    note = {
        "instance": instance,
        "session": boundary.session,
        "substrate": boundary.substrate,
        "window": window.report()["key"],
    }
    return archive, window, lookup, display, note


def show(context_archive, cited=(8,)):
    _, window, lookup, display, note = context_archive
    return display.read(
        window.boundary.instance,
        note,
        tuple(window.records[index] for index in cited),
        lookup.records,
    )


def test_context_uses_captured_order_and_speaker_stretches(context_archive):
    archive, window, _, display, note = context_archive
    result = show(context_archive)
    assert result["context"]["status"] == "available"
    assert [row["record"] for row in result["rows"]] == [
        window.records[index].identity for index in (3, 8, 9, 10)
    ]
    assert [row["speaker"] for row in result["rows"]] == ["owner", "agent", "owner", "owner"]
    assert sum(row["cited"] for row in result["rows"]) == 1
    assert all(row["at"] == window.records[0].timestamp for row in result["rows"])
    before = canonical(archive.database[display.policy.windows_collection].find_one())
    display.windows.save(window)
    assert canonical(archive.database[display.policy.windows_collection].find_one()) == before
    archive.verify([*window.records, window.boundary])
    assert (
        display.windows.load("another-owner", note["session"], note["substrate"], note["window"])
        is None
    )


def test_citation_wins_small_budget_and_original_is_not_truncated(context_archive):
    archive, window, lookup, display, note = context_archive
    bounded = SourceDisplay(
        WindowStore(archive, display.policy.model_copy(update={"source_tokens": 2})), display.reader
    )
    result = bounded.read(window.boundary.instance, note, (window.records[8],), lookup.records)
    assert len(result["rows"]) == 1
    row = result["rows"][0]
    assert row["cited"] and row["clipped"]
    assert row["text"] == "Agent ci"
    assert result["context"]["budget_reached"]
    assert result["context"]["status"] == "partial"
    reopened = lookup.records(window.boundary.instance, (window.records[8].identity,))[0]
    assert reopened.source == window.records[8].source
    assert reopened.identity == window.records[8].identity
    assert reopened.source_line != window.records[8].source_line


def test_missing_manifest_is_explicit_without_guessing_or_writes(context_archive):
    archive, _, _, display, _ = context_archive
    collection = archive.database[display.policy.windows_collection]
    collection.delete_many({})
    result = show(context_archive)
    assert result["rows"] == []
    assert result["context"]["status"] == "unavailable"
    assert "missing" in result["context"]["reason"]
    assert collection.count_documents({}) == 0


def test_corrupt_manifest_order_is_rejected_not_repaired(context_archive):
    archive, window, _, display, _ = context_archive
    collection = archive.database[display.policy.windows_collection]
    collection.update_one(
        {}, {"$set": {"records": [record.identity for record in reversed(window.records)]}}
    )
    before = canonical(collection.find_one())
    with pytest.raises(SetupError, match="corrupt"):
        show(context_archive)
    assert canonical(collection.find_one()) == before


def test_window_requires_originals_and_single_scope(context_archive):
    archive, window, _, display, _ = context_archive
    with pytest.raises(SetupError, match="ownership"):
        display.windows.save(
            replace(window, records=(replace(window.records[0], instance="other"),))
        )
    absent = replace(window.records[0], source={"type": "user", "message": "Not archived."})
    with pytest.raises(SetupError, match="closing marker"):
        display.windows.save(Window(window.boundary, (absent,)))
    bounded = WindowStore(archive, display.policy.model_copy(update={"max_window_records": 1}))
    with pytest.raises(SetupError, match="record bound"):
        bounded.save(window)
    with pytest.raises(SetupError, match="differ"):
        WindowStore(
            archive,
            display.policy.model_copy(
                update={"windows_collection": archive.settings.collections["conversation"]}
            ),
        )


def test_filtered_citation_is_not_invented_human_speech(context_archive):
    result = show(context_archive, (4,))
    assert result["rows"] == []
    assert result["context"]["status"] == "partial"
    assert len(result["context"]["untranslated_citations"]) == 1


def test_scan_bound_and_disjoint_spans_are_explicit(context_archive):
    archive, window, lookup, display, note = context_archive
    bounded = SourceDisplay(
        WindowStore(archive, display.policy.model_copy(update={"context_records": 3})),
        display.reader,
    )
    result = bounded.read(window.boundary.instance, note, (window.records[8],), lookup.records)
    assert result["context"]["scan_limited"]
    assert result["context"]["status"] == "partial"
    selected = nearby(1000, [2, 900], 6)
    assert len(selected) == 6 and 2 in selected and 900 in selected
    assert not any(10 < index < 890 for index in selected)


def test_manifest_limits_and_altered_source_fail_without_repair(context_archive):
    archive, window, _, display, _ = context_archive
    bounded = WindowStore(archive, display.policy.model_copy(update={"max_manifest_bytes": 1}))
    with pytest.raises(SetupError, match="byte bound"):
        bounded.save(window)
    collection = archive.database[archive.settings.collections["conversation"]]
    collection.update_one(
        {"_id": window.records[8].identity}, {"$set": {"source.message": "Altered."}}
    )
    with pytest.raises(SetupError, match="corrupt"):
        show(context_archive)
    with pytest.raises(SetupError, match="source differs"):
        display.windows.save(window)


def test_no_guess_for_missing_window_foreign_citation_or_unsupported_substrate(context_archive):
    archive, window, lookup, display, note = context_archive
    cited = (window.records[8],)
    assert (
        display.read(window.boundary.instance, {**note, "window": None}, cited, lookup.records)[
            "context"
        ]["status"]
        == "unavailable"
    )
    assert (
        display.read(
            window.boundary.instance, {**note, "substrate": "unsupported"}, cited, lookup.records
        )["context"]["status"]
        == "unavailable"
    )
    elsewhere = replace(window.records[8], session="another-session")
    archive.write([elsewhere])
    result = display.read(window.boundary.instance, note, (elsewhere,), lookup.records)
    assert result["context"]["status"] == "unavailable"
    assert "outside" in result["context"]["reason"]


def test_reference_lookup_uses_primary_key(context_archive):
    archive, window, _, display, _ = context_archive
    plan = (
        archive.database[display.policy.windows_collection]
        .find(
            {
                "_id": window.report()["key"],
                "instance": window.boundary.instance,
                "session": window.boundary.session,
                "substrate": window.boundary.substrate,
            }
        )
        .explain()["queryPlanner"]["winningPlan"]
    )
    assert "COLLSCAN" not in str(plan)


def test_shared_speech_read_keeps_original_numbers(context_archive):
    _, window, _, display, _ = context_archive
    expected = display.reader.read(window)
    actual = display.reader.read_records(enumerate(window.records))
    assert actual == expected
    selected = display.reader.read_records([(8, window.records[8]), (10, window.records[10])])
    assert [row.n for row in selected.rows] == [8, 10]
