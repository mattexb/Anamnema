"""UserPromptSubmit: bring the memory's source-linked notes for this prompt into the session.

The hook asks the running memory service, never opens the stores itself, so a prompt costs one
local HTTP call. The bearer comes from the project keyring through the service's own access
selector for this instance. An outage, a refusal or an empty answer adds no context and never
blocks the prompt; an outage is named once in a visible system message.
"""

import argparse
import json
import sys
import urllib.error
import urllib.request
from collections.abc import Callable
from importlib.resources import files
from pathlib import Path
from typing import Any, Literal, Self, TextIO, cast

from pydantic import Field, JsonValue, ValidationError, model_validator

from anamnema.hook_merge import HookBinding
from anamnema.ingest_config import archive_settings
from anamnema.secret_service import SecretService
from anamnema.service_config import ServiceSettings, load_service
from anamnema.setup_config import Nonempty, SetupError, StrictModel, load_config


class RecallPolicy(StrictModel):
    native_event: Literal["UserPromptSubmit"]
    native_fields: dict[Literal["event", "prompt", "cwd"], Nonempty]
    max_input_bytes: int = Field(gt=0)
    min_prompt_characters: int = Field(ge=0)
    max_question_characters: int = Field(gt=0)
    skip_prefixes: tuple[Nonempty, ...] = ()
    request_timeout_seconds: float = Field(gt=0, allow_inf_nan=False)
    hook_timeout_seconds: float = Field(gt=0, allow_inf_nan=False)
    max_response_bytes: int = Field(gt=0)
    max_notes: int = Field(gt=0)
    max_note_characters: int = Field(gt=0)
    max_context_bytes: int = Field(gt=0)
    header: Nonempty

    @model_validator(mode="after")
    def request_inside_hook(self) -> Self:
        if self.request_timeout_seconds >= self.hook_timeout_seconds:
            raise ValueError("the service request must end before the hook timeout")
        return self


class RecallDeployment(StrictModel):
    """Which assistant asks, and the nonsecret files that locate its service and keyring."""

    instance: Nonempty
    cwd: Path
    config: Path
    archive_config: Path | None = None
    service_config: Path | None = None

    @model_validator(mode="after")
    def explicit_paths(self) -> Self:
        paths = [self.cwd, self.config, self.archive_config, self.service_config]
        if any(path is not None and not path.is_absolute() for path in paths):
            raise ValueError("recall deployment paths must be absolute")
        return self


def recall_policy(path: Path | None = None) -> RecallPolicy:
    try:
        text = (
            path.read_text(encoding="utf-8")
            if path
            else files("anamnema").joinpath("config/recall_hook.json").read_text(encoding="utf-8")
        )
        return RecallPolicy.model_validate_json(text)
    except (OSError, UnicodeError, ValidationError) as exc:
        raise SetupError(f"invalid recall hook policy ({type(exc).__name__})") from exc


def recall_deployment(path: Path) -> RecallDeployment:
    try:
        return RecallDeployment.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValidationError) as exc:
        raise SetupError(f"invalid recall deployment ({type(exc).__name__})") from exc


def recall_binding(
    executable: Path, deployment: Path, policy: RecallPolicy, policy_file: Path | None = None
) -> HookBinding:
    """The one UserPromptSubmit group the installer owns for recall."""
    if not executable.is_absolute() or not deployment.is_absolute():
        raise SetupError("recall hook paths must be absolute")
    extra = ("--recall-policy", str(policy_file)) if policy_file else ()
    return HookBinding(
        event=policy.native_event,
        command=(str(executable), "recall-hook", "--deployment", str(deployment), *extra),
        timeout_seconds=policy.hook_timeout_seconds,
    )


def question_of(payload: object, deployment: RecallDeployment, policy: RecallPolicy) -> str | None:
    """The prompt to search for, or None when this prompt asks for no recall."""
    if not isinstance(payload, dict):
        raise SetupError("Claude hook input must be an object")
    data = cast(dict[str, Any], payload)
    fields = policy.native_fields
    if data.get(fields["event"]) != policy.native_event:
        raise SetupError("Claude hook event does not match the recall handler")
    if data.get(fields["cwd"]) != str(deployment.cwd):
        return None
    prompt = data.get(fields["prompt"])
    if not isinstance(prompt, str):
        raise SetupError("UserPromptSubmit requires prompt text")
    prompt = prompt.strip()
    if len(prompt) < policy.min_prompt_characters or prompt.startswith(policy.skip_prefixes):
        return None
    return prompt[: policy.max_question_characters]


def render(result: dict[str, Any], policy: RecallPolicy) -> str:
    """Bounded plain lines, newest facts first as ranked; empty when nothing qualifies."""
    notes = cast(list[dict[str, Any]], result.get("notes") or [])
    lines: list[str] = []
    used = len(policy.header.encode("utf-8"))
    for note in notes[: policy.max_notes]:
        text = " ".join(str(note.get("text", "")).split())
        if len(text) > policy.max_note_characters:
            text = text[: policy.max_note_characters - 1] + "…"
        said = note.get("said_at") or "date unknown"
        status = "" if note.get("status") in (None, "active") else f", {note['status']}"
        line = f"- [{note.get('kind', 'note')}{status}] {text} ({said}) [{note.get('id')}]"
        size = len(line.encode("utf-8")) + 1
        if used + size > policy.max_context_bytes:
            break
        lines.append(line)
        used += size
    return "\n".join([policy.header, *lines]) if lines else ""


Fetch = Callable[[str, dict[str, JsonValue], str, RecallPolicy], dict[str, Any]]


def fetch(
    url: str, body: dict[str, JsonValue], bearer: str, policy: RecallPolicy
) -> dict[str, Any]:
    """One local POST; the bearer travels only in the header, never in a log or error."""
    request = urllib.request.Request(  # noqa: S310 - URL built from a validated localhost Endpoint
        url,
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {bearer}"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=policy.request_timeout_seconds) as answer:  # noqa: S310
            raw = answer.read(policy.max_response_bytes + 1)
    except urllib.error.HTTPError as exc:
        raise SetupError(f"memory service refused the search (HTTP {exc.code})") from None
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise SetupError(f"memory service unreachable ({type(exc).__name__})") from None
    if len(raw) > policy.max_response_bytes:
        raise SetupError("memory service answer exceeds configured bound")
    try:
        data = json.loads(raw.decode("utf-8"))
    except UnicodeError, json.JSONDecodeError:
        raise SetupError("memory service answer is not JSON") from None
    if not isinstance(data, dict):
        raise SetupError("memory service answer is not an object")
    return cast(dict[str, Any], data)


def _bearer(deployment: RecallDeployment, service: ServiceSettings) -> str:
    access = [a for a in service.access if a.instance == deployment.instance]
    if len(access) != 1:
        raise SetupError("memory service has no access entry for this instance")
    setup = load_config(deployment.config)
    keyring = SecretService(setup.keyring, archive_settings(deployment.archive_config))
    return keyring.lookup(access[0].entry, access[0].field)


def run_recall(
    stream: TextIO,
    deployment: RecallDeployment,
    policy: RecallPolicy,
    ask: Fetch = fetch,
) -> dict[str, Any]:
    """Native output: context, nothing, or a visible non-blocking notice on failure."""
    raw = stream.read(policy.max_input_bytes + 1)
    try:
        if len(raw.encode("utf-8")) > policy.max_input_bytes:
            raise SetupError("Claude hook input exceeds configured bound")
        question = question_of(json.loads(raw), deployment, policy)
        if question is None:
            return {}
        service = load_service(deployment.service_config)
        url = f"http://{service.bind.host}:{service.bind.port}{service.search_path}"
        context = render(
            ask(url, {"question": question}, _bearer(deployment, service), policy), policy
        )
    except (SetupError, json.JSONDecodeError) as exc:
        print(f"recall ERROR: {exc}", file=sys.stderr)
        return {"systemMessage": f"Memory recall skipped: {exc}"}
    print("recall SUCCESS", file=sys.stderr)
    if not context:
        return {}
    return {
        "hookSpecificOutput": {"hookEventName": policy.native_event, "additionalContext": context}
    }


def add_recall_hook(parser: argparse.ArgumentParser) -> None:
    """``anamnema recall-hook --deployment FILE [--recall-policy FILE]``, native JSON on stdin."""
    parser.add_argument("--deployment", type=Path, required=True)
    parser.add_argument("--recall-policy", type=Path)


def run_recall_hook(args: argparse.Namespace, stream: TextIO) -> dict[str, Any]:
    """Never raises and never blocks: a bad deployment is a visible notice, like an outage."""
    try:
        policy = recall_policy(args.recall_policy)
        deployment = recall_deployment(args.deployment)
    except SetupError as exc:
        return {"systemMessage": f"Memory recall skipped: {exc}"}
    return run_recall(stream, deployment, policy)
