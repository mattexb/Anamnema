"""Real Mongo read-back and journal evidence precede raw prepared-payload retirement."""

import uuid

import pytest
from test_batch_prepare import inputs as inputs
from test_batch_prepare import window as window

from anamnema.archive_models import digest
from anamnema.archive_mongo import MongoArchive
from anamnema.batch_archive import ArchiveWindow
from anamnema.batch_journal import BatchJournal
from anamnema.batch_models import BatchPlan, batch_policy
from anamnema.batch_runner import BatchRunner
from anamnema.ingest_config import archive_settings
from anamnema.setup_config import SetupError
from anamnema.source_policy import source_policy
from anamnema.window_store import WindowStore


@pytest.fixture
def archived(inputs, window, disposable_mongo, tmp_path):
    _, client = disposable_mongo
    archive = MongoArchive(client["batch_" + uuid.uuid4().hex], archive_settings())
    path = inputs.save(window)
    operation = ArchiveWindow(
        inputs, "fixture", path.parent.name, archive, WindowStore(archive, source_policy())
    )
    plan = BatchPlan(
        instance="fixture",
        session="session-one",
        input_path=path,
        input_digest=path.parent.name,
        profile_digest=digest({"fixture": "archive"}),
        stages=("archive", "later"),
    )
    journal = BatchJournal(tmp_path / "jobs", plan, batch_policy())
    return operation, journal


def test_actual_archive_stage_journal_and_verified_retirement_leave_recoverable_window(
    archived, window
):
    operation, journal = archived
    payload = journal.plan.input_path.parent / operation.inputs.policy.payload_file
    with pytest.raises(SetupError, match="not durably recorded"):
        operation.retire_payload(journal)
    assert payload.is_file()
    loaded = []

    def later():
        loaded.append(operation.load())
        return {"verified": True}

    result = BatchRunner(journal, {"archive": operation.archive_input, "later": later}).run()
    assert result.complete and loaded == [window]
    assert operation.retire_payload(journal) and not payload.exists()
    assert not operation.retire_payload(journal)
    assert operation.load() == window and journal.plan.input_path.is_file()
    assert BatchRunner(journal, {"archive": operation.archive_input, "later": later}).run().complete
    assert loaded == [window]


def test_lost_archive_result_replays_without_duplicate_source_or_replacement(archived):
    operation, journal = archived
    with journal.worker() as execution:
        assert execution.begin() == "archive"
        first = operation.archive_input()
        assert first["inserted"] > 0
    result = BatchRunner(
        journal, {"archive": operation.archive_input, "later": lambda: {"ok": True}}
    ).run()
    assert result.stages[0].attempts[0].category == "interrupted"
    assert result.stages[0].attempts[-1].metrics["inserted"] == 0
    assert operation.load()


def test_corrupt_archived_source_prevents_local_retirement(archived):
    operation, journal = archived
    BatchRunner(journal, {"archive": operation.archive_input, "later": lambda: {"ok": True}}).run()
    payload = journal.plan.input_path.parent / operation.inputs.policy.payload_file
    before = payload.read_bytes()
    collection = operation.archive.database[operation.archive.settings.collections["conversation"]]
    collection.update_one(
        {"_id": operation.manifest.records[0]},
        {"$set": {"source": {"message": "changed synthetic source"}}},
    )
    with pytest.raises(SetupError, match="corrupt"):
        operation.retire_payload(journal)
    assert payload.read_bytes() == before


def test_missing_reference_or_foreign_source_never_becomes_an_empty_success(archived):
    operation, _ = archived
    with pytest.raises(SetupError, match="reference is absent"):
        operation.load()
    operation.archive_input()
    with pytest.raises(SetupError, match="incomplete"):
        operation.archive.restore("foreign", "conversation", (operation.manifest.records[0],))
    operation.archive.database[operation.windows.policy.windows_collection].delete_many({})
    with pytest.raises(SetupError, match="reference is absent"):
        operation.load()
