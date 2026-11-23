"""Launch records that let the next session start clear what a killed parent left behind.

A model worker runs over private input folders. While its parent lives, the parent reaps the
worker's process group and removes the folders. When the parent is killed outright (SIGKILL,
the OOM killer, a host hook timeout) none of that runs: the worker launcher has already set a
parent-death signal on itself, so the kernel kills the worker, and the record written here
before the launch is how the next session start finds the folders (worker_sweep).

A record is written before the worker starts, under one lock per records directory, with a
unique name, so two workers of one threaded process never share or overwrite a record. It names
its owner (uid), the boot, the parent (pid and kernel start time) and the folders; the launcher
adds its own pid and start time just before it execs the worker. The parent releases it only
after the folders are gone.
"""

import fcntl
import json
import os
import secrets
import sys
import time
from collections.abc import Generator, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Self, cast

from pydantic import Field, model_validator

from anamnema.private_files import PrivateFiles, PrivateStateBusy
from anamnema.setup_config import SetupError, StrictModel

BOOT_ID = Path("/proc/sys/kernel/random/boot_id")
LOCK_NAME = ".lock"
RECORD_SUFFIX = ".json"
LAUNCH_RECORD_LIMIT = 65536
LAUNCH_LOCK_WAIT_SECONDS = 5.0
LOCK_POLL_SECONDS = 0.01
FILES = PrivateFiles(0o700, 0o600, ".tmp-")


class RegistryConfig(StrictModel):
    """Where records live and which folders a sweep may ever remove."""

    records_dir: Path
    folder_roots: tuple[Path, ...] = Field(min_length=1)
    max_record_bytes: int = Field(default=16384, gt=0, le=LAUNCH_RECORD_LIMIT)
    max_records_scanned: int = Field(default=256, gt=0)
    lock_wait_seconds: float = Field(default=5.0, gt=0)

    @model_validator(mode="after")
    def explicit_paths(self) -> Self:
        """Absolute single-line paths; the records never sit where a sweep removes folders."""
        paths = (self.records_dir, *self.folder_roots)
        if any(not p.is_absolute() or "\n" in str(p) or "\r" in str(p) for p in paths):
            raise ValueError("worker registry paths must be absolute single lines")
        if any(".." in p.parts for p in paths):
            raise ValueError("worker registry paths cannot step to a parent")
        if any(self.records_dir.is_relative_to(root) for root in self.folder_roots):
            raise ValueError("worker records cannot live inside a swept folder root")
        return self


def start_time(pid: int) -> int | None:
    """Kernel start time of a process (field 22 of /proc/<pid>/stat), None when it is gone.

    With the pid and the boot it names one process: a reused pid starts later.
    """
    try:
        line = Path(f"/proc/{pid}/stat").read_text(encoding="ascii")
    except OSError, UnicodeError:
        return None
    # The command name (field 2) may hold spaces and parentheses; fields resume after the last ')'.
    return int(line[line.rindex(")") + 2 :].split()[19])


def boot_id() -> str:
    """This boot's identity; start times only compare within one boot."""
    return BOOT_ID.read_text(encoding="ascii").strip()


def runs(pid: object, start: object) -> bool:
    """True when this exact process (pid and start time, this boot) still runs."""
    return type(pid) is int and type(start) is int and start_time(pid) == start


def inside(folder: Path, roots: Sequence[Path]) -> bool:
    """Lexically strictly inside a root, no parent step; the sweep re-checks links on disk."""
    return (
        folder.is_absolute()
        and ".." not in folder.parts
        and any(folder.is_relative_to(root) and folder != root for root in roots)
    )


@contextmanager
def locked(records: Path, wait_seconds: float) -> Generator[None]:
    """One writer per records directory; waits a bounded time for another one to finish."""
    descriptor = os.open(records / LOCK_NAME, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        FILES.check_file(descriptor)
        deadline = time.monotonic() + wait_seconds
        while True:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError as exc:
                if time.monotonic() >= deadline:
                    raise PrivateStateBusy("worker records are busy; retry") from exc
                time.sleep(LOCK_POLL_SECONDS)
        yield
    finally:
        os.close(descriptor)


def save(path: Path, record: dict[str, Any], limit: int) -> None:
    """Write a record atomically, private to its owner, within its bound."""
    content = json.dumps(record, sort_keys=True).encode("utf-8")
    if len(content) > limit:
        raise SetupError("worker launch record exceeds configured bound")
    FILES.save(path, content)


def load(path: Path, limit: int) -> dict[str, Any]:
    """Read a private record of this user; anything else is refused."""
    record: object = json.loads(FILES.read(path, limit))
    if not isinstance(record, dict):
        raise SetupError("worker launch record is malformed")
    data = cast(dict[str, Any], record)
    if data.get("uid") != os.getuid():
        raise SetupError("worker launch record is foreign")
    return data


def register(config: RegistryConfig, folders: Sequence[Path]) -> Path:
    """Before the launch: record this parent and the folders its worker will read."""
    if not all(inside(folder, config.folder_roots) for folder in folders):
        raise SetupError("worker folders must lie strictly inside a configured folder root")
    FILES.directory(config.records_dir)
    parent = os.getpid()
    parent_start = start_time(parent)
    path = config.records_dir / f"{parent}-{parent_start}-{secrets.token_hex(8)}{RECORD_SUFFIX}"
    record = {
        "uid": os.getuid(),
        "boot_id": boot_id(),
        "parent_pid": parent,
        "parent_start": parent_start,
        "folders": [str(folder) for folder in folders],
        "pid": None,
        "start": None,
    }
    with locked(config.records_dir, config.lock_wait_seconds):
        save(path, record, config.max_record_bytes)
    return path


def launcher_argv(record: Path) -> list[str]:
    """The prefix that starts a worker through the launcher, bound to this parent."""
    parent = os.getpid()
    return [
        sys.executable,
        "-I",
        "-m",
        "anamnema.worker_launcher",
        "--record",
        str(record),
        "--parent-pid",
        str(parent),
        "--parent-start",
        str(start_time(parent)),
        "--",
    ]


def add_worker(record: Path, pid: int, start: int) -> None:
    """The launcher names itself, the leader of the worker's process group, once."""
    with locked(record.parent, LAUNCH_LOCK_WAIT_SECONDS):
        data = load(record, LAUNCH_RECORD_LIMIT)
        if data.get("pid") is not None:
            raise SetupError("worker launch record already names a worker")
        data.update(pid=pid, start=start)
        save(record, data, LAUNCH_RECORD_LIMIT)


def release(config: RegistryConfig, record: Path) -> None:
    """Drop the record once the worker group is reaped and its folders are removed."""
    with locked(config.records_dir, config.lock_wait_seconds):
        record.unlink(missing_ok=True)
