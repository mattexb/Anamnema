"""Neutral Secret Service transport: exact values on pipes, never in argv or error messages."""

import hmac
import os
import subprocess
from importlib.resources import files
from pathlib import Path
from typing import Protocol

from pydantic import Field, ValidationError

from anamnema.setup_config import Keyring, Nonempty, SetupError, StrictModel


class KeyringRuntime(Protocol):
    """The existing shared profile supplies bus and timeout choices, not secret values."""

    @property
    def bus_env(self) -> str: ...

    @property
    def bus_fallback(self) -> str: ...

    @property
    def keyring_timeout_seconds(self) -> float: ...


class SecretPolicy(StrictModel):
    """Public value/attribute bounds and a neutral keyring-manager label."""

    max_value_bytes: int = Field(gt=0)
    max_attribute_characters: int = Field(gt=0)
    max_injections: int = Field(gt=0)
    label_prefix: Nonempty


def secret_policy(path: Path | None = None) -> SecretPolicy:
    try:
        raw = (
            path.read_text(encoding="utf-8")
            if path
            else files("anamnema").joinpath("config/keyring.json").read_text(encoding="utf-8")
        )
        return SecretPolicy.model_validate_json(raw)
    except (OSError, UnicodeError, ValidationError) as exc:
        raise SetupError("keyring policy cannot be read or validated") from exc


class SecretService:
    """The source service/entry/field contract without private indexes or credential files."""

    def __init__(self, config: Keyring, runtime: KeyringRuntime) -> None:
        self.config, self.runtime = config, runtime
        self.policy = secret_policy(config.policy)

    def attributes(self, entry: str, field: str) -> list[str]:
        """Validate nonsecret item coordinates before prompting for a value."""
        for value in (self.config.namespace, entry, field):
            if (
                not value.strip()
                or len(value) > self.policy.max_attribute_characters
                or any(ord(c) < 32 or ord(c) == 127 for c in value)
            ):
                raise SetupError("keyring attributes must be bounded nonempty single lines")
        return ["service", self.config.namespace, "entry", entry, "field", field]

    def _run(
        self, arguments: list[str], value: str | None = None
    ) -> subprocess.CompletedProcess[str]:
        environment = dict(os.environ)
        if not environment.get(self.runtime.bus_env):
            environment[self.runtime.bus_env] = self.runtime.bus_fallback.format(uid=os.getuid())
        try:
            return subprocess.run(  # noqa: S603 - explicit configured tool, fixed operation, no shell
                [self.config.executable, *arguments],
                input=value,
                capture_output=True,
                text=True,
                encoding="utf-8",
                env=environment,
                timeout=self.runtime.keyring_timeout_seconds,
                check=False,
            )
        except (OSError, UnicodeError, ValueError, subprocess.TimeoutExpired) as exc:
            raise SetupError(f"project keyring command failed ({type(exc).__name__})") from exc

    def lookup(self, entry: str, field: str) -> str:
        """Non-TTY secret-tool output is the value itself, with no extra newline to strip."""
        result = self._run(["lookup", *self.attributes(entry, field)])
        if result.returncode or not result.stdout:
            raise SetupError("project credential unavailable; check keyring entry/unlock")
        if len(_value_bytes(result.stdout)) > self.policy.max_value_bytes:
            raise SetupError("project keyring value exceeds configured byte limit")
        return result.stdout

    def store(self, entry: str, field: str, value: str) -> None:
        """Create/replace explicitly, then verify exact bytes; never silently roll back a write."""
        attributes = self.attributes(entry, field)
        value_bytes = _value_bytes(value)
        if not value or len(value_bytes) > self.policy.max_value_bytes or "\x00" in value:
            raise SetupError(
                "keyring value is empty, contains NUL or exceeds configured byte limit"
            )
        label = " ".join([self.policy.label_prefix, self.config.namespace, entry, field])
        result = self._run(["store", "--label", label, *attributes], value)
        if result.returncode:
            raise SetupError("project keyring store failed; output withheld")
        try:
            stored = self.lookup(entry, field)
        except SetupError as exc:
            raise SetupError("keyring may have changed; store verification failed") from exc
        if not hmac.compare_digest(_value_bytes(stored), value_bytes):
            raise SetupError("keyring may have changed; stored value differs on read-back")


def _value_bytes(value: str) -> bytes:
    try:
        return value.encode("utf-8")
    except UnicodeError as exc:
        raise SetupError("keyring value is not valid UTF-8 text") from exc
