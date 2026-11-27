"""PreToolUse: bring the lessons that fit an action into the session before it runs.

Harmless reads are skipped. For any other tool the hook asks the running memory service
for every admitted lesson that fits the action, with no fixed count: a cap of two had kept
fitting lessons away (lesson-delivery fix, 2026-11-27). The hook byte budget bounds the
context. Like recall, it uses one local HTTP call and the same deployment file; an outage,
a refusal or an empty answer adds no context and never blocks the action.
"""

import argparse
import json
import sys
from importlib.resources import files
from pathlib import Path
from typing import Any, Literal, Self, TextIO, cast

from pydantic import Field, ValidationError, model_validator

from anamnema import recall_hook
from anamnema.hook_merge import HookBinding
from anamnema.recall_hook import Fetch, RecallDeployment, recall_deployment
from anamnema.service_config import load_service
from anamnema.setup_config import Nonempty, SetupError, StrictModel


class ActionHookPolicy(StrictModel):
    native_event: Literal["PreToolUse"]
    native_fields: dict[Literal["event", "tool", "input", "cwd"], Nonempty]
    skip_tools: tuple[Nonempty, ...]
    action_fields: tuple[Nonempty, ...] = Field(min_length=1)
    max_input_bytes: int = Field(gt=0)
    max_action_characters: int = Field(gt=0)
    request_timeout_seconds: float = Field(gt=0, allow_inf_nan=False)
    hook_timeout_seconds: float = Field(gt=0, allow_inf_nan=False)
    max_response_bytes: int = Field(gt=0)
    max_route_characters: int = Field(gt=0)
    max_context_bytes: int = Field(gt=0)
    header: Nonempty

    @model_validator(mode="after")
    def request_inside_hook(self) -> Self:
        if self.request_timeout_seconds >= self.hook_timeout_seconds:
            raise ValueError("the service request must end before the hook timeout")
        return self


def action_hook_policy(path: Path | None = None) -> ActionHookPolicy:
    try:
        text = (
            path.read_text(encoding="utf-8")
            if path
            else files("anamnema").joinpath("config/action_hook.json").read_text(encoding="utf-8")
        )
        return ActionHookPolicy.model_validate_json(text)
    except (OSError, UnicodeError, ValidationError) as exc:
        raise SetupError(f"invalid action hook policy ({type(exc).__name__})") from exc


def action_binding(
    executable: Path, deployment: Path, policy: ActionHookPolicy, policy_file: Path | None = None
) -> HookBinding:
    """The one PreToolUse group the installer owns for lessons before an action."""
    if not executable.is_absolute() or not deployment.is_absolute():
        raise SetupError("action hook paths must be absolute")
    extra = ("--action-policy", str(policy_file)) if policy_file else ()
    return HookBinding(
        event=policy.native_event,
        command=(str(executable), "action-hook", "--deployment", str(deployment), *extra),
        timeout_seconds=policy.hook_timeout_seconds,
    )


def action_of(
    payload: object, deployment: RecallDeployment, policy: ActionHookPolicy
) -> str | None:
    """The action text to match lessons against, or None for a skipped tool or project."""
    if not isinstance(payload, dict):
        raise SetupError("Claude hook input must be an object")
    data = cast(dict[str, Any], payload)
    fields = policy.native_fields
    if data.get(fields["event"]) != policy.native_event:
        raise SetupError("Claude hook event does not match the action handler")
    if data.get(fields["cwd"]) != str(deployment.cwd):
        return None
    tool = data.get(fields["tool"])
    if not isinstance(tool, str) or not tool:
        raise SetupError("PreToolUse requires a tool name")
    if tool in policy.skip_tools:
        return None
    arguments = data.get(fields["input"])
    values = cast(dict[str, Any], arguments) if isinstance(arguments, dict) else {}
    parts = [
        f"{name}: {values[name]}"
        for name in policy.action_fields
        if isinstance(values.get(name), str) and values[name].strip()
    ]
    return "\n".join([tool, *parts])[: policy.max_action_characters]


def _clip(text: object, limit: int) -> str:
    flat = " ".join(str(text).split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


def render(result: dict[str, Any], policy: ActionHookPolicy) -> str:
    """Every fitting lesson the byte budget holds, nearest first; empty when none fits."""
    lessons = cast(list[dict[str, Any]], result.get("lessons") or [])
    blocks: list[str] = []
    used = len(policy.header.encode("utf-8"))
    for lesson in lessons:
        statement = _clip(lesson.get("statement", ""), policy.max_route_characters)
        lines = [f"- [lesson, {lesson.get('severity', 'normal')}] {statement} [{lesson.get('id')}]"]
        for route in cast(list[dict[str, Any]], lesson.get("routes") or []):
            mark = "EXCEPTION " if route.get("exception") is True else ""
            lines.append(
                f"  - {mark}if {_clip(route.get('condition', ''), policy.max_route_characters)}: "
                f"{_clip(route.get('correction', ''), policy.max_route_characters)}"
            )
        block = "\n".join(lines)
        size = len(block.encode("utf-8")) + 1
        if used + size > policy.max_context_bytes:
            break
        blocks.append(block)
        used += size
    return "\n".join([policy.header, *blocks]) if blocks else ""


def run_action(
    stream: TextIO,
    deployment: RecallDeployment,
    policy: ActionHookPolicy,
    ask: Fetch = recall_hook.fetch,
) -> dict[str, Any]:
    """Native output: context, nothing, or a visible non-blocking notice on failure."""
    raw = stream.read(policy.max_input_bytes + 1)
    try:
        if len(raw.encode("utf-8")) > policy.max_input_bytes:
            raise SetupError("Claude hook input exceeds configured bound")
        action = action_of(json.loads(raw), deployment, policy)
        if action is None:
            return {}
        service = load_service(deployment.service_config)
        url = f"http://{service.bind.host}:{service.bind.port}{service.advice_path}"
        body: dict[str, Any] = {"action": action, "cwd": str(deployment.cwd)}
        bearer = recall_hook.service_bearer(deployment, service)
        context = render(ask(url, body, bearer, policy), policy)
    except (SetupError, json.JSONDecodeError) as exc:
        print(f"action ERROR: {exc}", file=sys.stderr)
        return {"systemMessage": f"Lessons before this action skipped: {exc}"}
    print("action SUCCESS", file=sys.stderr)
    if not context:
        return {}
    return {
        "hookSpecificOutput": {"hookEventName": policy.native_event, "additionalContext": context}
    }


def add_action_hook(parser: argparse.ArgumentParser) -> None:
    """``anamnema action-hook --deployment FILE [--action-policy FILE]``, native JSON on stdin."""
    parser.add_argument("--deployment", type=Path, required=True)
    parser.add_argument("--action-policy", type=Path)


def run_action_hook(args: argparse.Namespace, stream: TextIO) -> dict[str, Any]:
    """Never raises and never blocks: a bad deployment is a visible notice, like an outage."""
    try:
        policy = action_hook_policy(args.action_policy)
        deployment = recall_deployment(args.deployment)
    except SetupError as exc:
        return {"systemMessage": f"Lessons before this action skipped: {exc}"}
    return run_action(stream, deployment, policy)
