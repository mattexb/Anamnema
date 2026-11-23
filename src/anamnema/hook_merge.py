"""Pure non-destructive Claude hook reconciliation with explicit prior ownership receipts."""

import copy
import shlex
from dataclasses import dataclass
from typing import Self

from pydantic import Field, JsonValue, model_validator

from anamnema.archive_models import canonical
from anamnema.setup_config import Nonempty, SetupError, StrictModel


class HookBinding(StrictModel):
    """One installer-owned matcher group; argv is quoted rather than interpolated into a shell."""

    event: Nonempty
    command: tuple[Nonempty, ...] = Field(min_length=1)
    timeout_seconds: float = Field(gt=0, allow_inf_nan=False)
    matcher: str | None = None

    @model_validator(mode="after")
    def readable_command(self) -> Self:
        if any(c in argument for argument in self.command for c in ("\n", "\r", "\x00")):
            raise ValueError("hook command arguments must fit single lines")
        return self

    def group(self) -> dict[str, JsonValue]:
        """Use only documented native settings keys, never an invented ownership marker."""
        group: dict[str, JsonValue] = {
            "hooks": [
                {
                    "type": "command",
                    "command": shlex.join(self.command),
                    "timeout": self.timeout_seconds,
                }
            ]
        }
        if self.matcher is not None:
            group["matcher"] = self.matcher
        return group


@dataclass(frozen=True)
class HookMerge:
    """A proposed settings document plus the exact groups the installer would own."""

    settings: dict[str, JsonValue]
    bindings: tuple[HookBinding, ...]
    changed: bool


def merge_hooks(
    settings: dict[str, JsonValue],
    previous: tuple[HookBinding, ...],
    desired: tuple[HookBinding, ...],
) -> HookMerge:
    """Remove only unchanged owned groups, retain all foreign settings, and refuse drift."""
    canonical(settings)
    if settings.get("disableAllHooks") is True and desired:
        raise SetupError("Claude hooks are explicitly disabled; no settings changed")
    _unique(previous)
    _unique(desired)
    original = copy.deepcopy(settings)
    proposed = copy.deepcopy(settings)
    native = proposed.get("hooks", {})
    if not isinstance(native, dict):
        raise SetupError("Claude hooks setting is not an object")
    for binding in previous:
        groups = _groups(native, binding.event)
        expected = binding.group()
        matches = [index for index, group in enumerate(groups) if group == expected]
        if len(matches) != 1:
            raise SetupError("an owned Claude hook changed, disappeared or was duplicated")
        del groups[matches[0]]
        if groups:
            native[binding.event] = groups
        else:
            native.pop(binding.event, None)
    for binding in desired:
        groups = _groups(native, binding.event)
        group = binding.group()
        if group in groups:
            raise SetupError("desired hook already exists outside the ownership receipt")
        native[binding.event] = [*groups, group]
    if native or "hooks" in original:
        proposed["hooks"] = native
    return HookMerge(proposed, desired, proposed != original)


def _groups(native: dict[str, JsonValue], event: str) -> list[JsonValue]:
    groups = native.get(event, [])
    if not isinstance(groups, list) or any(not isinstance(group, dict) for group in groups):
        raise SetupError("Claude hook event does not contain a matcher-group array")
    return list(groups)


def _unique(bindings: tuple[HookBinding, ...]) -> None:
    keys = [canonical({"event": b.event, "group": b.group()}) for b in bindings]
    if len(keys) != len(set(keys)):
        raise SetupError("installer hook bindings repeat")
