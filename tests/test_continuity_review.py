"""Peer-review regressions: Stop gates actual obligations, never inferred initialization debt."""

import pytest
from test_continuity import ack, settled, snapshot_text, state
from test_continuity import continuity as continuity

from anamnema.continuity import Continuity
from anamnema.continuity_state import StateFiles
from anamnema.setup_config import SetupError


def test_missing_snapshot_warns_without_creating_stop_debt(continuity):
    core = continuity
    settled(core)
    context = core.start("session-one", "compact")
    assert "snapshot" in context.lower() and "unavailable" in context.lower()
    assert state(core)[1].failure is None
    assert core.stop("session-one", "continue normally") == ""


def test_new_idle_precompute_replaces_only_the_unapplied_snapshot(continuity):
    core = continuity
    files = settled(core)
    old = core.stage("session-one", snapshot_text(core, body="Earlier state."))
    new = core.stage("session-one", snapshot_text(core, body="Newer state after more turns."))
    assert new.digest != old.digest and state(core)[1].snapshot == new
    assert not (files.folder / old.blob).exists()
    context = core.start("session-one", "compact")
    assert new.digest in context and old.digest not in context
    assert core.stop("session-one", core.snapshot_ack("session-one", new)) == ""


def test_live_registration_stop_without_start_has_no_debt(continuity):
    core = continuity
    assert core.stop("session-one", "already running session") == ""
    assert state(core)[1].initialized is False
    core.start("session-one", "resume")
    _, held = state(core)
    assert held.reads and core.stop("session-one", "no ack yet")
    assert core.stop("session-one", ack(core, held)) == ""


def test_first_compact_start_also_produces_an_identity_read(continuity):
    core = continuity
    snapshot = core.stage("session-one", snapshot_text(core))
    context = core.start("session-one", "compact")
    _, held = state(core)
    assert held.initialized and len(held.reads) == 1
    assert core.policy.read_marker in context and snapshot.digest in context
    assert core.stop("session-one", core.snapshot_ack("session-one", snapshot))
    assert core.stop("session-one", ack(core, held)) == ""


def test_full_spool_refuses_new_staging_not_stop_and_journal_stays_bounded(continuity):
    core = Continuity(
        continuity.config,
        continuity.policy.model_copy(update={"max_spool_items": 2, "max_state_bytes": 600}),
    )
    files = settled(core)
    for index in range(2):
        item = core.stage("session-one", snapshot_text(core, body=f"Window {index}"))
        core.start("session-one", "compact")
        assert core.stop("session-one", core.snapshot_ack("session-one", item)) == ""
    assert (files.folder / core.policy.state_file).stat().st_size < core.policy.max_state_bytes
    assert len(list((files.folder / core.policy.spool_directory).iterdir())) == 2
    with pytest.raises(SetupError, match="spool is full"):
        core.stage("session-one", snapshot_text(core, body="Third window"))
    assert core.stop("session-one", "done") == ""
    assert "unavailable" in core.start("session-one", "compact")
    assert core.stop("session-one", "done") == ""


def test_ack_spool_publication_then_failed_journal_clear_is_retryable(continuity, monkeypatch):
    core = continuity
    files = settled(core)
    snapshot = core.stage("session-one", snapshot_text(core))
    core.start("session-one", "compact")
    before = state(core)[1]
    original = StateFiles.commit

    def stop(*_):
        raise OSError("fixture interrupted after spool publication")

    monkeypatch.setattr(StateFiles, "commit", stop)
    with pytest.raises(SetupError):
        core.stop("session-one", core.snapshot_ack("session-one", snapshot))
    assert state(core)[1] == before
    assert (files.folder / snapshot.blob).exists()
    target = files.folder / core.policy.spool_directory / snapshot.blob
    assert target.is_file()
    monkeypatch.setattr(StateFiles, "commit", original)
    assert core.stop("session-one", core.snapshot_ack("session-one", snapshot)) == ""
    assert not (files.folder / snapshot.blob).exists() and target.is_file()
    assert len(list(target.parent.iterdir())) == 1


def test_stop_does_not_reread_archived_spool_blobs(continuity, monkeypatch):
    core = continuity
    settled(core)
    snapshot = core.stage("session-one", snapshot_text(core))
    core.start("session-one", "compact")
    core.stop("session-one", core.snapshot_ack("session-one", snapshot))
    original = StateFiles._read_in

    def no_spool_read(self, folder, *args):
        assert folder.name != core.policy.spool_directory
        return original(self, folder, *args)

    monkeypatch.setattr(StateFiles, "_read_in", no_spool_read)
    assert core.stop("session-one", "normal later turn") == ""


def test_legacy_spool_migration_preserves_content_without_hot_journal_growth(continuity):
    core = continuity
    files = settled(core)
    item = core.stage("session-one", snapshot_text(core))
    core.start("session-one", "compact")
    with files.locked() as held:
        legacy = held.snapshot
        files.commit(held.model_copy(update={"snapshot": None, "spool": (legacy,)}))
    assert core.stop("session-one", "legacy session") == ""
    _, held = state(core)
    assert held.spool == () and held.last_snapshot == legacy
    assert (files.folder / core.policy.spool_directory / item.blob).is_file()
    assert not (files.folder / item.blob).exists()


def test_legacy_failure_flag_never_overrides_actual_read_proof(continuity):
    core = continuity
    core.start("session-one", "startup")
    files, held = state(core)
    files.commit(held.model_copy(update={"failure": "old error-only gate"}))
    assert core.stop("session-one", "still owe the actual identity read")
    assert core.stop("session-one", ack(core, held)) == ""
    assert state(core)[1].failure is None


def test_identity_source_outage_does_not_hide_an_available_snapshot(continuity):
    core = continuity
    core.config.identity_files[0].unlink()
    snapshot = core.stage("session-one", snapshot_text(core))
    context = core.start("session-one", "compact")
    assert "Identity read unavailable" in context and snapshot.digest in context
    _, held = state(core)
    assert not held.initialized and held.reads == () and held.snapshot.applied
    assert core.stop("session-one", core.snapshot_ack("session-one", snapshot)) == ""
    core.config.identity_files[0].write_text("Restored identity", encoding="utf-8")
    assert core.policy.read_marker in core.start("session-one", "resume")
