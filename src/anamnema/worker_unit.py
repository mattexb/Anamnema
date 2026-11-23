"""Pure systemd worker units; generation never installs, enables or starts a service."""

from importlib.resources import files
from pathlib import Path
from typing import Literal, Self

from pydantic import Field, ValidationError, model_validator

from anamnema.setup_choices import Account
from anamnema.setup_config import Nonempty, SetupError, StrictModel


class WorkerUnitPolicy(StrictModel):
    """Restart and shutdown limits belong to installation policy, not a private host recipe."""

    restart_seconds: float = Field(gt=0, allow_inf_nan=False)
    stop_seconds: float = Field(gt=0, allow_inf_nan=False)
    start_limit_seconds: float = Field(gt=0, allow_inf_nan=False)
    start_limit_burst: int = Field(gt=0)
    user_target: Nonempty
    system_target: Nonempty
    umask: Literal["0077"]

    @model_validator(mode="after")
    def safe_targets(self) -> Self:
        for target in (self.user_target, self.system_target):
            if not target.endswith(".target") or any(
                not (character.isascii() and (character.isalnum() or character in "_.-"))
                for character in target
            ):
                raise ValueError("worker install target must be a plain target unit name")
        return self


class WorkerUnit(StrictModel):
    """Explicit command/account coordinates, with no secret environment or shell expansion."""

    description: Nonempty
    command: tuple[Nonempty, ...] = Field(min_length=1)
    cwd: Path
    scope: Literal["user", "system"]
    account: Account | None = None

    @model_validator(mode="after")
    def safe_values(self) -> Self:
        if not self.cwd.is_absolute() or not Path(self.command[0]).is_absolute():
            raise ValueError("worker executable and working directory must be absolute")
        if self.scope == "system" and (not self.account or self.account == "root"):
            raise ValueError("system worker requires an explicit non-root account")
        if self.scope == "user" and self.account is not None:
            raise ValueError("user worker uses its manager's account, not a User override")
        for value in (self.description, str(self.cwd), *self.command):
            if any(ord(character) < 32 or ord(character) == 127 for character in value):
                raise ValueError("worker unit values cannot contain control characters")
        if any(argument == ";" for argument in self.command):
            raise ValueError("standalone systemd command separators are not arguments")
        if self.description.endswith("\\"):
            raise ValueError("worker description cannot continue onto the next unit line")
        if "\\" in str(self.cwd) or str(self.cwd) != str(self.cwd).strip():
            raise ValueError("worker directory cannot contain backslashes or trailing whitespace")
        return self


def worker_unit_policy(path: Path | None = None) -> WorkerUnitPolicy:
    """Load service limits without consulting systemd, a native account or user settings."""
    try:
        raw = (
            path.read_text(encoding="utf-8")
            if path
            else files("anamnema").joinpath("config/worker_unit.json").read_text(encoding="utf-8")
        )
        return WorkerUnitPolicy.model_validate_json(raw)
    except (OSError, UnicodeError, ValidationError) as exc:
        raise SetupError("worker unit policy cannot be read or validated") from exc


def _quoted(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%") + '"'


def render_worker_unit(unit: WorkerUnit, policy: WorkerUnitPolicy) -> str:
    """Use systemd argv syntax, disable environment expansion, and reap the entire cgroup."""
    target = policy.user_target if unit.scope == "user" else policy.system_target
    lines = [
        "[Unit]",
        "Description=" + unit.description.replace("%", "%%"),
        f"StartLimitIntervalSec={policy.start_limit_seconds:g}",
        f"StartLimitBurst={policy.start_limit_burst}",
        "",
        "[Service]",
        "Type=exec",
        "ExecStart=:" + " ".join(_quoted(argument) for argument in unit.command),
        "WorkingDirectory=" + str(unit.cwd).replace("%", "%%"),
        "Restart=always",
        f"RestartSec={policy.restart_seconds:g}",
        f"TimeoutStopSec={policy.stop_seconds:g}",
        "KillMode=control-group",
        "SendSIGKILL=yes",
        "OOMPolicy=stop",
        "NoNewPrivileges=yes",
        "UMask=" + policy.umask,
        "LimitCORE=0",
        "StandardOutput=journal",
        "StandardError=journal",
    ]
    if unit.account:
        lines.append("User=" + unit.account)
    return "\n".join([*lines, "", "[Install]", "WantedBy=" + target, ""])
