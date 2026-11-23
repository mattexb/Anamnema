"""Actual CLI envelopes with isolated configured state and synthetic native payloads."""

import io
import json

import pytest

from anamnema.cli import main
from anamnema.continuity_config import continuity_policy
from anamnema.substrates.claude_hooks import continuity_event


def args(tmp_path, action):
    identity = tmp_path / "identity.md"
    identity.write_text("Synthetic identity", encoding="utf-8")
    config = tmp_path / "config.json"
    config.write_text(
        json.dumps(
            {
                "instance": "example",
                "state_root": str(tmp_path / "managed"),
                "identity_files": [str(identity)],
            }
        ),
        encoding="utf-8",
    )
    return ["continuity", action, "--continuity-config", str(config)]


def test_cli_exact_ack_and_stop_hook_active_still_holds_debt(tmp_path, monkeypatch, capsys):
    start = args(tmp_path, "start")
    payload = {
        "session_id": "native-session",
        "hook_event_name": "SessionStart",
        "source": "startup",
    }
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))
    assert main(start) == 0
    result = json.loads(capsys.readouterr().out)
    context = result["hookSpecificOutput"]["additionalContext"]
    policy = continuity_policy()
    journal = next((tmp_path / "managed").rglob(policy.state_file))
    held = json.loads(journal.read_text(encoding="utf-8"))
    token = held["reads"][0]["token"]
    assert token not in context
    stop = [*start[:1], "stop", *start[2:]]
    payload.update(hook_event_name="Stop", stop_hook_active=True, last_assistant_message="done")
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))
    assert main(stop) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["decision"] == "block" and token not in result["reason"]
    payload["last_assistant_message"] = policy.read_marker + " " + token
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))
    assert main(stop) == 0
    output = capsys.readouterr()
    assert json.loads(output.out) == {} and "SUCCESS" in output.err


@pytest.mark.parametrize("raw", ["", "[]", "{", '{"hook_event_name":"Wrong"}'])
def test_cli_bad_stop_input_fails_closed_visibly(tmp_path, monkeypatch, capsys, raw):
    command = args(tmp_path, "stop")
    monkeypatch.setattr("sys.stdin", io.StringIO(raw))
    assert main(command) == 0
    output = capsys.readouterr()
    assert json.loads(output.out)["decision"] == "block" and "ERROR" in output.err


@pytest.mark.parametrize("active", [None, "false", 0, 1, [], {}])
def test_native_stop_active_must_be_an_actual_boolean(active):
    payload = {
        "session_id": "session",
        "hook_event_name": "Stop",
        "last_assistant_message": "done",
        "stop_hook_active": active,
    }
    with pytest.raises(ValueError, match="boolean"):
        continuity_event(payload, "stop", continuity_policy())


def test_cli_snapshot_staging_is_not_precompact_or_model_execution(tmp_path, capsys):
    command = args(tmp_path, "snapshot")
    source = tmp_path / "snapshot.md"
    policy = continuity_policy()
    body = (
        f"{policy.snapshot_marker}\nInstance-ID: example\nSession-ID: session\n"
        "\nState: synthetic test."
    )
    source.write_text(body, encoding="utf-8")
    assert main([*command, "--session", "session", "--file", str(source)]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["snapshot"]["applied"] is False
    assert source.read_text(encoding="utf-8") == body


def test_cli_missing_identity_start_warns_without_an_error_only_stop_gate(
    tmp_path, monkeypatch, capsys
):
    command = args(tmp_path, "start")
    (tmp_path / "identity.md").unlink()
    payload = {"session_id": "session", "hook_event_name": "SessionStart", "source": "startup"}
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))
    assert main(command) == 0
    output = capsys.readouterr()
    assert "unavailable" in json.loads(output.out)["hookSpecificOutput"]["additionalContext"]
    payload.update(hook_event_name="Stop", stop_hook_active=False, last_assistant_message="done")
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))
    assert main([command[0], "stop", *command[2:]]) == 0
    assert json.loads(capsys.readouterr().out) == {}


def test_cli_bounded_input_and_invalid_snapshot_are_not_silent(tmp_path, monkeypatch, capsys):
    command = args(tmp_path, "start")
    policy = tmp_path / "policy.json"
    policy.write_text(
        json.dumps({**continuity_policy().model_dump(mode="json"), "max_input_bytes": 8}),
        encoding="utf-8",
    )
    monkeypatch.setattr("sys.stdin", io.StringIO('{"large":"synthetic"}'))
    assert main([*command, "--continuity-policy", str(policy)]) == 0
    assert "bound" in json.loads(capsys.readouterr().out)["systemMessage"]
    command = args(tmp_path, "snapshot")
    source = tmp_path / "invalid.md"
    source.write_text("Not a snapshot", encoding="utf-8")
    assert main([*command, "--session", "session", "--file", str(source)]) == 2
    assert "ERROR" in capsys.readouterr().err
