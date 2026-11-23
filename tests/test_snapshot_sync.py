"""Real Mongo read-back, local spool safety, and nonblocking session-lock boundaries."""

import hashlib
import io
import json
import os
import subprocess
import sys

import pytest
from pymongo.errors import OperationFailure
from test_continuity import continuity as continuity
from test_continuity import settled, snapshot_text
from test_ingestion import make_archive

from anamnema.cli import main
from anamnema.continuity import Continuity
from anamnema.continuity_state import StateFiles
from anamnema.setup_config import SetupError, load_config
from anamnema.snapshot_archive import MongoSnapshots, snapshot_archive_policy
from anamnema.snapshot_sync import SnapshotSync


@pytest.fixture
def snapshots(disposable_mongo):
    return MongoSnapshots(make_archive(disposable_mongo), snapshot_archive_policy())


def queued(core, text="First state"):
    files = settled(core)
    snapshot = core.stage("session-one", snapshot_text(core, body=text))
    core.start("session-one", "compact")
    source = files.folder / snapshot.blob
    written = 1_700_000_000_000_000_123
    os.utime(source, ns=(written, written))
    core.stop("session-one", core.snapshot_ack("session-one", snapshot))
    target = files.folder / core.policy.spool_directory / snapshot.blob
    assert target.stat().st_mtime_ns == written
    return target


def sync_for(core, policy=None):
    return SnapshotSync(
        core.config, core.policy, "session-one", policy or snapshot_archive_policy()
    )


def test_exact_archive_readback_frees_quota_and_is_idempotent(continuity, snapshots):
    core = Continuity(
        continuity.config, continuity.policy.model_copy(update={"max_spool_items": 1})
    )
    target = queued(core)
    data = target.read_bytes()
    sync = sync_for(core)
    assert sync.preview()["spooled"] == 1 and target.read_bytes() == data
    report = sync.apply(snapshots)
    assert report["verified"] == report["inserted"] == report["removed"] == 1
    assert not target.exists()
    collection = snapshots.archive.database[snapshots.policy.collection]
    document = collection.find_one({})
    assert document["text"].encode("utf-8") == data and document["instance"] == core.config.instance
    assert document["session"] == "session-one" and document["pair_status"] == "pending"
    assert document["written_at"].startswith("2023-11-14T22:13:20")
    assert sync.apply(snapshots)["removed"] == 0 and collection.count_documents({}) == 1
    assert core.stage("session-one", snapshot_text(core, body="Second state"))


def test_mongo_work_holds_no_live_session_lock(continuity, snapshots, monkeypatch):
    core = continuity
    queued(core)
    original = snapshots.keep

    def independent_hook(document):
        assert core.stop("session-one", "a simultaneous normal Stop") == ""
        return original(document)

    monkeypatch.setattr(snapshots, "keep", independent_hook)
    assert sync_for(core).apply(snapshots)["removed"] == 1


@pytest.mark.parametrize(
    "field,value",
    [("text", "corrupt"), ("instance", "other"), ("session", "foreign"), ("sha256", "forged")],
)
def test_corrupt_archive_with_cached_hash_never_deletes_or_overwrites_spool(
    continuity, snapshots, field, value
):
    target = queued(continuity)
    data = target.read_bytes()
    document = snapshots.document(
        continuity.config.instance, "session-one", data.decode(), "2023-11-14T22:13:20+00:00"
    )
    snapshots.keep(document)
    collection = snapshots.archive.database[snapshots.policy.collection]
    collection.update_one({"_id": document["_id"]}, {"$set": {field: value}})
    with pytest.raises(SetupError, match="differs"):
        sync_for(continuity).apply(snapshots)
    assert target.read_bytes() == data and collection.find_one({})[field] == value


def test_interruption_after_verification_retains_copy_and_retry_deduplicates(
    continuity, snapshots, monkeypatch
):
    target = queued(continuity)
    original = StateFiles.remove_spooled

    def stopped(*_):
        raise RuntimeError("fixture stopped after verified archive read")

    monkeypatch.setattr(StateFiles, "remove_spooled", stopped)
    with pytest.raises(RuntimeError, match="after verified"):
        sync_for(continuity).apply(snapshots)
    assert (
        target.is_file()
        and snapshots.archive.database[snapshots.policy.collection].count_documents({}) == 1
    )
    monkeypatch.setattr(StateFiles, "remove_spooled", original)
    report = sync_for(continuity).apply(snapshots)
    assert report["inserted"] == 0 and report["verified"] == report["removed"] == 1
    assert snapshots.archive.database[snapshots.policy.collection].count_documents({}) == 1


def test_concurrently_changed_spool_is_retained_after_archive_verify(
    continuity, snapshots, monkeypatch
):
    target = queued(continuity)
    data = target.read_bytes()
    verify = snapshots.verify

    def changed(*args):
        verify(*args)
        target.write_bytes(b"changed after verification")

    monkeypatch.setattr(snapshots, "verify", changed)
    with pytest.raises(SetupError, match="changed"):
        sync_for(continuity).apply(snapshots)
    assert target.read_bytes() == b"changed after verification"
    assert (
        snapshots.archive.database[snapshots.policy.collection].find_one({})["text"].encode()
        == data
    )


@pytest.mark.parametrize("action", ["write", "read", "schema"])
def test_driver_outage_redacts_details_and_preserves_spool(continuity, snapshots, action):
    target = queued(continuity)
    data = target.read_bytes()
    database = snapshots.archive.database

    class FailedCollection:
        def __init__(self, name):
            self.name = name

        def create_index(self, *args, **kwargs):
            if action == "schema":
                raise OperationFailure("synthetic-secret-never-print")
            return database[self.name].create_index(*args, **kwargs)

        def update_one(self, *args, **kwargs):
            if action == "write":
                raise OperationFailure("synthetic-secret-never-print")
            return database[snapshots.policy.collection].update_one(*args, **kwargs)

        def find_one(self, *args, **kwargs):
            raise OperationFailure("synthetic-secret-never-print")

    class FailedDatabase:
        def __getitem__(self, name):
            return FailedCollection(name)

    snapshots.archive.database = FailedDatabase()
    with pytest.raises(SetupError) as error:
        sync_for(continuity).apply(snapshots)
    assert "OperationFailure" in str(error.value) and "synthetic-secret" not in str(error.value)
    assert target.read_bytes() == data


def test_batch_limit_leaves_later_snapshot_retryable(continuity, snapshots):
    queued(continuity, "First state")
    queued(continuity, "Second state")
    policy = snapshots.policy.model_copy(update={"batch_size": 1})
    archive = MongoSnapshots(snapshots.archive, policy)
    sync = sync_for(continuity, policy)
    assert sync.preview()["more"] is True
    assert sync.apply(archive)["removed"] == 1
    assert sync.preview()["spooled"] == 1
    assert sync.apply(archive)["removed"] == 1
    assert sync.preview()["spooled"] == 0


def test_foreign_header_and_corrupt_local_body_are_not_uploaded(continuity, snapshots):
    target = queued(continuity)
    target.write_bytes(b"corrupt local body")
    report = sync_for(continuity).apply(snapshots)
    assert report["refused"] == [{"file": target.name, "reason": "invalid_spool"}]
    assert (
        target.exists()
        and snapshots.archive.database[snapshots.policy.collection].count_documents({}) == 0
    )


def test_hash_valid_foreign_header_is_not_uploaded_under_the_selected_owner(continuity, snapshots):
    target = queued(continuity)
    data = target.read_bytes().replace(b"Instance-ID: example", b"Instance-ID: foreign")
    foreign = target.parent / (hashlib.sha256(data).hexdigest() + continuity.policy.blob_suffix)
    target.unlink()
    foreign.write_bytes(data)
    foreign.chmod(0o600)
    report = sync_for(continuity).apply(snapshots)
    assert report["refused"] == [{"file": foreign.name, "reason": "invalid_spool"}]
    assert (
        foreign.is_file()
        and snapshots.archive.database[snapshots.policy.collection].count_documents({}) == 0
    )


def test_poison_first_file_does_not_starve_a_one_item_upload_batch(continuity, snapshots):
    target = queued(continuity)
    poison = target.parent / ("0" * 64 + continuity.policy.blob_suffix)
    poison.write_bytes(b"corrupt")
    poison.chmod(0o600)
    assert poison.name < target.name
    policy = snapshots.policy.model_copy(update={"batch_size": 1})
    archive = MongoSnapshots(snapshots.archive, policy)
    sync = sync_for(continuity, policy)
    report = sync.apply(archive)
    assert report["removed"] == report["verified"] == 1
    assert report["refused"] == [{"file": poison.name, "reason": "invalid_spool"}]
    assert poison.read_bytes() == b"corrupt" and not target.exists()
    retry = sync.apply(archive)
    assert retry["removed"] == 0 and retry["refused"] == report["refused"]


def test_hash_valid_invalid_utf8_is_refused_without_upload(continuity, snapshots):
    target = queued(continuity)
    data = b"\xff\xfe"
    poison = target.parent / (hashlib.sha256(data).hexdigest() + continuity.policy.blob_suffix)
    target.unlink()
    poison.write_bytes(data)
    poison.chmod(0o600)
    report = sync_for(continuity).apply(snapshots)
    assert report["refused"] == [{"file": poison.name, "reason": "invalid_spool"}]
    assert poison.read_bytes() == data and report["inserted"] == 0


def test_read_only_preview_does_not_clean_orphans_or_migrate_journals(continuity):
    target = queued(continuity)
    files = StateFiles(continuity.config, continuity.policy, "session-one")
    with files.locked():
        orphan = files.blob("unreferenced synthetic content")
        journal = files.folder / continuity.policy.state_file
        before = journal.read_bytes()
    assert sync_for(continuity).preview()["spooled"] == 1
    assert (files.folder / orphan).exists() and journal.read_bytes() == before and target.is_file()


def test_document_bound_fails_before_mongo_write_and_leaves_spool(continuity, snapshots):
    target = queued(continuity)
    snapshots.archive.settings = snapshots.archive.settings.model_copy(
        update={"max_document_bytes": 1}
    )
    with pytest.raises(SetupError, match="document bound"):
        sync_for(continuity).apply(snapshots)
    assert (
        target.exists()
        and snapshots.archive.database[snapshots.policy.collection].count_documents({}) == 0
    )


def test_dry_run_unknown_session_has_no_files_secrets_or_network(tmp_path, capsys, monkeypatch):
    config = tmp_path / "config.json"
    config.write_text(
        json.dumps(
            {
                "instance": "example",
                "state_root": str(tmp_path / "not-created"),
                "identity_files": [str(tmp_path / "identity")],
            }
        ),
        encoding="utf-8",
    )
    before = set(tmp_path.rglob("*"))
    monkeypatch.setattr("sys.stdin", io.StringIO("not hook JSON"))
    assert (
        main(
            [
                "continuity",
                "sync",
                "--session",
                "session",
                "--continuity-config",
                str(config),
                "--config",
                str(tmp_path / "missing-setup"),
            ]
        )
        == 0
    )
    result = json.loads(capsys.readouterr().out)
    assert result["mode"] == "dry-run" and result["spooled"] == 0
    assert set(tmp_path.rglob("*")) == before


def test_snapshot_policy_cannot_reuse_native_collection(snapshots):
    policy = snapshots.policy.model_copy(
        update={"collection": snapshots.archive.settings.collections["summary"]}
    )
    with pytest.raises(SetupError, match="must differ"):
        MongoSnapshots(snapshots.archive, policy)


def test_actual_cli_apply_uses_only_fixture_keyring_and_local_mongo(
    continuity, snapshots, tmp_path, capsys
):
    target = queued(continuity)
    client = snapshots.archive.database.client
    fixture = tmp_path / "fixture-keyring"
    uri = f"mongodb://127.0.0.1:{client.address[1]}"
    fixture.write_text(
        f"#!{sys.executable}\nimport sys\nsys.stdout.write({uri!r})\n", encoding="utf-8"
    )
    fixture.chmod(0o700)
    setup = load_config().model_dump(mode="json")
    setup["keyring"]["executable"] = str(fixture)
    setup["services"]["archive"]["port"] = client.address[1]
    setup_file = tmp_path / "setup.json"
    setup_file.write_text(json.dumps(setup), encoding="utf-8")
    config = tmp_path / "continuity.json"
    config.write_text(continuity.config.model_dump_json(), encoding="utf-8")
    archive = tmp_path / "archive.json"
    settings = snapshots.archive.settings.model_copy(
        update={"database": snapshots.archive.database.name}
    )
    archive.write_text(settings.model_dump_json(), encoding="utf-8")
    command = [
        sys.executable,
        "-m",
        "anamnema",
        "continuity",
        "sync",
        "--session",
        "session-one",
        "--continuity-config",
        str(config),
        "--archive-config",
        str(archive),
        "--config",
        str(setup_file),
        "--apply",
    ]
    result = subprocess.run(  # noqa: S603 - own Python entry point and synthetic fixture config
        command,
        cwd=tmp_path,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=15,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["removed"] == 1 and not target.exists()
    assert snapshots.archive.database[snapshots.policy.collection].count_documents({}) == 1
