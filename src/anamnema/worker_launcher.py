"""Start a worker that dies with its parent and names itself in its launch record.

Run as its own program, never as a preexec function, so nothing runs in a forked copy of a
threaded parent. The parent starts it in a new session with the worker's argv after `--`:

1. it checks that its parent is the expected process (pid and kernel start time);
2. it sets PR_SET_PDEATHSIG to SIGKILL on itself, which survives the exec below;
3. it checks the parent again: a parent that died before step 2 was replaced as parent, and the
   launcher exits instead of starting a worker nobody waits for;
4. it writes its own pid and start time into the record, under the registry lock;
5. it execs the worker, which keeps the pid, the process group and the death signal.

The death signal follows the thread that started this process: the parent starts and waits for
the worker from one thread that outlives the worker. Exit 125: the parent is not the expected
one, or the death signal could not be set. Exit 126: the record could not be written. Exit 127:
the worker could not be executed. In all three the worker never runs.
"""

import argparse
import ctypes
import os
import signal
import sys
from pathlib import Path

from anamnema.setup_config import SetupError
from anamnema.worker_registry import add_worker, start_time

PR_SET_PDEATHSIG = 1
PARENT_REFUSED = 125
RECORD_REFUSED = 126
EXEC_FAILED = 127


def parse(argv: list[str]) -> argparse.Namespace:
    """The record, the expected parent and the worker argv after `--`."""
    parser = argparse.ArgumentParser(prog="anamnema.worker_launcher")
    parser.add_argument("--record", type=Path, required=True)
    parser.add_argument("--parent-pid", type=int, required=True)
    parser.add_argument("--parent-start", type=int, required=True)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    if args.command[:1] == ["--"]:
        args.command = args.command[1:]
    if not args.command:
        parser.error("a worker command is required after --")
    return args


def expected_parent(pid: int, start: int) -> bool:
    """This process's parent is still the exact process that started it."""
    return os.getppid() == pid and start_time(pid) == start


def die_with_parent() -> bool:
    """Ask the kernel to kill this process (and the worker it becomes) when the parent dies."""
    libc = ctypes.CDLL(None, use_errno=True)
    return libc.prctl(PR_SET_PDEATHSIG, signal.SIGKILL, 0, 0, 0) == 0


def main(argv: list[str] | None = None) -> int:
    """Run the five steps; only a fully registered launcher becomes the worker."""
    args = parse(sys.argv[1:] if argv is None else argv)
    if not expected_parent(args.parent_pid, args.parent_start) or not die_with_parent():
        return PARENT_REFUSED
    if not expected_parent(args.parent_pid, args.parent_start):
        return PARENT_REFUSED
    own_start = start_time(os.getpid())
    if own_start is None:
        return RECORD_REFUSED
    try:
        add_worker(args.record, os.getpid(), own_start)
    except OSError, ValueError, SetupError:
        return RECORD_REFUSED
    try:
        # The parent's validated worker argv replaces this process; no shell is involved.
        os.execv(args.command[0], args.command)  # noqa: S606
    except OSError:
        return EXEC_FAILED
    return EXEC_FAILED  # pragma: no cover - execv does not return


if __name__ == "__main__":
    sys.exit(main())
