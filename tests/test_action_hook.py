"""The pre-action hook brings every fitting lesson, skips harmless reads and never blocks."""

import io
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from anamnema import recall_hook
from anamnema.action_hook import action_binding, action_hook_policy, action_of, render, run_action
from anamnema.recall_hook import RecallDeployment
from anamnema.setup_config import SetupError


def _lesson(identity: str, statement: str, exception: bool = False) -> dict[str, Any]:
    return {
        "id": identity,
        "similarity": 0.9,
        "statement": statement,
        "severity": "normal",
        "routes": [
            {"condition": "Before running the gate", "correction": "Run it in the background"},
            {"condition": "A single test file", "correction": "Inline", "exception": exception},
        ],
    }


LESSONS = {"lessons": [_lesson(f"lesson-{i}", f"Rule {i}", exception=i == 0) for i in range(5)]}


def _deployment(tmp_path: Path) -> RecallDeployment:
    return RecallDeployment(instance="owner", cwd=tmp_path, config=tmp_path / "setup.json")


def _payload(tmp_path: Path, tool: str, arguments: dict[str, Any], **extra: Any) -> io.StringIO:
    data = {
        "hook_event_name": "PreToolUse",
        "tool_name": tool,
        "tool_input": arguments,
        "cwd": str(tmp_path),
        **extra,
    }
    return io.StringIO(json.dumps(data))


@pytest.fixture
def bearer(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(recall_hook, "_bearer", lambda _d, _s: "fixture-bearer")


def test_every_fitting_lesson_arrives_not_a_fixed_two(tmp_path: Path, bearer: None) -> None:
    # A cap of two had kept fitting lessons away (lesson-delivery fix, 2026-11-27).
    asked: list[dict[str, Any]] = []

    def ask(url: str, body: dict[str, Any], token: str, _policy: Any) -> dict[str, Any]:
        asked.append({"url": url, "body": body, "token": token})
        return LESSONS

    stream = _payload(tmp_path, "Bash", {"command": "./check", "description": "run the gate"})
    out = run_action(stream, _deployment(tmp_path), action_hook_policy(), ask)
    context = out["hookSpecificOutput"]["additionalContext"]
    assert out["hookSpecificOutput"]["hookEventName"] == "PreToolUse"
    assert all(f"Rule {i} [lesson-{i}]" in context for i in range(5))
    assert "  - if Before running the gate: Run it in the background" in context
    assert "  - EXCEPTION if A single test file: Inline" in context
    assert asked == [
        {
            "url": "http://127.0.0.1:8095/v1/advice",
            "body": {
                "action": "Bash\ncommand: ./check\ndescription: run the gate",
                "cwd": str(tmp_path),
            },
            "token": "fixture-bearer",
        }
    ]


@pytest.mark.parametrize(("tool", "cwd_ok"), [("Read", True), ("Grep", True), ("Bash", False)])
def test_harmless_reads_and_other_projects_ask_nothing(
    tmp_path: Path, bearer: None, tool: str, cwd_ok: bool
) -> None:
    def ask(*_args: Any) -> dict[str, Any]:
        raise AssertionError("no lesson search expected")

    arguments = {"command": "./check", "file_path": "/x"}
    stream = (
        _payload(tmp_path, tool, arguments)
        if cwd_ok
        else _payload(tmp_path, tool, arguments, cwd="/elsewhere")
    )
    assert run_action(stream, _deployment(tmp_path), action_hook_policy(), ask) == {}


def test_action_text_names_the_tool_and_only_configured_text_fields(tmp_path: Path) -> None:
    payload = {
        "hook_event_name": "PreToolUse",
        "tool_name": "Edit",
        "tool_input": {"file_path": "/src/a.py", "replace_all": True, "new_string": "x" * 9000},
        "cwd": str(tmp_path),
    }
    policy = action_hook_policy()
    action = action_of(payload, _deployment(tmp_path), policy)
    assert action is not None and action.startswith("Edit\nfile_path: /src/a.py\nnew_string: x")
    assert "replace_all" not in action and len(action) == policy.max_action_characters
    with pytest.raises(SetupError, match="tool name"):
        action_of({**payload, "tool_name": ""}, _deployment(tmp_path), policy)
    with pytest.raises(SetupError, match="does not match"):
        action_of({**payload, "hook_event_name": "Stop"}, _deployment(tmp_path), policy)


def test_an_outage_is_a_visible_notice_never_a_block(tmp_path: Path, bearer: None) -> None:
    def ask(*_args: Any) -> dict[str, Any]:
        raise SetupError("memory service unreachable (URLError)")

    stream = _payload(tmp_path, "Bash", {"command": "./check"})
    out = run_action(stream, _deployment(tmp_path), action_hook_policy(), ask)
    assert out == {
        "systemMessage": "Lessons before this action skipped: memory service unreachable (URLError)"
    }
    assert "decision" not in out and "hookSpecificOutput" not in out
    big = io.StringIO("x" * (action_hook_policy().max_input_bytes + 1))
    assert (
        "exceeds" in run_action(big, _deployment(tmp_path), action_hook_policy())["systemMessage"]
    )


def test_empty_answer_adds_nothing_and_context_stops_at_its_byte_bound(tmp_path: Path) -> None:
    policy = action_hook_policy().model_copy(update={"max_context_bytes": 900})
    many = {"lessons": [_lesson(f"lesson-{i}", "word " * 30) for i in range(20)]}
    context = render(many, policy)
    assert len(context.encode("utf-8")) <= 900 and 1 <= context.count("\n- [lesson") < 20
    assert render({"lessons": []}, policy) == ""


def test_binding_is_one_pre_tool_group_inside_the_three_to_five_second_bar(tmp_path: Path) -> None:
    policy = action_hook_policy()
    binding = action_binding(Path("/opt/anamnema"), tmp_path / "recall.json", policy)
    assert binding.event == "PreToolUse" and binding.timeout_seconds <= 5
    assert binding.command[:2] == ("/opt/anamnema", "action-hook")
    with pytest.raises(SetupError, match="absolute"):
        action_binding(Path("anamnema"), tmp_path / "recall.json", policy)


def test_installed_command_with_a_bad_deployment_is_a_notice(tmp_path: Path) -> None:
    payload = json.dumps({"hook_event_name": "PreToolUse", "tool_name": "Bash"})
    done = subprocess.run(  # noqa: S603 - the interpreter running this test
        [sys.executable, "-m", "anamnema", "action-hook", "--deployment", str(tmp_path / "no")],
        input=payload,
        capture_output=True,
        text=True,
        check=True,
    )
    assert json.loads(done.stdout)["systemMessage"].startswith("Lessons before this action skipped")
