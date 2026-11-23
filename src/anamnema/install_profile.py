"""Explicit first-profile deployment inputs; no private discovery or implicit account creation."""

from pathlib import Path
from typing import Annotated, Literal, Self

from pydantic import Field, ValidationError, model_validator

from anamnema.native_commands import NativeCommandPolicy
from anamnema.runtime_install import RuntimeInstallation
from anamnema.setup_config import Nonempty, SetupError, StrictModel


class InstallProfile(StrictModel):
    schema_version: Literal[1]
    instance: Nonempty
    state_root: Path
    database_state: Path
    runtime: RuntimeInstallation
    native_commands: NativeCommandPolicy
    dpkg_query: Path
    prerequisite_packages: tuple[Annotated[str, Field(pattern=r"^[a-z0-9][a-z0-9+.-]*$")], ...] = (
        "ca-certificates",
        "curl",
        "gpg",
        "build-essential",
        "cmake",
        "git",
        "pkg-config",
        "gnome-keyring",
        "libsecret-tools",
        "dbus-user-session",
    )
    memory_file: Path
    database_policy_file: Path | None = None
    memory_service_file: Path
    continuity_file: Path
    snapshot_worker_file: Path
    capture_deployment_file: Path
    capture_policy_file: Path
    recall_deployment_file: Path
    refresh_deployment_file: Path
    nightly_file: Path
    claude_settings: Path
    worker_policy_file: Path | None = None
    nightly_policy_file: Path | None = None
    hook_policy_file: Path | None = None
    max_profile_bytes: int = Field(default=1048576, gt=0, le=1048576)

    @model_validator(mode="after")
    def deployment(self) -> Self:
        paths = [
            value
            for value in (
                self.state_root,
                self.database_state,
                self.dpkg_query,
                self.memory_file,
                self.database_policy_file,
                self.memory_service_file,
                self.continuity_file,
                self.snapshot_worker_file,
                self.capture_deployment_file,
                self.capture_policy_file,
                self.recall_deployment_file,
                self.refresh_deployment_file,
                self.nightly_file,
                self.claude_settings,
                self.worker_policy_file,
                self.nightly_policy_file,
                self.hook_policy_file,
            )
            if value is not None
        ]
        if self.instance != self.runtime.instance or any(
            not path.is_absolute() or ".." in path.parts for path in paths
        ):
            raise ValueError("installation owner and absolute deployment paths must agree")
        if self.state_root == self.state_root.parent:
            raise ValueError("installation state must not be a filesystem root")
        return self


def install_profile(path: Path) -> InstallProfile:
    try:
        with path.open("rb") as stream:
            raw = stream.read(1048577)
        if len(raw) > 1048576:
            raise SetupError("installation profile exceeds bootstrap read bound")
        profile = InstallProfile.model_validate_json(raw)
        if len(raw) > profile.max_profile_bytes:
            raise SetupError("installation profile exceeds its configured read bound")
        return profile
    except (OSError, ValidationError) as exc:
        raise SetupError("installation profile cannot be read or validated") from exc
