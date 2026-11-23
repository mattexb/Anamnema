"""Bounded POSIX fresh-worker transport; output and owned process groups cannot outlive errors."""

import os
import selectors
import signal
import subprocess
import time
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO, Protocol

from anamnema.setup_config import SetupError
from anamnema.worker_registry import RegistryConfig, launcher_argv, register, release


class WorkerDirectory(Protocol):
    @property
    def cwd(self) -> Path: ...


class WorkerBounds(Protocol):
    @property
    def read_chunk_bytes(self) -> int: ...

    @property
    def max_output_bytes(self) -> int: ...

    @property
    def timeout_seconds(self) -> float: ...


@dataclass(frozen=True)
class CommandResult:
    code: int
    stdout: bytes
    stderr: bytes = field(default=b"", repr=False)


@contextmanager
def registered_command(
    command: list[str], registry: RegistryConfig | None, record: Path | None
) -> Generator[list[str]]:
    """A folderless command owns its intent; input owners release theirs after cleanup."""
    if registry is None:
        if record is not None:
            raise SetupError("worker record requires its explicit registry")
        yield command
        return
    owns_record = record is None
    if record is None:
        record = register(registry, ())
    if record.parent != registry.records_dir:
        raise SetupError("worker record differs from its configured registry")
    try:
        yield [*launcher_argv(record), *command]
    finally:
        if owns_record:
            release(registry, record)


def _feed(
    stream: IO[bytes], selector: selectors.BaseSelector, data: bytes, offset: int, chunk_size: int
) -> int:
    """Feed stdin without blocking the simultaneous stdout/stderr drain."""
    try:
        written = os.write(stream.fileno(), data[offset : offset + chunk_size])
    except BlockingIOError:
        return offset
    if written == 0:
        raise SetupError("worker input pipe made no progress")
    offset += written
    if offset == len(data):
        selector.unregister(stream)
        stream.close()
    return offset


def _collect(
    process: subprocess.Popen[bytes],
    policy: WorkerBounds,
    deadline: float,
    context: str,
    input_data: bytes | None,
) -> CommandResult:
    """Drain both pipes with a combined byte bound and one whole-invocation deadline."""
    if process.stdout is None or process.stderr is None:
        raise SetupError(f"{context} output pipes are unavailable")
    output: dict[int, bytearray] = {
        process.stdout.fileno(): bytearray(),
        process.stderr.fileno(): bytearray(),
    }
    stdout = process.stdout.fileno()
    total, sent = 0, 0
    with selectors.DefaultSelector() as selector:
        for stream in (process.stdout, process.stderr):
            selector.register(stream, selectors.EVENT_READ)
        incoming = process.stdin
        if incoming is not None:
            if input_data:
                os.set_blocking(incoming.fileno(), False)
                selector.register(incoming, selectors.EVENT_WRITE, "input")
            else:
                incoming.close()
        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise SetupError(f"{context} timed out; previous state retained")
            for key, _ in selector.select(remaining):
                if key.data == "input":
                    if incoming is None or input_data is None:
                        raise SetupError(f"{context} input pipe is unavailable")
                    sent = _feed(incoming, selector, input_data, sent, policy.read_chunk_bytes)
                    continue
                chunk = os.read(key.fd, min(policy.read_chunk_bytes, policy.max_output_bytes + 1))
                total += len(chunk)
                if total > policy.max_output_bytes:
                    raise SetupError(f"{context} output exceeds configured bound")
                if chunk:
                    output[key.fd].extend(chunk)
                else:
                    selector.unregister(key.fileobj)
        code = process.wait(timeout=max(0, deadline - time.monotonic()))
    return CommandResult(code, bytes(output[stdout]), bytes(output[process.stderr.fileno()]))


def run_worker(
    command: list[str],
    worker: WorkerDirectory,
    policy: WorkerBounds,
    context: str = "snapshot worker",
    *,
    input_text: str | None = None,
    input_limit: int | None = None,
    registry: RegistryConfig | None = None,
    record: Path | None = None,
) -> str:
    """Spawn only the explicit executable, no shell; always reap this worker and its children."""
    request: bytes | None = None
    if input_text is not None:
        if input_limit is None or input_limit <= 0:
            raise SetupError(f"{context} stdin requires an explicit positive byte limit")
        try:
            request = input_text.encode("utf-8")
        except UnicodeError as exc:
            raise SetupError(f"{context} input is not valid UTF-8") from exc
        if len(request) > input_limit:
            raise SetupError(f"{context} input exceeds configured byte bound")
    result = run_command(
        command,
        worker,
        policy,
        context,
        input_data=request,
        input_limit=input_limit,
        registry=registry,
        record=record,
    )
    if result.code != 0:
        raise SetupError(
            f"{context} exited {result.code}; output withheld to protect transcript data"
        )
    try:
        return result.stdout.decode("utf-8").strip()
    except UnicodeError as exc:
        raise SetupError(f"{context} transport failed ({type(exc).__name__})") from exc


def run_command(
    command: list[str],
    worker: WorkerDirectory,
    policy: WorkerBounds,
    context: str,
    *,
    input_data: bytes | None = None,
    input_limit: int | None = None,
    environment: dict[str, str] | None = None,
    registry: RegistryConfig | None = None,
    record: Path | None = None,
) -> CommandResult:
    """Bound both binary output channels; callers must not expose private diagnostics."""
    if input_data is not None and (
        input_limit is None or input_limit <= 0 or len(input_data) > input_limit
    ):
        raise SetupError(f"{context} input exceeds an explicit positive byte bound")
    deadline = time.monotonic() + policy.timeout_seconds
    with registered_command(command, registry, record) as prepared:
        try:
            process = subprocess.Popen(  # noqa: S603 - explicit argv, no shell, owned launch group
                prepared,
                cwd=worker.cwd,
                stdin=subprocess.PIPE if input_data is not None else subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=True,
                env=environment,
            )
        except OSError as exc:
            raise SetupError(f"{context} cannot start ({type(exc).__name__})") from exc
        try:
            return _collect(process, policy, deadline, context, input_data)
        except (OSError, UnicodeError, subprocess.TimeoutExpired) as exc:
            raise SetupError(f"{context} transport failed ({type(exc).__name__})") from exc
        finally:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                if process.poll() is None:
                    raise SetupError(f"{context} disappeared before it could be reaped") from None
            finally:
                process.wait()
                if process.stdout is not None:
                    process.stdout.close()
                if process.stderr is not None:
                    process.stderr.close()
                if process.stdin is not None and not process.stdin.closed:
                    process.stdin.close()
