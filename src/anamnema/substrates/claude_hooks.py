"""Translate the selected Claude lifecycle protocol without changing archive adapters."""

from dataclasses import dataclass
from typing import Any, Literal, cast

from anamnema.continuity_config import ContinuityPolicy
from anamnema.setup_config import SetupError
from anamnema.worker_registry import RegistryConfig
from anamnema.worker_sweep import sweep


@dataclass(frozen=True)
class ClaudeContinuityEvent:
    """The shared lifecycle core sees normalized fields, never native routing identities."""

    session: str
    source: str = ""
    message: str = ""


def continuity_event(
    payload: object,
    event: Literal["start", "stop"],
    policy: ContinuityPolicy,
    *,
    registry: RegistryConfig | None = None,
) -> ClaudeContinuityEvent:
    """Strictly validate used fields while allowing unrelated native protocol metadata.

    A valid start with a worker registry also clears what killed worker parents left behind;
    the sweep never raises and never delays the start beyond its lock wait.
    """
    if not isinstance(payload, dict):
        raise SetupError("Claude hook input must be an object")
    data = cast(dict[str, Any], payload)
    fields = policy.native_fields
    if data.get(fields["event"]) != policy.native_events[event]:
        raise SetupError("Claude hook event does not match invoked handler")
    session = data.get(fields["session"])
    if not isinstance(session, str) or not session:
        raise SetupError("Claude hook requires a session id")
    if event == "start":
        source = data.get(fields["source"])
        if not isinstance(source, str) or not source:
            raise SetupError("Claude SessionStart requires a source")
        if registry is not None:
            sweep(registry)
        return ClaudeContinuityEvent(session, source=source)
    message, active = data.get(fields["message"]), data.get(fields["active"])
    if not isinstance(message, str) or type(active) is not bool:
        raise SetupError("Claude Stop requires message text and a boolean stop_hook_active")
    return ClaudeContinuityEvent(session, message=message)


def continuity_output(
    event: Literal["start", "stop"], context: str, policy: ContinuityPolicy
) -> dict[str, Any]:
    """Only an explicit Stop obligation becomes a blocking native decision."""
    if not context:
        return {}
    if event == "stop":
        return {"decision": "block", "reason": context}
    return {
        "hookSpecificOutput": {
            "hookEventName": policy.native_events[event],
            "additionalContext": context,
        }
    }
