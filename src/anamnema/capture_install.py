"""Pure capture installation assembly; hooks may be applied only after watcher verification."""

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from anamnema.archive_models import digest
from anamnema.capture_queue_config import CaptureDeployment, CaptureQueuePolicy
from anamnema.hook_merge import HookBinding
from anamnema.setup_config import SetupError
from anamnema.worker_unit import WorkerUnit, WorkerUnitPolicy, render_worker_unit


@dataclass(frozen=True)
class CaptureInstallation:
    """Generated settings are not installation evidence or permission to register live hooks."""

    input_digest: str
    watcher: WorkerUnit
    unit: str
    hooks: tuple[HookBinding, ...]


def capture_installation(
    executable: Path,
    deployment_path: Path,
    policy_path: Path,
    deployment: CaptureDeployment,
    policy: CaptureQueuePolicy,
    service_policy: WorkerUnitPolicy,
    *,
    scope: Literal["user", "system"],
    account: str | None,
) -> CaptureInstallation:
    """Share the exact deployment/policy arguments between the watcher and all three events."""
    if any(not path.is_absolute() for path in (executable, deployment_path, policy_path)):
        raise SetupError("capture installation coordinates must be explicit absolute paths")
    common = ("--deployment", str(deployment_path), "--queue-policy", str(policy_path), "--apply")
    watcher = WorkerUnit(
        description="Anamnema marked-capture queue watcher",
        command=(str(executable), "capture-events", "drain", *common, "--watch"),
        cwd=deployment.cwd,
        scope=scope,
        account=account,
    )
    hooks = tuple(
        HookBinding(
            event=native,
            command=(str(executable), "capture-events", "hook", *common, "--event", event),
            timeout_seconds=policy.hook_timeout_seconds,
        )
        for event, native in policy.native_events.items()
    )
    fingerprint = digest(
        {
            "deployment": deployment.model_dump(mode="json"),
            "queue": policy.model_dump(mode="json"),
            "watcher": watcher.model_dump(mode="json"),
            "service": service_policy.model_dump(mode="json"),
        }
    )
    return CaptureInstallation(
        fingerprint, watcher, render_worker_unit(watcher, service_policy), hooks
    )
