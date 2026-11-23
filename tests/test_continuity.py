"""Real private files prove lifecycle state transitions, not native Claude execution."""

import hashlib
import json
import stat
import subprocess
import sys
import time

import pytest

from anamnema.continuity import Continuity
from anamnema.continuity_config import ContinuityConfig, continuity_policy
from anamnema.continuity_state import StateFiles
from anamnema.setup_config import SetupError


@pytest.fixture
def continuity(tmp_path):
    identity = tmp_path / "identity.md"
    identity.write_text("Synthetic assistant. Verify before claiming success.\n", encoding="utf-8")
    config = ContinuityConfig(
        instance="example", state_root=tmp_path / "managed", identity_files=(identity,)
    )
    return Continuity(config, continuity_policy())


def state(core, session="session-one"):
    files = StateFiles(core.config, core.policy, session)
    with files.locked() as held:
        return files, held


def ack(core, held):
    return "\n".join(f"{core.policy.read_marker} {r.token}" for r in held.reads)


def snapshot_text(core, session="session-one", body="Preserve the current task and next action."):
    return (
        f"{core.policy.snapshot_marker}\nInstance-ID: {core.config.instance}\n"
        f"Session-ID: {session}\n\n{body}\n"
    )


def settled(core):
    core.start("session-one", "startup")
    files, held = state(core)
    assert core.stop("session-one", ack(core, held)) == ""
    return files


def test_identity_nonce_is_only_in_last_managed_line_and_ack_cleans_copy(continuity):
    core = continuity
    original = core.config.identity_files[0].read_bytes()
    context = core.start("session-one", "startup")
    files, held = state(core)
    item = held.reads[0]
    assert item.token not in context and str(files.folder / item.blob) in context
    data = files.content(item.blob)
    assert json.loads(data.splitlines()[-1]) == {"read_token": item.token}
    assert "Synthetic assistant" in data
    assert stat.S_IMODE(files.folder.stat().st_mode) == core.policy.directory_mode
    assert stat.S_IMODE((files.folder / item.blob).stat().st_mode) == core.policy.file_mode
    assert core.stop("session-one", "Not an acknowledgement") == context
    assert core.stop("session-one", ack(core, held)) == ""
    assert not (files.folder / item.blob).exists()
    assert core.config.identity_files[0].read_bytes() == original
    assert core.start("session-one", "startup") == ""
    assert core.start("session-one", "resume") == ""


@pytest.mark.parametrize("source", ["startup", "resume", "clear", "fork"])
def test_repeated_start_preserves_unread_tokens(continuity, source):
    core = continuity
    first = core.start("session-one", "startup")
    _, before = state(core)
    assert core.start("session-one", source) == first
    assert state(core)[1] == before


@pytest.mark.parametrize(
    "prefix,suffix", [("quoted: ", ""), ("", " trailing"), (" ", ""), ("", " ")]
)
def test_echoed_embedded_or_nonexact_ack_does_not_pass(continuity, prefix, suffix):
    core = continuity
    core.start("session-one", "startup")
    _, held = state(core)
    assert core.stop("session-one", prefix + ack(core, held) + suffix)
    assert state(core)[1].reads == held.reads


def test_instance_and_session_cannot_ack_each_others_reads(continuity):
    core = continuity
    other = Continuity(core.config.model_copy(update={"instance": "other"}), core.policy)
    core.start("session-one", "startup")
    core.start("session-two", "startup")
    other.start("session-one", "startup")
    _, held = state(core)
    assert core.stop("session-two", ack(core, held))
    assert other.stop("session-one", ack(core, held))
    assert state(core)[0].folder != state(other)[0].folder


def test_idle_snapshot_is_not_owed_until_compact_then_ack_spools_once(continuity):
    core = continuity
    files = settled(core)
    text = snapshot_text(core)
    snapshot = core.stage("session-one", text)
    assert core.stage("session-one", text) == snapshot
    assert core.stop("session-one", "No read owed yet") == ""
    injected = core.start("session-one", "compact")
    assert snapshot.digest == hashlib.sha256(text.rstrip().encode("utf-8")).hexdigest()
    assert str(files.folder / snapshot.blob) in injected
    assert core.snapshot_ack("session-one", snapshot) in injected
    assert core.stop("session-one", "wrong hash") == injected
    assert core.stop("session-one", core.snapshot_ack("session-one", snapshot)) == ""
    _, held = state(core)
    assert held.snapshot is None and held.spool == () and held.last_snapshot.applied
    assert (files.folder / core.policy.spool_directory / snapshot.blob).exists()
    assert "unavailable" in core.start("session-one", "compact")
    assert core.stage("session-one", text).digest == snapshot.digest
    assert state(core)[1].snapshot is None


def test_identity_and_snapshot_ack_are_partial_and_independent(continuity):
    core = continuity
    core.start("session-one", "startup")
    _, held = state(core)
    snapshot = core.stage("session-one", snapshot_text(core))
    core.start("session-one", "compact")
    reason = core.stop("session-one", ack(core, held))
    assert (
        core.policy.read_marker not in reason
        and core.snapshot_ack("session-one", snapshot) in reason
    )
    assert state(core)[1].reads == ()
    assert core.stop("session-one", core.snapshot_ack("session-one", snapshot)) == ""


def test_unread_snapshot_cannot_be_replaced(continuity):
    core = continuity
    settled(core)
    core.stage("session-one", snapshot_text(core))
    core.start("session-one", "compact")
    before = state(core)[1]
    with pytest.raises(SetupError, match="refusing replacement"):
        core.stage("session-one", snapshot_text(core, body="Different task."))
    assert state(core)[1] == before


@pytest.mark.parametrize("session", ["../escape", "/absolute/escape", "wild*", "line\nbreak", ""])
def test_unsafe_session_is_refused_before_state_write(continuity, session):
    with pytest.raises(SetupError, match="unsafe"):
        continuity.start(session, "startup")
    assert not continuity.config.state_root.exists()


def test_identity_outage_warns_without_creating_unfulfillable_debt_and_can_be_retried(continuity):
    core = continuity
    identity = core.config.identity_files[0]
    identity.unlink()
    assert "Identity read unavailable" in core.start("session-one", "startup")
    assert state(core)[1].failure is None and not state(core)[1].initialized
    assert core.stop("session-one", "done") == ""
    identity.write_text("Restored synthetic identity", encoding="utf-8")
    assert core.start("session-one", "startup")
    assert state(core)[1].failure is None


def test_missing_start_or_missing_compaction_snapshot_is_visible(continuity):
    assert continuity.stop("session-one", "done") == ""
    assert "snapshot unavailable" in continuity.start("session-one", "compact")
    assert state(continuity)[1].failure is None
    assert continuity.stop("session-one", "done")  # Only the successfully produced identity read.


@pytest.mark.parametrize("bad", ["corrupt", "foreign", "symlink", "content"])
def test_corrupt_or_redirected_state_is_never_reset_or_followed(continuity, tmp_path, bad):
    core = continuity
    core.start("session-one", "startup")
    files, held = state(core)
    journal = files.folder / core.policy.state_file
    if bad == "corrupt":
        journal.write_text("{broken", encoding="utf-8")
    elif bad == "foreign":
        files.commit(held)
        data = json.loads(journal.read_text(encoding="utf-8"))
        data["instance"] = "foreign"
        journal.write_text(json.dumps(data), encoding="utf-8")
    elif bad == "symlink":
        outside = tmp_path / "outside"
        outside.write_text("Do not touch", encoding="utf-8")
        journal.unlink()
        journal.symlink_to(outside)
    else:
        (files.folder / held.reads[0].blob).write_text("changed", encoding="utf-8")
    before = journal.read_bytes()
    with pytest.raises(SetupError):
        core.stop("session-one", ack(core, held))
    assert journal.read_bytes() == before


def test_busy_session_is_refused_not_overwritten(continuity):
    core = continuity
    core.start("session-one", "startup")
    files = StateFiles(core.config, core.policy, "session-one")
    with files.locked() as held:
        with pytest.raises(SetupError, match="busy"):
            core.start("session-one", "startup")
    assert state(core)[1] == held


def test_failed_journal_commit_preserves_owed_reads_and_retry_settles(continuity, monkeypatch):
    core = continuity
    core.start("session-one", "startup")
    files, held = state(core)
    commit = StateFiles.commit

    def stopped(*_):
        raise OSError("fixture interrupted before journal publication")

    monkeypatch.setattr(StateFiles, "commit", stopped)
    with pytest.raises(SetupError):
        core.stop("session-one", ack(core, held))
    assert (files.folder / held.reads[0].blob).exists()
    assert state(core)[1] == held
    monkeypatch.setattr(StateFiles, "commit", commit)
    assert core.stop("session-one", ack(core, held)) == ""


def test_failed_snapshot_journal_write_leaves_recoverable_orphan_not_lost_debt(
    continuity, monkeypatch
):
    core = continuity
    files = settled(core)
    before = set(files.folder.iterdir())
    original = StateFiles.commit

    def stopped(*_):
        raise OSError("fixture interrupted after content write")

    monkeypatch.setattr(StateFiles, "commit", stopped)
    with pytest.raises(SetupError):
        core.stage("session-one", snapshot_text(core))
    monkeypatch.setattr(StateFiles, "commit", original)
    assert state(core)[1].snapshot is None
    spool = files.folder / core.policy.spool_directory
    assert set(files.folder.iterdir()) == before | {spool}
    assert list(spool.iterdir()) == []
    assert core.stage("session-one", snapshot_text(core))


def test_directory_symlink_or_public_mode_is_refused(continuity, tmp_path):
    core = continuity
    root = core.config.state_root
    outside = tmp_path / "outside"
    outside.mkdir()
    root.symlink_to(outside, target_is_directory=True)
    with pytest.raises(SetupError, match="symlinks"):
        core.start("session-one", "startup")
    assert list(outside.iterdir()) == []
    root.unlink()
    root.mkdir(mode=0o755)
    with pytest.raises(SetupError, match="not private"):
        core.start("session-one", "startup")
    assert stat.S_IMODE(root.stat().st_mode) == 0o755


def test_snapshot_headers_and_content_bounds_are_enforced(continuity):
    core = continuity
    for text in (
        "",
        snapshot_text(core, "foreign"),
        snapshot_text(core).replace("example", "other"),
    ):
        with pytest.raises(SetupError, match="headers"):
            core.stage("session-one", text)
    limited = Continuity(core.config, core.policy.model_copy(update={"max_content_bytes": 8}))
    assert "Identity read unavailable" in limited.start("session-one", "startup")
    assert limited.stop("session-one", "done") == ""


@pytest.mark.parametrize("boundary", ["staging", "acknowledgement"])
def test_killed_writer_keeps_old_journal_and_retries_without_loss(continuity, tmp_path, boundary):
    core = continuity
    files = settled(core)
    applied = None
    if boundary == "acknowledgement":
        applied = core.stage("session-one", snapshot_text(core))
        core.start("session-one", "compact")
    before = state(core)[1]
    config = tmp_path / "deployment.json"
    config.write_text(core.config.model_dump_json(), encoding="utf-8")
    snapshot = tmp_path / "snapshot.md"
    snapshot.write_text(snapshot_text(core), encoding="utf-8")
    ready = tmp_path / "ready"
    script = """
import sys, time
from pathlib import Path
from anamnema.continuity import Continuity
from anamnema.continuity_config import continuity_config, continuity_policy
from anamnema.continuity_state import StateFiles
def pause(self, state):
    Path(sys.argv[3]).write_text('content published', encoding='utf-8')
    time.sleep(60)
    raise RuntimeError('parent failed to stop fixture')
StateFiles.commit = pause
core = Continuity(continuity_config(Path(sys.argv[1])), continuity_policy())
if sys.argv[4] == 'staging':
    core.stage('session-one', Path(sys.argv[2]).read_text(encoding='utf-8'))
else:
    core.stop('session-one', sys.argv[5])
"""
    process = subprocess.Popen(  # noqa: S603 - own interpreter and synthetic fixture paths
        [
            sys.executable,
            "-c",
            script,
            str(config),
            str(snapshot),
            str(ready),
            boundary,
            core.snapshot_ack("session-one", applied) if applied else "",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
    )
    try:
        deadline = time.monotonic() + 10
        while not ready.exists() and process.poll() is None and time.monotonic() < deadline:
            time.sleep(0.01)
        assert ready.exists(), "owned child did not reach the interrupted commit boundary"
    finally:
        if process.poll() is None:
            process.kill()
        stdout, stderr = process.communicate(timeout=5)
    assert process.returncode < 0, (stdout, stderr)
    assert state(core)[1] == before
    if applied:
        target = files.folder / core.policy.spool_directory / applied.blob
        assert target.is_file() and (files.folder / applied.blob).is_file()
        assert core.stop("session-one", core.snapshot_ack("session-one", applied)) == ""
        assert target.is_file() and not (files.folder / applied.blob).exists()
        assert len(list(target.parent.iterdir())) == 1
    else:
        assert not list(files.folder.glob("*" + core.policy.blob_suffix))
        assert core.stage("session-one", snapshot_text(core))
