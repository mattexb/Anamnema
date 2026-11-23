"""Actual stacks and bundles: a rebuilt held page is replaced as a new required read."""

import argparse
import io
import json
from pathlib import Path
from typing import Any

import pytest
from test_briefing_stack import bundle
from test_briefing_stack import stack as stack
from test_continuity import continuity as continuity

from anamnema.briefing_refresh import (
    RefreshDeployment,
    add_briefing_refresh,
    held_briefings,
    load_replacements,
    record_replacements,
    refresh_session,
    run_briefing_refresh,
)
from anamnema.setup_config import SetupError


def _deployment(stack: Any, tmp_path: Path) -> RefreshDeployment:
    config = tmp_path / "continuity.json"
    config.write_text(stack.continuity.config.model_dump_json(), encoding="utf-8")
    return RefreshDeployment(
        instance="example",
        briefing_root=stack.bundles.root,
        stack_root=stack.root,
        state_directory=tmp_path / "nightly",
        continuity_config=config,
    )


def test_a_replaced_held_page_is_delivered_new_and_the_old_dropped(
    stack: Any, tmp_path: Path
) -> None:
    old = bundle(stack)
    new = bundle(stack, "Rebuilt synthetic recall.\n")
    stack.deliver("session-one", old)
    assert held_briefings(stack.root, "example") == [old]
    deployment = _deployment(stack, tmp_path)
    assert refresh_session(deployment, "session-one") == []
    deployment.state_directory.mkdir(mode=0o700)
    assert record_replacements(deployment.state_directory, "example", {old: new}, 4096) == 1
    delivered = refresh_session(deployment, "session-one")
    assert [item["briefing"] for item in delivered] == [new]
    assert held_briefings(stack.root, "example") == [new]
    assert refresh_session(deployment, "session-one") == []


def test_replacements_chain_and_refuse_a_cycle_or_another_assistant(tmp_path: Path) -> None:
    folder = tmp_path / "nightly"
    folder.mkdir(mode=0o700)
    record_replacements(folder, "example", {"a": "b"}, 4096)
    record_replacements(folder, "example", {"b": "c"}, 4096)
    assert load_replacements(folder, "example", 4096).current("a") == "c"
    with pytest.raises(SetupError, match="cycle"):
        record_replacements(folder, "example", {"c": "a"}, 4096)
    with pytest.raises(SetupError, match="another assistant"):
        load_replacements(folder, "other", 4096)


def test_the_prompt_hook_returns_context_or_a_notice_never_a_block(
    stack: Any, tmp_path: Path
) -> None:
    old, new = bundle(stack), bundle(stack, "Rebuilt synthetic recall.\n")
    stack.deliver("session-one", old)
    deployment = _deployment(stack, tmp_path)
    deployment.state_directory.mkdir(mode=0o700)
    record_replacements(deployment.state_directory, "example", {old: new}, 4096)
    file = tmp_path / "refresh.json"
    file.write_text(deployment.model_dump_json(), encoding="utf-8")
    parser = argparse.ArgumentParser()
    add_briefing_refresh(parser)
    args = parser.parse_args(["--deployment", str(file)])
    payload = json.dumps({"hook_event_name": "UserPromptSubmit", "session_id": "session-one"})
    out = run_briefing_refresh(args, io.StringIO(payload))
    assert out["hookSpecificOutput"]["hookEventName"] == "UserPromptSubmit"
    assert out["hookSpecificOutput"]["additionalContext"]
    assert run_briefing_refresh(args, io.StringIO(payload)) == {}
    broken = run_briefing_refresh(args, io.StringIO("{}"))
    assert broken["systemMessage"].startswith("Briefing refresh skipped")
