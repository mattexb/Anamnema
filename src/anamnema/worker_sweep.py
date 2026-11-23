"""Clear what a worker parent killed outright left behind: its input folders and its record.

Only a private record of this user, within its bound, can lead to any action:
- its parent still runs in this boot (same pid and start time): left alone, a launch in progress;
- otherwise the worker's whole session is ended when its leader still runs with the recorded
  start time in this boot, or when no process holds the leader's pid any more: every member,
  in any process group, is signalled through a pidfd checked against its observed identity, and
  the session must be verified empty within a bounded wait; a leader pid now held by another
  process is never signalled;
- each folder is removed only if, at removal time, it is a real directory owned by this user
  strictly inside a configured root, reached from / without following any link (the root and
  its ancestors included) and with no parent step in its path; the record goes only once every
  such folder is verified gone, otherwise it stays and the next start retries.
A record from another boot has a dead parent by definition: its folders are cleared, nothing is
signalled. A foreign, oversized or unreadable record is left in place and counted. The sweep
never raises for one record; it holds the registry lock while it works.
"""

import ctypes
import logging
import os
import shutil
import signal
import stat
import time
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from anamnema.setup_config import SetupError
from anamnema.worker_registry import (
    FILES,
    RECORD_SUFFIX,
    RegistryConfig,
    boot_id,
    inside,
    load,
    locked,
    runs,
    start_time,
)

LOGGER = logging.getLogger(__name__)
END_WAIT_SECONDS = 1.0
END_POLL_SECONDS = 0.02
# Generic syscall numbers, the same on x86_64 and arm64; used when Python lacks the wrappers.
PIDFD_SEND_SIGNAL = 424
PIDFD_OPEN = 434


@dataclass
class SweepCounts:
    """What one sweep did; every field zero means there was nothing to clear."""

    live: int = 0
    cleared: int = 0
    killed: int = 0
    folders: int = 0
    kept_outside: int = 0
    unsafe: int = 0
    unscanned: int = 0
    failed: int = 0

    def worth_reporting(self) -> bool:
        """Something was cleared, kept, refused or left for the next start."""
        return any((self.cleared, self.kept_outside, self.unsafe, self.unscanned, self.failed))


def open_directory(path: Path) -> int:
    """Open an absolute directory from / one component at a time, never following a link.

    The root and every ancestor are checked at use, so a root replaced by a link is refused.
    """
    descriptor = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in path.parts[1:]:
            step = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = step
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor


def ignore(function: object, path: str, error: BaseException) -> None:
    """A failed step is not raised: the removal is verified afterwards instead."""


def remove(folder: Path, roots: Sequence[Path]) -> str:
    """Remove a real owned directory strictly inside a root; say what happened to it.

    "removed" and "absent" mean it is verified gone; "outside" means it is not ours to remove;
    "failed" means it is still there and its record must stay for the next start.
    """
    root = next((r for r in roots if inside(folder, (r,))), None)
    if root is None:
        return "outside"
    *between, name = folder.relative_to(root).parts
    try:
        parent = open_directory(root.joinpath(*between))
    except FileNotFoundError:
        return "absent"
    except OSError:
        return "outside"
    try:
        try:
            info = os.lstat(name, dir_fd=parent)
        except FileNotFoundError:
            return "absent"
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
            return "outside"
        shutil.rmtree(name, dir_fd=parent, onexc=ignore)
        try:
            os.lstat(name, dir_fd=parent)
        except FileNotFoundError:
            return "removed"
        return "failed"
    finally:
        os.close(parent)


def valid(record: dict[str, Any]) -> bool:
    """Every field has the type the registry writes."""
    folders: object = record.get("folders")
    return (
        isinstance(record.get("boot_id"), str)
        and type(record.get("parent_pid")) is int
        and type(record.get("parent_start")) in (int, type(None))
        and isinstance(folders, list)
        and all(isinstance(name, str) for name in cast(list[object], folders))
        and type(record.get("pid")) in (int, type(None))
        and type(record.get("start")) in (int, type(None))
    )


def identity(pid: int) -> tuple[int, int, int, str] | None:
    """Session id, start time, owner and state of a process, None when it is gone."""
    entry = Path(f"/proc/{pid}")
    try:
        fields = (entry / "stat").read_text(encoding="ascii").rsplit(")", 1)[1].split()
        owner = entry.stat().st_uid
        return int(fields[3]), int(fields[19]), owner, fields[0]
    except OSError, UnicodeError, IndexError, ValueError:
        return None


def session_members(session: int) -> list[tuple[int, int]]:
    """Live processes of this user in `session`, each as (pid, start time); zombies are done."""
    members: list[tuple[int, int]] = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        found = identity(int(entry.name))
        if found and found[0] == session and found[2] == os.getuid() and found[3] != "Z":
            members.append((int(entry.name), found[1]))
    return members


def _syscall(number: int, *arguments: int) -> int:
    """A raw Linux syscall; a failure raises the OSError subclass its errno names."""
    result = ctypes.CDLL(None, use_errno=True).syscall(
        ctypes.c_long(number), *(ctypes.c_long(a) for a in arguments)
    )
    if result < 0:
        code = ctypes.get_errno()
        raise OSError(code, os.strerror(code))
    return result


def pidfd_open(pid: int) -> int:
    """A descriptor pinned to the process that holds `pid` now (Linux 5.3)."""
    if hasattr(os, "pidfd_open"):
        return os.pidfd_open(pid)
    return _syscall(PIDFD_OPEN, pid, 0)


def pidfd_kill(descriptor: int) -> None:
    """SIGKILL the pinned process, never whatever holds its pid later."""
    if hasattr(signal, "pidfd_send_signal"):
        signal.pidfd_send_signal(descriptor, signal.SIGKILL)
        return
    _syscall(PIDFD_SEND_SIGNAL, descriptor, signal.SIGKILL, 0, 0)


def signal_member(pid: int, start: int, session: int) -> str:
    """SIGKILL one observed member through a pidfd, so a reused pid is never hit.

    The pidfd pins whatever process holds `pid` when it is opened; only if that process still
    has the observed session, start time and owner is it signalled. "killed", "gone" (it
    exited or the pid now names another process) or "failed".
    """
    try:
        descriptor = pidfd_open(pid)
    except ProcessLookupError:
        return "gone"
    except OSError:
        return "failed"
    try:
        found = identity(pid)
        if found is None or found[:3] != (session, start, os.getuid()):
            return "gone"
        pidfd_kill(descriptor)
        return "killed"
    except ProcessLookupError:
        return "gone"
    except OSError:
        return "failed"
    finally:
        os.close(descriptor)


def end_session(session: int, counts: SweepCounts) -> bool:
    """Kill every member of a worker's session and verify, for a bounded time, none is left.

    The worker leads its own session, so everything it started carries its pid as session id,
    whatever process group it moved to. Linux never gives that number to a new process while any
    process still uses it as a session id, so every member found is the worker's own.
    """
    deadline = time.monotonic() + END_WAIT_SECONDS
    signalled: set[tuple[int, int]] = set()
    while True:
        members = session_members(session)
        if not members:
            return True
        for member in members:
            outcome = signal_member(*member, session)
            if outcome == "failed":
                return False
            if outcome == "killed" and member not in signalled:
                signalled.add(member)
                counts.killed += 1
        if time.monotonic() >= deadline:
            return not session_members(session)
        time.sleep(END_POLL_SECONDS)


def clear(record: dict[str, Any], roots: Sequence[Path], counts: SweepCounts) -> bool:
    """End a worker's session of this boot, then remove its folders.

    The session is ended when its leader still runs with the recorded start time, or when no
    process holds the leader's pid any more; a leader pid now held by another process is never
    signalled. False when the session could not be verified empty or a folder that is ours
    could not be removed: the record then stays for the next start.
    """
    pid = record["pid"]
    ended = True
    if record["boot_id"] == boot_id() and type(pid) is int:
        if runs(pid, record["start"]) or start_time(pid) is None:
            ended = end_session(pid, counts)
    failed = not ended
    for name in record["folders"]:
        outcome = remove(Path(name), roots)
        counts.folders += outcome == "removed"
        counts.kept_outside += outcome == "outside"
        failed = failed or outcome == "failed"
    return not failed


def visit(path: Path, config: RegistryConfig, counts: SweepCounts) -> None:
    """Decide one record: leave it, clear it, or count it unsafe."""
    try:
        record = load(path, config.max_record_bytes)
    except OSError, ValueError, SetupError:
        counts.unsafe += 1
        return
    if not valid(record):
        counts.unsafe += 1
        return
    if record["boot_id"] == boot_id() and runs(record["parent_pid"], record["parent_start"]):
        counts.live += 1
        return
    if not clear(record, config.folder_roots, counts):
        counts.failed += 1
        return
    path.unlink(missing_ok=True)
    counts.cleared += 1


def sweep(config: RegistryConfig, logger: logging.Logger = LOGGER) -> SweepCounts:
    """Clear every record whose parent is gone; return what was done, as counts."""
    counts = SweepCounts()
    records = config.records_dir
    try:
        FILES.existing_directory(records)
    except OSError, SetupError:
        return counts
    try:
        with locked(records, config.lock_wait_seconds):
            with os.scandir(records) as entries:
                names = sorted(e.name for e in entries if e.name.endswith(RECORD_SUFFIX))
            counts.unscanned = max(0, len(names) - config.max_records_scanned)
            for name in names[: config.max_records_scanned]:
                visit(records / name, config, counts)
    except (OSError, SetupError) as exc:
        logger.warning("worker sweep stopped (%s)", type(exc).__name__)
    if counts.worth_reporting():
        logger.info("worker sweep: %s", counts)
    return counts
