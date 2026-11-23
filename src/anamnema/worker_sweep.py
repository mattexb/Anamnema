"""Clear what a worker parent killed outright left behind: its input folders and its record.

Only a private record of this user, within its bound, can lead to any action:
- its parent still runs in this boot (same pid and start time): left alone, a launch in progress;
- otherwise the worker's group is killed only if its leader still runs with the recorded start
  time in this boot (its death signal should already have killed it); a group whose leader is
  gone is never signalled, so a reused pid or group id is never hit;
- each folder is removed only if, at removal time, it is a real directory owned by this user
  strictly inside a configured root, reached from / without following any link (the root and
  its ancestors included) and with no parent step in its path; the record goes only once every
  such folder is verified gone, otherwise it stays and the next start retries.
A record from another boot has a dead parent by definition: its folders are cleared, nothing is
signalled. A foreign, oversized or unreadable record is left in place and counted. The sweep
never raises for one record; it holds the registry lock while it works.
"""

import logging
import os
import shutil
import signal
import stat
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
)

LOGGER = logging.getLogger(__name__)


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


def clear(record: dict[str, Any], roots: Sequence[Path], counts: SweepCounts) -> bool:
    """Kill a still-running worker of this boot, then remove the folders inside the roots.

    False when a folder that is ours could not be removed: the record then stays.
    """
    pid = record["pid"]
    if record["boot_id"] == boot_id() and runs(pid, record["start"]):
        try:
            os.killpg(pid, signal.SIGKILL)
            counts.killed += 1
        except ProcessLookupError:
            pass
    failed = False
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
