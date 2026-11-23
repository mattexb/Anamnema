"""Install the nightly chain as a user oneshot service and its timer; plan, apply and remove.

The long-running worker template does not fit: it restarts forever and is wanted by the default
target. This oneshot never restarts, is started only by its timer, and its whole control group
is killed at the start deadline. The installer owns exactly the two files in its receipt: a file
it did not write is never adopted, an owned file edited by hand is never overwritten, and the
receipt's intent is saved before any file or systemctl change, so an interrupted apply or remove
finishes on the next run.
"""

import os
import subprocess
from importlib.resources import files
from pathlib import Path
from typing import Literal, Self

from pydantic import Field, ValidationError, model_validator

from anamnema.private_files import PrivateFiles
from anamnema.setup_config import Nonempty, SetupError, StrictModel


class NightlyTimerPolicy(StrictModel):
    unit_name: Nonempty = Field(pattern=r"^anamnema-[a-z0-9][a-z0-9-]*$")
    on_calendar: Nonempty
    randomized_delay_seconds: int = Field(ge=0)
    persistent: bool
    timeout_start_seconds: int = Field(gt=0)
    command_seconds: float = Field(gt=0, allow_inf_nan=False)
    max_state_bytes: int = Field(gt=0)

    @model_validator(mode="after")
    def single_line(self) -> Self:
        if any(ord(c) < 32 or ord(c) == 127 for c in self.on_calendar):
            raise ValueError("timer calendar must be one line")
        return self


class NightlyReceipt(StrictModel):
    version: Literal[1]
    phase: Literal["writing", "active", "removing"]
    units: dict[str, str]


def nightly_timer_policy(path: Path | None = None) -> NightlyTimerPolicy:
    try:
        text = (
            path.read_text(encoding="utf-8")
            if path
            else files("anamnema").joinpath("config/nightly_timer.json").read_text(encoding="utf-8")
        )
        return NightlyTimerPolicy.model_validate_json(text)
    except (OSError, UnicodeError, ValidationError) as exc:
        raise SetupError(f"invalid nightly timer policy ({type(exc).__name__})") from exc


def _quoted(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%") + '"'


def render_nightly(
    executable: Path,
    nightly_config: Path,
    graph_options: tuple[str, ...],
    cwd: Path,
    policy: NightlyTimerPolicy,
) -> dict[str, str]:
    """The service and timer bodies, keyed by unit file name; argv quoted, never a shell."""
    command = (str(executable), "nightly", "--nightly-config", str(nightly_config), *graph_options)
    paths = (executable, nightly_config, cwd)
    if any(not path.is_absolute() for path in paths):
        raise SetupError("nightly unit paths must be absolute")
    for value in (*command, str(cwd)):
        if any(ord(c) < 32 or ord(c) == 127 for c in value) or value == ";":
            raise SetupError("nightly unit values cannot contain control characters or ';'")
    service = "\n".join(
        [
            "[Unit]",
            "Description=Anamnema nightly finishing chain",
            "",
            "[Service]",
            "Type=oneshot",
            "ExecStart=:" + " ".join(_quoted(argument) for argument in command),
            "WorkingDirectory=" + str(cwd).replace("%", "%%"),
            f"TimeoutStartSec={policy.timeout_start_seconds}",
            "KillMode=control-group",
            "SendSIGKILL=yes",
            "NoNewPrivileges=yes",
            "UMask=0077",
            "LimitCORE=0",
            "StandardOutput=journal",
            "StandardError=journal",
            "",
        ]
    )
    timer = "\n".join(
        [
            "[Unit]",
            "Description=Run the Anamnema nightly finishing chain",
            "",
            "[Timer]",
            "OnCalendar=" + policy.on_calendar,
            f"RandomizedDelaySec={policy.randomized_delay_seconds}",
            "Persistent=" + ("true" if policy.persistent else "false"),
            f"Unit={policy.unit_name}.service",
            "",
            "[Install]",
            "WantedBy=timers.target",
            "",
        ]
    )
    return {f"{policy.unit_name}.service": service, f"{policy.unit_name}.timer": timer}


class NightlyInstaller:
    """plan/apply/remove for the installer; it never edits any other unit or setting."""

    def __init__(
        self,
        units_directory: Path,
        state_directory: Path,
        systemctl: Path,
        policy: NightlyTimerPolicy,
    ) -> None:
        if any(not p.is_absolute() for p in (units_directory, state_directory, systemctl)):
            raise SetupError("nightly installer paths must be absolute")
        self.units, self.state, self.systemctl, self.policy = (
            units_directory,
            state_directory,
            systemctl,
            policy,
        )
        self.files = PrivateFiles(0o700, 0o600, ".nightly-units-")
        self.receipt = state_directory / "nightly_units.json"
        self.timer = f"{policy.unit_name}.timer"

    def plan(self, desired: dict[str, str]) -> dict[str, object]:
        receipt = self._load()
        self._verify(receipt, desired)
        current = receipt.units if receipt else {}
        return {
            "changed": current != desired or (receipt is not None and receipt.phase != "active"),
            "units": sorted(desired),
        }

    def apply(self, desired: dict[str, str]) -> dict[str, object]:
        if os.getuid() == 0:
            raise SetupError("the nightly user timer must not be installed as root")
        self.files.directory(self.state)
        self.units.mkdir(parents=True, exist_ok=True, mode=0o700)
        with self.files.locked(self.state / ".nightly-units.lock"):
            receipt = self._load()
            if receipt is not None and receipt.phase == "removing":
                raise SetupError("finish nightly remove() before apply()")
            if receipt is not None and set(receipt.units) != set(desired):
                raise SetupError("nightly unit names changed; remove() before apply()")
            self._verify(receipt, desired)
            if receipt is not None and receipt.units == desired and receipt.phase == "active":
                return {"changed": False, "units": sorted(desired)}
            self._save("writing", desired)
            for name, body in desired.items():
                self.files.save(self.units / name, body.encode("utf-8"))
            self._command("daemon-reload")
            self._command("enable", "--now", self.timer)
            self._save("active", desired)
            return {"changed": True, "units": sorted(desired)}

    def remove(self) -> dict[str, object]:
        if not self.receipt.exists():
            return {"removed": 0}
        with self.files.locked(self.state / ".nightly-units.lock"):
            receipt = self._load()
            if receipt is None:
                return {"removed": 0}
            self._verify(receipt, receipt.units)
            self._save("removing", receipt.units)
            if (self.units / self.timer).exists():
                self._command("disable", "--now", self.timer)
            service = f"{self.policy.unit_name}.service"
            if (self.units / service).exists():
                self._command("stop", service)
            for name in receipt.units:
                (self.units / name).unlink(missing_ok=True)
            self._command("daemon-reload")
            self.receipt.unlink()
            return {"removed": len(receipt.units)}

    def _verify(self, receipt: NightlyReceipt | None, desired: dict[str, str]) -> None:
        """A target holds the recorded body or the desired one; anything else is not ours."""
        for name in desired:
            path = self.units / name
            if not os.path.lexists(path):
                continue
            if receipt is None or name not in receipt.units:
                raise SetupError("a nightly unit already exists that this installer did not write")
            actual = self.files.read(path, self.policy.max_state_bytes).decode("utf-8")
            if actual not in (receipt.units[name], desired[name]):
                raise SetupError("an owned nightly unit was edited by hand; nothing replaced")

    def _load(self) -> NightlyReceipt | None:
        try:
            raw = self.files.read(self.receipt, self.policy.max_state_bytes)
        except FileNotFoundError:
            return None
        try:
            return NightlyReceipt.model_validate_json(raw)
        except ValidationError as exc:
            raise SetupError("nightly unit receipt is invalid") from exc

    def _save(self, phase: Literal["writing", "active", "removing"], units: dict[str, str]) -> None:
        receipt = NightlyReceipt(version=1, phase=phase, units=units)
        self.files.save(self.receipt, receipt.model_dump_json().encode("utf-8"))

    def _command(self, *arguments: str) -> None:
        try:
            result = subprocess.run(  # noqa: S603 - explicit systemctl argv, no shell
                [str(self.systemctl), "--user", *arguments],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=self.policy.command_seconds,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise SetupError("user service manager failed or timed out; retry") from exc
        if result.returncode:
            raise SetupError("user service manager refused the operation; receipt retained")
