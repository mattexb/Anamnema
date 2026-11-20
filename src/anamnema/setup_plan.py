"""Read-only prerequisite discovery and an honest ordered installation plan."""

from __future__ import annotations

import platform
import shutil
import sys
from dataclasses import asdict, dataclass
from typing import Literal

from anamnema.setup_config import SetupConfig


@dataclass(frozen=True)
class Host:
    """Read-only facts available without probing services or touching secrets."""

    system: str
    distribution: str
    version: str
    python: tuple[int, int]
    commands: frozenset[str]


@dataclass(frozen=True)
class Check:
    """A bounded observation, never a claim that a service is healthy."""

    name: str
    status: Literal["pass", "missing", "unsupported"]
    detail: str


@dataclass(frozen=True)
class Step:
    """An installation stage with explicit, currently unavailable execution."""

    name: str
    purpose: str
    status: Literal["not-implemented"] = "not-implemented"


STEPS = (
    Step("prerequisites", "Prepare the reviewed native Debian dependencies and Python environment"),
    Step("keyring", "Configure and verify the user's keyring without revealing secrets"),
    Step("archive", "Install and configure the local MongoDB archive"),
    Step("graph", "Install and configure the local Neo4j memory store"),
    Step("models", "Set up the tested notes, encoder and reranker models or reviewed API route"),
    Step("application", "Install the complete Anamnema memory application"),
    Step("claude", "Merge reviewed Claude hooks without replacing existing settings"),
    Step("schedule", "Register the ordered local nightly workflow"),
    Step("verification", "Prove the installed conversation-to-correction workflow"),
)


def required_commands(config: SetupConfig) -> tuple[str, ...]:
    """List configured executables relevant to this backend, without executing them."""
    backend = config.requirements.vulkan if config.backend == "vulkan" else config.requirements.api
    return tuple(dict.fromkeys((*config.requirements.common, config.keyring.executable, *backend)))


def inspect_host(config: SetupConfig) -> Host:
    """Read OS metadata and PATH only; no subprocess, network or keyring calls."""
    system = platform.system()
    release: dict[str, str] = {}
    if system == "Linux":
        try:
            release = platform.freedesktop_os_release()
        except OSError:
            release = {}
    commands = frozenset(name for name in required_commands(config) if shutil.which(name))
    return Host(
        system,
        release.get("ID", "unknown"),
        release.get("VERSION_ID", "unknown"),
        (sys.version_info.major, sys.version_info.minor),
        commands,
    )


def plan(config: SetupConfig, host: Host) -> dict[str, object]:
    """Report prerequisite observations and missing work without an installation claim."""
    target = config.target
    matches = (
        host.system == target.system
        and host.distribution == target.distribution
        and host.version == target.version
    )
    checks = [
        Check(
            "platform",
            "pass" if matches else "unsupported",
            f"observed {host.system}/{host.distribution}/{host.version}; "
            f"target {target.system}/{target.distribution}/{target.version}, {target.mode}",
        ),
        Check(
            "python",
            "pass" if host.python >= target.python_minimum else "unsupported",
            f"observed {host.python}; minimum {target.python_minimum}",
        ),
        Check(
            "backend",
            "unsupported" if config.backend == "cuda" else "pass",
            "CUDA is reserved, not implemented"
            if config.backend == "cuda"
            else f"{config.backend} is selected for planning, not verified inference",
        ),
    ]
    checks.extend(
        Check(name, "pass" if name in host.commands else "missing", "PATH discovery only")
        for name in required_commands(config)
    )
    return {
        "mode": "dry-run",
        "phase": "setup-planning-foundation",
        "backend": config.backend,
        "substrate": config.substrate,
        "choices": config.choices.model_dump(mode="json"),
        "checks": [asdict(check) for check in checks],
        "prerequisites_found": all(check.status == "pass" for check in checks),
        "ready_to_install": False,
        "steps": [asdict(step) for step in STEPS],
        "not_checked": [
            "Keyring session, unlock and secret access",
            "Database availability, authentication and ownership",
            "GPU capabilities, available memory and model execution",
            "Model downloads, licences and hardware minimums",
            "Claude hook compatibility and actual memory delivery",
        ],
        "blockers": [
            "Installation actions and dependent memory modules are not implemented",
            "Service privilege/account and unattended keyring policy require review",
        ],
    }
