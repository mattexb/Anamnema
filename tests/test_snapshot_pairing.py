"""Actual archive records prove scoped, ambiguity-preserving and late snapshot pairing."""

import pytest
from test_continuity import continuity as continuity
from test_snapshot_sync import queued, sync_for
from test_snapshot_sync import snapshots as snapshots

from anamnema.archive_models import Record
from anamnema.snapshot_archive import MongoSnapshots


def summary(snapshots, core, timestamp, *, instance=None, session="session-one", text="Summary"):
    source = {
        "type": "user",
        "timestamp": timestamp,
        "isCompactSummary": True,
        "message": {"content": text},
    }
    row = Record(
        instance or core.config.instance, "claude", session, "summary", timestamp, 1, source
    )
    snapshots.archive.write([row])
    snapshots.archive.verify([row])
    return row


def test_late_summary_pairs_on_retry_without_any_spool_left(continuity, snapshots):
    target = queued(continuity)
    sync = sync_for(continuity)
    first = sync.apply(snapshots)
    assert not target.exists() and first["pairs"]["pending"] == 1
    row = summary(snapshots, continuity, "2023-11-14T22:20:00Z")
    report = sync.apply(snapshots)
    assert report["removed"] == 0 and report["pairs"]["paired"] == 1
    stored = snapshots.archive.database[snapshots.policy.collection].find_one({})
    assert stored["summary_id"] == row.identity and stored["pair_status"] == "paired"
    assert sync.apply(snapshots)["pairs"]["paired"] == 0


def test_multiple_current_candidates_are_ambiguous_not_guessed(continuity, snapshots):
    queued(continuity)
    one = summary(snapshots, continuity, "2023-11-14T22:20:00Z")
    two = summary(snapshots, continuity, "2023-11-14T22:30:00Z", text="Another summary")
    report = sync_for(continuity).apply(snapshots)
    assert report["pairs"]["ambiguous"] == 1
    stored = snapshots.archive.database[snapshots.policy.collection].find_one({})
    assert set(stored["summary_candidates"]) == {one.identity, two.identity}
    assert "summary_id" not in stored


def test_other_owner_or_session_does_not_supply_pair_or_advance_watermark(continuity, snapshots):
    queued(continuity)
    summary(snapshots, continuity, "2023-11-14T22:20:00Z", instance="foreign")
    summary(snapshots, continuity, "2023-11-14T23:20:00Z", session="other-session")
    assert sync_for(continuity).apply(snapshots)["pairs"]["pending"] == 1


@pytest.mark.parametrize(
    "stamp,status", [("2023-11-14T22:00:00Z", "pending"), ("2023-11-14T23:00:00Z", "unpaired")]
)
def test_archive_progress_not_wall_clock_controls_unpaired(continuity, snapshots, stamp, status):
    queued(continuity)
    summary(snapshots, continuity, stamp)
    report = sync_for(continuity).apply(snapshots)
    assert report["pairs"][status] == 1


@pytest.mark.parametrize("bad", ["not a timestamp", "2023-11-14T22:20:00"])
def test_invalid_or_naive_time_is_reported_and_prevents_guessing(continuity, snapshots, bad):
    queued(continuity)
    invalid = summary(snapshots, continuity, bad)
    summary(snapshots, continuity, "2023-11-14T22:20:00Z")
    report = sync_for(continuity).apply(snapshots)
    assert report["pairs"]["invalid_summaries"] == 1 and report["pairs"]["pending"] == 1
    assert report["pairs"]["repair_required"] == [
        {"summary_id": invalid.identity, "reason": "invalid_or_naive_timestamp"}
    ]
    assert report["pairs"]["blocked_snapshots"] == 1


def test_corrupted_summary_body_is_rejected_without_cached_digest_trust(continuity, snapshots):
    queued(continuity)
    row = summary(snapshots, continuity, "2023-11-14T22:20:00Z")
    collection = snapshots.archive.database[snapshots.archive.settings.collections["summary"]]
    collection.update_one({"_id": row.identity}, {"$set": {"source.message.content": "corrupt"}})
    report = sync_for(continuity).apply(snapshots)
    assert report["removed"] == report["verified"] == 1
    assert report["pairs"] is None and "differs" in report["pairing_error"]
    snapshot = snapshots.archive.database[snapshots.policy.collection].find_one({})
    assert snapshot["pair_status"] == "pending" and "summary_id" not in snapshot


@pytest.mark.parametrize("limit", ["max_summaries", "max_pair_bytes"])
def test_pairing_limits_fail_without_truncating_into_a_false_unique_match(
    continuity, snapshots, limit
):
    queued(continuity)
    summary(snapshots, continuity, "2023-11-14T22:20:00Z")
    summary(snapshots, continuity, "2023-11-14T22:30:00Z", text="Another summary")
    policy = snapshots.policy.model_copy(update={limit: 1})
    archive = MongoSnapshots(snapshots.archive, policy)
    report = sync_for(continuity, policy).apply(archive)
    assert report["removed"] == report["inserted"] == report["verified"] == 1
    assert report["pairs"] is None and "bound" in report["pairing_error"]
    retry = sync_for(continuity, policy).apply(archive)
    assert retry["removed"] == retry["inserted"] == 0 and "bound" in retry["pairing_error"]
    stored = snapshots.archive.database[policy.collection].find_one({})
    assert stored["pair_status"] == "pending" and "summary_id" not in stored
