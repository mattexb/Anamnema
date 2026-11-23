"""Independent journals reuse private atomic IO, without sharing state or locks."""

import os
import subprocess
import sys

import pytest

from anamnema.continuity_config import continuity_policy
from anamnema.private_files import PrivateFiles
from anamnema.setup_config import SetupError


@pytest.fixture
def private(tmp_path):
    policy = continuity_policy()
    files = PrivateFiles(policy.directory_mode, policy.file_mode, policy.temporary_prefix)
    root = tmp_path / "owned"
    files.directory(root)
    return files, root


def test_atomic_files_preserve_time_and_fail_before_publish_without_losing_prior(
    private, monkeypatch
):
    files, root = private
    path = root / "state.json"
    files.save(path, b"old", written_ns=123456789)
    assert path.stat().st_mtime_ns == 123456789
    assert files.read(path, 3) == b"old"

    def stopped(*_):
        raise OSError("fixture interruption before replace")

    monkeypatch.setattr("anamnema.private_files.os.replace", stopped)
    with pytest.raises(OSError, match="before replace"):
        files.save(path, b"new")
    assert files.read(path, 3) == b"old" and list(root.iterdir()) == [path]


@pytest.mark.parametrize("kind", ["symlink", "hardlink", "fifo", "public", "oversize"])
def test_private_reads_refuse_redirected_linked_nonregular_public_or_large_files(private, kind):
    files, root = private
    path = root / "state.json"
    files.save(path, b"owned")
    target = root / "other"
    if kind == "symlink":
        target.symlink_to(path)
    elif kind == "hardlink":
        os.link(path, target)
    elif kind == "fifo":
        os.mkfifo(target, mode=files.file_mode)
    elif kind == "public":
        path.chmod(0o644)
        target = path
    else:
        target = path
    with pytest.raises((SetupError, OSError)):
        files.read(target, 1 if kind == "oversize" else 10)


def test_private_directories_refuse_symlink_ancestors_and_public_permissions(private):
    files, root = private
    link = root.parent / "redirect"
    link.symlink_to(root, target_is_directory=True)
    with pytest.raises(SetupError, match="symlink"):
        files.directory(link / "child")
    root.chmod(0o755)
    with pytest.raises(SetupError, match="private"):
        files.existing_directory(root)


def test_lock_refuses_concurrent_owner_then_releases_without_resetting_state(private):
    files, root = private
    path, journal = root / ".lock", root / "state.json"
    files.save(journal, b"pending")
    with files.locked(path):
        with pytest.raises(SetupError, match="busy"):
            with files.locked(path):
                pytest.fail("busy lock admitted another owner")
    with files.locked(path):
        assert files.read(journal, 7) == b"pending"


def test_process_kill_releases_only_lock_and_retains_durable_state(private):
    files, root = private
    journal, lock = root / "state.json", root / ".worker.lock"
    files.save(journal, b"retryable")
    code = (
        "import pathlib,time; from anamnema.private_files import PrivateFiles; "
        f"f=PrivateFiles({files.directory_mode},{files.file_mode},{files.temporary_prefix!r}); "
        f"c=f.locked(pathlib.Path({str(lock)!r})); c.__enter__(); "
        "print('locked',flush=True); time.sleep(30)"
    )
    child = subprocess.Popen(  # noqa: S603 - own Python child and synthetic paths, no shell
        [sys.executable, "-c", code],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
    )
    try:
        assert child.stdout is not None and child.stdout.readline().strip() == "locked"
        with pytest.raises(SetupError, match="busy"):
            with files.locked(lock):
                pytest.fail("live worker lock was not exclusive")
        child.kill()
        stdout, stderr = child.communicate(timeout=10)
        assert not stdout and not stderr
        with files.locked(lock):
            assert files.read(journal, 9) == b"retryable"
    finally:
        if child.poll() is None:
            child.kill()
        child.communicate(timeout=10)
