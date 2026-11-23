"""Real temporary child processes test the transport, not native Claude compatibility."""

import hashlib
import json
import sys

import pytest
from pydantic import ValidationError

from anamnema.judge_config import JudgeConfig, JudgePolicy, judge_policy
from anamnema.native_judge import NativeJudge
from anamnema.setup_config import SetupError, StrictModel
from anamnema.worker_registry import RegistryConfig


class Answer(StrictModel):
    agreed: bool


WORKER = """import hashlib, json, pathlib, sys, time
root = pathlib.Path.cwd()
args = sys.argv[1:]
(root / "args.json").write_text(json.dumps(args), encoding="utf-8")
request = json.loads((root / "reply.json").read_text(encoding="utf-8"))
if request == "duplex":
    sys.stderr.write("x" * 262144)
    sys.stderr.flush()
incoming = sys.stdin.buffer.read()
(root / "input.sha256").write_text(hashlib.sha256(incoming).hexdigest(), encoding="utf-8")
if request == "sleep":
    time.sleep(60)
elif request == "overflow":
    print("x" * 100000)
elif request == "duplex":
    print(json.dumps({"is_error": False, "subtype": "success", "modelUsage": {"fixture": {}},
                      "structured_output": {"agreed": True}}))
else:
    print(json.dumps(request))
"""


def success(**changes):
    return {
        "is_error": False,
        "subtype": "success",
        "modelUsage": {"fixture-model": {}},
        "structured_output": {"agreed": True},
        **changes,
    }


@pytest.fixture
def judge(tmp_path):
    executable = tmp_path / "fixture-worker"
    executable.write_text(f"#!{sys.executable}\n" + WORKER, encoding="utf-8")
    executable.chmod(0o700)
    config = JudgeConfig(executable=executable, cwd=tmp_path, model="explicit-fixture")
    policy = judge_policy().model_copy(update={"timeout_seconds": 2.0})
    return NativeJudge(config, policy)


def reply(judge, value):
    (judge.config.cwd / "reply.json").write_text(json.dumps(value), encoding="utf-8")


def test_real_worker_stdin_privacy_security_floor_and_reported_model(judge):
    reply(judge, success())
    result = judge.judge("synthetic evidence", Answer)
    assert result.answer.agreed is True and result.models == ("fixture-model",)
    assert result.identity != judge.identity
    args = json.loads((judge.config.cwd / "args.json").read_text(encoding="utf-8"))
    assert "synthetic evidence" not in json.dumps(args)
    assert args[-2] == "--json-schema"
    assert (judge.config.cwd / "input.sha256").read_text(encoding="utf-8") == hashlib.sha256(
        b"synthetic evidence"
    ).hexdigest()
    assert args[:2] == ["--model", "explicit-fixture"]
    assert json.loads(args[args.index("--json-schema") + 1]) == Answer.model_json_schema()
    assert args[2 : 2 + len(judge.policy.worker_arguments)] == list(judge.policy.worker_arguments)


def test_large_stdin_and_stderr_are_transferred_without_pipe_deadlock(judge):
    reply(judge, "duplex")
    prompt = "synthetic evidence " * 8192
    judge.policy = judge.policy.model_copy(update={"max_input_bytes": len(prompt.encode())})
    assert judge.judge(prompt, Answer).answer.agreed is True
    assert (judge.config.cwd / "input.sha256").read_text(encoding="utf-8") == hashlib.sha256(
        prompt.encode()
    ).hexdigest()


def test_registered_judge_does_not_register_or_remove_its_working_directory(judge):
    registry = RegistryConfig(
        records_dir=judge.config.cwd / "records", folder_roots=(judge.config.cwd / "inputs",)
    )
    judge.config = judge.config.model_copy(update={"registry": registry})
    judge.policy = judge.policy.model_copy(update={"timeout_seconds": 5.0})
    reply(judge, success())
    assert judge.judge("synthetic evidence", Answer).answer.agreed
    assert judge.config.cwd.is_dir() and (judge.config.cwd / "reply.json").is_file()
    assert not list(registry.records_dir.glob("*.json"))


@pytest.mark.parametrize(
    "value",
    [
        [],
        success(is_error="false"),
        success(is_error=0),
        success(is_error=True),
        success(subtype="error_during_execution"),
        success(modelUsage={}),
        success(structured_output={"agreed": "false"}),
        success(structured_output=None),
        {"result": '{"agreed":true}'},
    ],
)
def test_error_free_text_or_malformed_envelope_never_returns_a_judgement(judge, value):
    reply(judge, value)
    with pytest.raises(SetupError, match="invalid structured success envelope"):
        judge.judge("synthetic evidence", Answer)


def test_input_bound_prevents_starting_any_child(judge):
    judge.policy = judge.policy.model_copy(update={"max_input_bytes": 3})
    for prompt in ("", "èè"):
        with pytest.raises(SetupError, match="byte limit"):
            judge.judge(prompt, Answer)
    assert not (judge.config.cwd / "args.json").exists()


@pytest.mark.parametrize("behavior,message", [("sleep", "timed out"), ("overflow", "bound")])
def test_worker_deadline_and_output_bound_are_visible(judge, behavior, message):
    reply(judge, behavior)
    judge.policy = judge.policy.model_copy(
        update={"timeout_seconds": 0.5, "max_output_bytes": 4096}
    )
    with pytest.raises(SetupError, match=message):
        judge.judge("synthetic evidence", Answer)


@pytest.mark.parametrize(
    "old,new",
    [
        ("--tools", "--allowedTools"),
        ("dontAsk", "bypassPermissions"),
        ('{"disableAllHooks":true}', "{}"),
        ('{"mcpServers":{}}', '{"mcpServers":{"foreign":{}}}'),
        ("--no-session-persistence", "--resume"),
    ],
)
def test_policy_overrides_cannot_weaken_worker_floor(old, new):
    data = judge_policy().model_dump(mode="json")
    data["worker_arguments"] = [new if flag == old else flag for flag in data["worker_arguments"]]
    with pytest.raises(ValidationError):
        JudgePolicy.model_validate_json(json.dumps(data))
