"""Bounded native installer commands and read-only fresh-package probes."""

import os
import socket
from pathlib import Path

from pydantic import Field

from anamnema.database_install import Ran
from anamnema.setup_config import SetupError, StrictModel
from anamnema.snapshot_worker import run_command


class NativeCommandPolicy(StrictModel):
    max_output_bytes: int = Field(gt=0)
    max_input_bytes: int = Field(gt=0)
    read_chunk_bytes: int = Field(gt=0)
    timeout_seconds: float = Field(gt=0, allow_inf_nan=False)
    probe_seconds: float = Field(default=1.0, gt=0, allow_inf_nan=False)


class NativeRunner:
    """Binary stdout is needed for signing keys; all error bodies remain withheld."""

    def __init__(self, cwd: Path, policy: NativeCommandPolicy) -> None:
        if not cwd.is_absolute():
            raise SetupError("native command directory must be absolute")
        self.cwd, self.policy = cwd, policy

    def __call__(
        self,
        argv: list[str],
        stdin: bytes | None,
        timeout: float,
        env: dict[str, str] | None,
    ) -> Ran:
        if not 0 < timeout <= self.policy.timeout_seconds:
            raise SetupError("native command deadline exceeds installer policy")
        bounds = self.policy.model_copy(update={"timeout_seconds": timeout})
        environment = None if env is None else {**os.environ, **env}
        result = run_command(
            argv,
            self,
            bounds,
            "native installer command",
            input_data=stdin,
            input_limit=self.policy.max_input_bytes,
            environment=environment,
        )
        return Ran(result.code, result.stdout)


class NativeProbe:
    """Package and path observations do not initialize stores or read their content."""

    def __init__(self, runner: NativeRunner, dpkg_query: Path) -> None:
        if not dpkg_query.is_absolute():
            raise SetupError("package probe executable must be absolute")
        self.runner, self.dpkg_query = runner, dpkg_query

    def package_installed(self, name: str) -> bool:
        result = self.runner(
            [str(self.dpkg_query), "--show", "--showformat=${db:Status-Status}", "--", name],
            None,
            self.runner.policy.timeout_seconds,
            None,
        )
        if result.code not in (0, 1):
            raise SetupError("native package state cannot be inspected")
        return result.code == 0 and result.stdout == b"installed"

    def exists(self, path: Path) -> bool:
        return os.path.lexists(path)

    def listening(self, host: str, port: int) -> bool:
        """Readiness only: never authenticate or contact a non-loopback service."""
        if host != "127.0.0.1" or not 0 < port <= 65535:
            raise SetupError("native listener probe requires a valid IPv4 loopback endpoint")
        try:
            with socket.create_connection((host, port), timeout=self.runner.policy.probe_seconds):
                return True
        except OSError:
            return False

    def nonempty_directory(self, path: Path) -> bool:
        if path.is_symlink():
            return True
        try:
            with os.scandir(path) as entries:
                return next(entries, None) is not None
        except FileNotFoundError:
            return False
        except PermissionError, NotADirectoryError:
            return True
