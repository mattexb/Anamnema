"""Refresh held briefings: the nightly rebuild records replacements, the next prompt delivers them.

A held page whose bundle was rebuilt on changed notes is replaced in the session through the
stack's own ``deliver`` (a new required read) and ``drop`` of the old page, so read debt and
budget follow the existing rules. The refresh runs on UserPromptSubmit, where no other handler
takes the continuity lock; SessionStart and Stop already hold it for their own reads.
"""

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Self, TextIO, cast

from pydantic import Field, ValidationError, model_validator

from anamnema.archive_models import canonical
from anamnema.briefing_files import BriefFiles, brief_file_policy
from anamnema.briefing_policy import briefing_policy
from anamnema.briefing_stack import BriefStack, StackState, stack_policy
from anamnema.continuity import Continuity
from anamnema.continuity_config import continuity_config, continuity_policy
from anamnema.private_files import PrivateFiles
from anamnema.setup_config import Nonempty, SetupError, StrictModel

REPLACEMENTS = "briefing_replacements.json"
FILES = PrivateFiles(0o700, 0o600, ".briefing-refresh-")


class Replacements(StrictModel):
    """Old bundle identity -> the newer ready bundle of the same focus."""

    instance: Nonempty
    replaced: dict[str, str] = Field(default_factory=dict[str, str])

    def current(self, identity: str) -> str:
        seen = {identity}
        while identity in self.replaced:
            identity = self.replaced[identity]
            if identity in seen:
                raise SetupError("briefing replacements form a cycle")
            seen.add(identity)
        return identity


class RefreshDeployment(StrictModel):
    instance: Nonempty
    briefing_root: Path
    stack_root: Path
    state_directory: Path
    continuity_config: Path
    max_replacements_bytes: int = Field(default=1048576, gt=0)

    @model_validator(mode="after")
    def explicit_paths(self) -> Self:
        paths = (self.briefing_root, self.stack_root, self.state_directory, self.continuity_config)
        if any(not path.is_absolute() for path in paths):
            raise ValueError("briefing refresh paths must be absolute")
        return self


def load_replacements(state_directory: Path, instance: str, limit: int) -> Replacements:
    try:
        raw = FILES.read(state_directory / REPLACEMENTS, limit)
    except FileNotFoundError:
        return Replacements(instance=instance)
    try:
        found = Replacements.model_validate_json(raw)
    except ValidationError as exc:
        raise SetupError("briefing replacement record is invalid") from exc
    if found.instance != instance:
        raise SetupError("briefing replacement record belongs to another assistant")
    return found


def record_replacements(
    state_directory: Path, instance: str, new: dict[str, str], limit: int
) -> int:
    """Merge newly rebuilt identities; the caller holds the nightly lock."""
    if not new:
        return 0
    current = load_replacements(state_directory, instance, limit)
    merged = current.model_copy(update={"replaced": {**current.replaced, **new}})
    merged.current(next(iter(new)))
    data = canonical(merged.model_dump(mode="json"))
    if len(data) > limit:
        raise SetupError("briefing replacement record exceeds configured bound")
    FILES.save(state_directory / REPLACEMENTS, data)
    return len(new)


def held_briefings(stack_root: Path, instance: str) -> list[str]:
    """Every bundle some session of this assistant still holds, read without locking."""
    policy = stack_policy()
    if not stack_root.is_dir():
        return []
    held: set[str] = set()
    for state_file in sorted(stack_root.glob(f"*/{policy.state_file}")):
        try:
            state = StackState.model_validate_json(FILES.read(state_file, policy.max_state_bytes))
        except OSError, ValidationError, SetupError:
            continue
        if state.instance == instance:
            held |= {item.briefing for item in state.held.values() if not item.dropped}
    return sorted(held)


def refresh_session(deployment: RefreshDeployment, session: str) -> list[dict[str, Any]]:
    """Deliver each held page's replacement, then drop the old page; nothing to do is cheap."""
    replacements = load_replacements(
        deployment.state_directory, deployment.instance, deployment.max_replacements_bytes
    )
    if not replacements.replaced:
        return []
    config = continuity_config(deployment.continuity_config)
    if config.instance != deployment.instance:
        raise SetupError("briefing refresh assistant differs from the continuity deployment")
    bundles = BriefFiles(deployment.briefing_root, brief_file_policy())
    stack = BriefStack(
        deployment.stack_root,
        bundles,
        Continuity(config, continuity_policy()),
        briefing_policy(),
        stack_policy(),
    )
    held = cast(dict[str, dict[str, Any]], stack.status(session)["held"])
    delivered: list[dict[str, Any]] = []
    for item in held.values():
        old, page = str(item["briefing"]), int(item["page"])
        new = replacements.current(old)
        if item.get("dropped") or new == old:
            continue
        _manifest, pages = bundles.read(deployment.instance, new)
        result = stack.deliver(session, new, min(page, len(pages)))
        stack.drop(session, old, page)
        delivered.append(result)
    return delivered


def add_briefing_refresh(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--deployment", type=Path, required=True)


def run_briefing_refresh(args: argparse.Namespace, stream: TextIO) -> dict[str, Any]:
    """UserPromptSubmit handler; a failure is a visible notice and never blocks the prompt."""
    try:
        deployment = RefreshDeployment.model_validate_json(
            args.deployment.read_text(encoding="utf-8")
        )
        payload: object = json.loads(stream.read(1048577)[:1048576])
        session = (
            cast(dict[str, Any], payload).get("session_id") if isinstance(payload, dict) else None
        )
        if not isinstance(session, str) or not session:
            raise SetupError("UserPromptSubmit requires a session id")
        delivered = refresh_session(deployment, session)
    except (OSError, ValidationError, SetupError, json.JSONDecodeError) as exc:
        print(f"briefing refresh ERROR: {type(exc).__name__}", file=sys.stderr)
        return {"systemMessage": f"Briefing refresh skipped: {exc}"}
    if not delivered:
        return {}
    context = "\n\n".join(str(item["required_read"]) for item in delivered)
    return {
        "hookSpecificOutput": {"hookEventName": "UserPromptSubmit", "additionalContext": context}
    }
