"""Generated capture registration invokes the actual CLI without live settings or services."""

import io
import json
from pathlib import Path

import pytest

from anamnema.capture_install import capture_installation
from anamnema.capture_queue import CaptureQueue
from anamnema.capture_queue_config import CaptureDeployment, capture_queue_policy
from anamnema.cli import main
from anamnema.setup_config import SetupError, load_config
from anamnema.worker_unit import worker_unit_policy


@pytest.fixture
def installed_plan(tmp_path):
    setup = tmp_path / "setup.json"
    setup.write_text(load_config().model_dump_json(), encoding="utf-8")
    transcript = tmp_path / "transcripts"
    transcript.mkdir()
    deployment = CaptureDeployment(
        instance="fixture",
        state_root=tmp_path / "queue",
        cwd=tmp_path,
        transcript_roots=(transcript,),
        config=setup,
    )
    policy = capture_queue_policy()
    deployment_path, policy_path = tmp_path / "deployment.json", tmp_path / "policy.json"
    deployment_path.write_text(deployment.model_dump_json(), encoding="utf-8")
    policy_path.write_text(policy.model_dump_json(), encoding="utf-8")
    plan = capture_installation(
        tmp_path / "app executable",
        deployment_path,
        policy_path,
        deployment,
        policy,
        worker_unit_policy(),
        scope="user",
        account=None,
    )
    return plan, deployment, policy, deployment_path, policy_path


def test_all_native_hook_commands_reach_real_cli_without_opening_stores(
    installed_plan, monkeypatch, capsys
):
    plan, deployment, policy, _, _ = installed_plan
    assert {hook.event for hook in plan.hooks} == set(policy.native_events.values())
    for hook in plan.hooks:
        payload = {
            "hook_event_name": hook.event,
            "session_id": "fixture-session",
            "transcript_path": str(deployment.transcript_roots[0] / "fixture.jsonl"),
            "cwd": str(deployment.cwd),
            "last_assistant_message": "No explicit self-note.",
        }
        # The other lifecycle events observe an empty, regular synthetic transcript.
        (deployment.transcript_roots[0] / "fixture.jsonl").write_text("", encoding="utf-8")
        monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))
        assert main(list(hook.command[1:])) == 0
        result = json.loads(capsys.readouterr().out)
        assert result == {}
        assert hook.timeout_seconds == policy.hook_timeout_seconds
        if hook.event == policy.native_events["stop"]:
            assert not deployment.state_root.exists()
    pending = CaptureQueue(deployment, policy).inspect().pending
    assert {job.event for job in pending} == {"precompact", "end"}
    assert all(job.selection.empty for job in pending)


def test_watcher_command_has_same_config_and_real_dry_run_does_not_create_state(
    installed_plan, capsys
):
    plan, deployment, _, deployment_path, policy_path = installed_plan
    command = list(plan.watcher.command[1:])
    assert command[command.index("--deployment") + 1] == str(deployment_path)
    assert command[command.index("--queue-policy") + 1] == str(policy_path)
    assert "--watch" in command and "KillMode=control-group" in plan.unit
    command[command.index("--apply")] = "--dry-run"
    assert main(command) == 0
    assert json.loads(capsys.readouterr().out)["mode"] == "dry-run"
    assert not deployment.state_root.exists()


def test_relative_install_coordinate_refuses(installed_plan):
    _, deployment, policy, deployment_path, policy_path = installed_plan
    with pytest.raises(SetupError, match="absolute"):
        capture_installation(
            Path("relative"),
            deployment_path,
            policy_path,
            deployment,
            policy,
            worker_unit_policy(),
            scope="user",
            account=None,
        )
