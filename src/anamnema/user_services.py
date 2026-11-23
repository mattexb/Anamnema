"""Owned, resumable user-unit installation; database package services are separate."""

import os
import stat
import subprocess
from pathlib import Path
from typing import Annotated, Literal, Self

from pydantic import Field, ValidationError, model_validator

from anamnema.archive_models import canonical, digest
from anamnema.private_files import PrivateFiles
from anamnema.setup_config import SetupError, StrictModel
from anamnema.worker_unit import WorkerUnit, WorkerUnitPolicy, render_worker_unit

UnitName = Annotated[str, Field(pattern=r"^anamnema-[a-z0-9][a-z0-9-]*\.service$")]


class UserServices(StrictModel):
    units_directory: Path
    state_directory: Path
    systemctl: Path
    command_seconds: float = Field(gt=0, allow_inf_nan=False)
    max_state_bytes: int = Field(gt=0)
    max_units: int = Field(gt=0)

    @model_validator(mode="after")
    def coordinates(self) -> Self:
        for path in (self.units_directory, self.state_directory, self.systemctl):
            if not path.is_absolute() or ".." in path.parts:
                raise ValueError("user service paths must be absolute without traversal")
        if (
            self.units_directory == self.state_directory
            or self.units_directory in self.state_directory.parents
            or self.state_directory in self.units_directory.parents
        ):
            raise ValueError("unit and receipt directories must be separate")
        return self


class ServiceReceipt(StrictModel):
    version: Literal[1]
    scope: str
    phase: Literal["prepared", "installed", "active", "rolling_back", "removing"]
    previous: dict[UnitName, str]
    desired: dict[UnitName, str]
    identity: str


class UserServiceInstaller:
    """Apply only explicitly supplied user units; retain the receipt on any failure."""

    def __init__(self, config: UserServices, policy: WorkerUnitPolicy) -> None:
        self.config, self.policy = config, policy
        self.files = PrivateFiles(0o700, 0o600, ".unit-install-")
        self.receipt = config.state_directory / "services.json"

    def plan(self, units: dict[str, WorkerUnit]) -> dict[str, str]:
        if not units or len(units) > self.config.max_units:
            raise SetupError("user service count violates configured bound")
        if any(unit.scope != "user" for unit in units.values()):
            raise SetupError("first-profile runtime units must use the logged-in user")
        rendered = {name: render_worker_unit(unit, self.policy) for name, unit in units.items()}
        self._receipt("prepared", {}, rendered)
        return rendered

    def apply(self, units: dict[str, WorkerUnit]) -> dict[str, str | bool | int]:
        desired = self.plan(units)
        if os.getuid() == 0:
            raise SetupError("user service installation must not run as root")
        if not self.config.systemctl.is_file() or not os.access(self.config.systemctl, os.X_OK):
            raise SetupError("user service manager executable is unavailable")
        self.files.directory(self.config.state_directory)
        self._unit_directory()
        with self.files.locked(self.config.state_directory / ".lock"):
            receipt = self._load()
            if receipt is not None and receipt.phase in {"rolling_back", "removing"}:
                raise SetupError("finish explicit user service rollback or removal first")
            if receipt is not None and set(receipt.desired) != set(desired):
                raise SetupError("changing owned service names requires remove() before apply()")
            self._verify_files(receipt, desired)
            unchanged = receipt is not None and receipt.desired == desired
            if receipt is not None and unchanged and receipt.phase == "active":
                self._command("is-active", "--quiet", *sorted(desired), inactive=True)
                return {"active": True, "units": len(desired), "unchanged": True}
            if not unchanged:
                if receipt is not None and receipt.phase != "active":
                    raise SetupError("finish the interrupted service plan or rollback() first")
                previous = receipt.desired if receipt is not None else {}
                receipt = self._receipt("prepared", previous, desired)
                self._save(receipt)
            if receipt is None:
                raise SetupError("user service receipt was not prepared")
            if receipt.phase == "prepared":
                for name, body in desired.items():
                    self.files.save(self.config.units_directory / name, body.encode("utf-8"))
                receipt = self._receipt("installed", receipt.previous, desired)
                self._save(receipt)
            self._command("daemon-reload")
            self._command("enable", *sorted(desired))
            self._command("restart", *sorted(desired))
            self._command("is-active", "--quiet", *sorted(desired))
            self._save(self._receipt("active", desired, desired))
            return {"active": True, "units": len(desired), "unchanged": False}

    def rollback(self) -> dict[str, bool | int]:
        """Abort a partial install or restore the last active bodies after a failed upgrade."""
        self.files.existing_directory(self.config.state_directory)
        self._unit_directory()
        with self.files.locked(self.config.state_directory / ".lock"):
            receipt = self._load()
            if receipt is None or receipt.phase == "active":
                raise SetupError("no interrupted user service plan to roll back")
            if receipt.phase == "removing":
                raise SetupError("finish user service remove() before another operation")
            self._verify_files(receipt, receipt.desired)
            receipt = receipt.model_copy(update={"phase": "rolling_back"})
            self._save(receipt)
            if not receipt.previous:
                self._remove_owned(receipt)
                return {"rolled_back": True, "active": False, "units": 0}
            self._command("stop", *sorted(receipt.desired))
            for name, body in receipt.previous.items():
                self.files.save(self.config.units_directory / name, body.encode("utf-8"))
            self._command("daemon-reload")
            self._command("enable", *sorted(receipt.previous))
            self._command("restart", *sorted(receipt.previous))
            self._command("is-active", "--quiet", *sorted(receipt.previous))
            self._save(self._receipt("active", receipt.previous, receipt.previous))
            return {"rolled_back": True, "active": True, "units": len(receipt.previous)}

    def remove(self) -> dict[str, bool | int]:
        """Stop and remove exact owned units only; keep foreign files and runtime data."""
        self.files.existing_directory(self.config.state_directory)
        self._unit_directory()
        with self.files.locked(self.config.state_directory / ".lock"):
            receipt = self._load()
            if receipt is None:
                return {"removed": False, "units": 0}
            if receipt.phase in {"prepared", "rolling_back"}:
                raise SetupError("finish or rollback() the prepared user service plan first")
            self._verify_files(receipt, receipt.desired)
            self._save(receipt.model_copy(update={"phase": "removing"}))
            self._remove_owned(receipt)
            return {"removed": True, "units": len(receipt.desired)}

    def _remove_owned(self, receipt: ServiceReceipt) -> None:
        present = sorted(
            name for name in receipt.desired if (self.config.units_directory / name).exists()
        )
        if present:
            self._command("disable", "--now", *present)
        for name in receipt.desired:
            (self.config.units_directory / name).unlink(missing_ok=True)
        self._sync_directory(self.config.units_directory)
        self._command("daemon-reload")
        self.receipt.unlink()
        self._sync_directory(self.config.state_directory)

    @staticmethod
    def _sync_directory(path: Path) -> None:
        descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def _receipt(
        self,
        phase: Literal["prepared", "installed", "active"],
        previous: dict[str, str],
        desired: dict[str, str],
    ) -> ServiceReceipt:
        try:
            result = ServiceReceipt(
                version=1,
                scope=self._scope(),
                phase=phase,
                previous=previous,
                desired=desired,
                identity=digest({name: body for name, body in desired.items()}),
            )
        except ValidationError as exc:
            raise SetupError("invalid owned user service names") from exc
        if len(result.model_dump_json().encode("utf-8")) > self.config.max_state_bytes:
            raise SetupError("user service receipt exceeds configured bound")
        return result

    def _load(self) -> ServiceReceipt | None:
        try:
            raw = self.files.read(self.receipt, self.config.max_state_bytes)
        except FileNotFoundError:
            return None
        try:
            receipt = ServiceReceipt.model_validate_json(raw)
        except ValidationError as exc:
            raise SetupError("user service receipt is invalid") from exc
        if (
            not receipt.desired
            or len(receipt.desired) > self.config.max_units
            or receipt.scope != self._scope()
            or receipt.identity != digest({name: body for name, body in receipt.desired.items()})
            or (receipt.previous and set(receipt.previous) != set(receipt.desired))
            or (receipt.phase == "active" and receipt.previous != receipt.desired)
        ):
            raise SetupError("user service receipt identity or scope differs")
        return receipt

    def _save(self, receipt: ServiceReceipt) -> None:
        self.files.save(self.receipt, canonical(receipt.model_dump(mode="json")))

    def _scope(self) -> str:
        return digest({"uid": os.getuid(), "directory": str(self.config.units_directory)})

    def _unit_directory(self) -> None:
        directory = self.config.units_directory
        if any(path.is_symlink() for path in (directory, *directory.parents)):
            raise SetupError("user service directory cannot contain symlinks")
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        info = directory.stat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o022:
            raise SetupError("user service directory must be owned and not publicly writable")

    def _verify_files(self, receipt: ServiceReceipt | None, planned: dict[str, str]) -> None:
        desired = receipt.desired if receipt is not None else {}
        if receipt is None:
            if any(os.path.lexists(self.config.units_directory / name) for name in planned):
                raise SetupError("unowned user service already exists; refusing adoption")
            return
        for name, body in desired.items():
            try:
                actual = self.files.read(
                    self.config.units_directory / name, self.config.max_state_bytes
                ).decode("utf-8")
            except FileNotFoundError:
                if receipt.phase == "removing" or (
                    receipt.phase in {"prepared", "rolling_back"} and name not in receipt.previous
                ):
                    continue
                raise SetupError("owned user service file is missing") from None
            except UnicodeError as exc:
                raise SetupError("owned user service file cannot be decoded") from exc
            allowed = {body}
            if receipt.phase in {"prepared", "rolling_back"} and name in receipt.previous:
                allowed.add(receipt.previous[name])
            if actual not in allowed:
                raise SetupError("owned user service was edited; refusing replacement")

    def _command(self, *arguments: str, inactive: bool = False) -> None:
        try:
            result = subprocess.run(  # noqa: S603 - explicit systemctl argv, no shell or secret values
                [str(self.config.systemctl), "--user", *arguments],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=self.config.command_seconds,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise SetupError("user service manager failed or exceeded its deadline; retry") from exc
        if result.returncode:
            if inactive:
                raise SetupError(
                    "owned user service is inactive; inspect systemctl --user status, "
                    "then restart it"
                )
            raise SetupError("user service manager refused the operation; receipt retained")
