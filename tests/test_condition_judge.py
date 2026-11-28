"""The condition check before an action: policy floor, cache, warm worker, judged routes."""

import json
import subprocess
import sys
import time
from argparse import Namespace

import pytest

from anamnema import condition_judge
from anamnema.condition_judge import (
    ConditionCache,
    ConditionCheck,
    ConditionPolicy,
    Worker,
    condition_check,
    condition_policy,
)
from anamnema.judge_config import JudgeConfig
from anamnema.memory_cli import _condition_check
from anamnema.setup_config import SetupError

# A stand-in model: reads stream-json questions and answers with every id after "holds:".
STAND_IN = """import json, sys, time
for line in sys.stdin:
    text = json.loads(line)["message"]["content"]
    if "slow" in text:
        time.sleep(3)
    ids = text.split("holds:")[-1].split() if "holds:" in text else []
    print(json.dumps({"type": "result", "structured_output": {"holds": ids}}), flush=True)
"""
STASH = {"condition": "Setting changes aside with git stash", "correction": "Fix it"}
MAE = {"condition": "Talking with Mae in the group", "correction": "Be kind"}


def policy(**changes):
    return condition_policy().model_copy(update={"turns": 3, "answer_seconds": 2.0, **changes})


class Model:
    """A recorded model: the labels whose condition contains one of ``holding`` hold."""

    def __init__(self, holding, answer=True):
        self.holding, self.answer, self.prompts = holding, answer, []

    def __call__(self, prompt, seconds, late=None):
        self.prompts.append(prompt)
        if not self.answer:
            return None
        rows = [
            line.split(": ", 1) for line in prompt.splitlines() if line[:1] == "r" and ": " in line
        ]
        return [label for label, text in rows if any(h in text for h in self.holding)]


def test_the_packaged_policy_holds_the_isolation_floor():
    chosen = condition_policy()
    assert chosen.turns == 20
    assert "--tools" in chosen.worker_arguments


@pytest.mark.parametrize(
    "change",
    [
        lambda a: [x for x in a if x != "--no-chrome"],
        lambda a: [*a, "--dangerously-skip-permissions"],
        lambda a: [x if x != "dontAsk" else "bypassPermissions" for x in a],
        lambda a: [*a, "--print"],
    ],
)
def test_arguments_that_weaken_the_floor_are_refused(change):
    raw = condition_policy().model_dump(mode="json")
    raw["worker_arguments"] = change(list(raw["worker_arguments"]))
    with pytest.raises(ValueError, match="condition judge arguments"):
        ConditionPolicy.model_validate_json(json.dumps(raw))


def test_a_prompt_without_its_places_and_an_unreadable_policy_are_refused(tmp_path):
    raw = condition_policy().model_dump(mode="json") | {"prompt": "no places"}
    with pytest.raises(ValueError, match="must name"):
        ConditionPolicy.model_validate_json(json.dumps(raw))
    bad = tmp_path / "policy.json"
    bad.write_text("{}", encoding="utf-8")
    with pytest.raises(SetupError, match="invalid condition judge policy"):
        condition_policy(bad)
    good = tmp_path / "good.json"
    good.write_text(condition_policy().model_dump_json(), encoding="utf-8")
    assert condition_policy(good) == condition_policy()


def test_only_holding_routes_come_and_the_answer_is_kept(tmp_path):
    cache = ConditionCache(tmp_path / "cache.json", 100, 30)
    model = Model(("git stash",))
    check = ConditionCheck(policy(), cache, model)
    lessons = [{"id": "mae", "routes": [MAE]}, {"id": "stash", "routes": [MAE, STASH]}]
    assert check.holding("git stash apply", "/w", lessons) == {"stash": [1]}
    assert check.holding("git stash apply", "/w", lessons) == {"stash": [1]}
    assert len(model.prompts) == 1  # the second time came from the cache
    assert "folder: /w" in model.prompts[0]


def test_no_answer_keeps_the_routes_out_and_asks_again(tmp_path):
    silent = Model(("git stash",), answer=False)
    check = ConditionCheck(policy(), ConditionCache(tmp_path / "c.json", 10, 30), silent)
    lessons = [{"id": "stash", "routes": [STASH]}]
    assert check.holding("a", None, lessons) == {}
    assert check.holding("a", None, lessons) == {}
    assert len(silent.prompts) == 2


def test_a_prompt_over_its_bound_is_not_sent(tmp_path):
    model = Model(("git stash",))
    check = ConditionCheck(
        policy(max_prompt_bytes=10), ConditionCache(tmp_path / "c", 10, 30), model
    )
    assert check.holding("a", None, [{"id": "stash", "routes": [STASH]}]) == {}
    assert model.prompts == []


def test_the_cache_is_bounded_in_entries_and_age(tmp_path):
    path = tmp_path / "cache.json"
    cache = ConditionCache(path, 2, 30)
    for number in range(5):
        cache.put({f"k{number}": True})
        time.sleep(0.01)
    assert set(json.loads(path.read_text())) == {"k3", "k4"}
    path.write_text(json.dumps({"stale": [True, time.time() - 40 * 86400]}))
    aged = ConditionCache(path, 10, 30)
    aged.put({"new": False})
    assert set(json.loads(path.read_text())) == {"new"}
    assert aged.get("new") is False
    assert aged.get("missing") is None
    path.write_text("[1]")
    assert ConditionCache(path, 10, 30).get("x") is None
    path.write_text("not json")
    assert ConditionCache(path, 10, 30).get("x") is None


def worker(tmp_path, script=STAND_IN, **changes):
    return Worker((sys.executable, "-c", script), tmp_path, policy(**changes))


def test_the_worker_answers_in_a_row_and_hands_over(tmp_path):
    each = worker(tmp_path, turns=2)
    first = each._current()
    assert each.ask("holds: r1 r2", 5.0) == ["r1", "r2"]
    assert each._spare is not None  # started while the first does its last judgement
    assert each.ask("holds: r3", 5.0) == ["r3"]
    assert each._process is not first
    assert first.wait(timeout=2) is not None
    assert each.ask("nothing", 5.0) == []


def test_a_late_answer_is_finished_in_the_background(tmp_path):
    each = worker(tmp_path, turns=5)
    first = each._current()
    arrived = []
    assert each.ask("slow holds: r1", 0.5, arrived.append) is None
    assert each.ask("holds: r2", 5.0) is None  # still finishing: this action goes without
    deadline = time.monotonic() + 5
    while not arrived and time.monotonic() < deadline:
        time.sleep(0.05)
    assert arrived == [["r1"]]
    assert each.ask("holds: r3", 5.0) == ["r3"]
    assert each._process is first


def test_a_late_answer_that_never_comes_ends_the_worker(tmp_path):
    each = worker(tmp_path, turns=5, late_seconds=0.3)
    assert each.ask("slow holds: r1", 0.01) is None
    deadline = time.monotonic() + 5
    while each._process is not None and time.monotonic() < deadline:
        time.sleep(0.05)
    assert each._process is None


@pytest.mark.parametrize(
    "script",
    [
        'import sys\nsys.stdin.readline()\nprint(\'{"type": "result"}\', flush=True)',
        "import sys\nsys.stdin.readline()",
        "import os, time\nos.close(0)\ntime.sleep(5)",
    ],
)
def test_a_worker_whose_stream_breaks_is_replaced(tmp_path, script):
    each = worker(tmp_path, script)
    each._current()
    time.sleep(0.5)  # the last stand-in closes its input before the question
    assert each.ask("x" * 200_000, 3.0) is None
    assert each._process is None


def test_a_worker_that_cannot_start_and_one_that_will_not_end(tmp_path, monkeypatch):
    missing = Worker((str(tmp_path / "missing"),), tmp_path, policy())
    with pytest.raises(SetupError, match="did not start"):
        missing.ask("x", 1.0)
    stubborn = worker(tmp_path, reap_seconds=0.2)
    process = subprocess.Popen([sys.executable, "-c", "import time\ntime.sleep(30)"])
    stubborn._reap(process)
    assert process.returncode is not None

    class Unclosable:
        killed = False

        class stdin:  # noqa: N801 - stands in for a process's stdin attribute
            @staticmethod
            def close():
                raise OSError("closed")

        def kill(self):
            self.killed = True

        def wait(self, timeout=None):
            return 0

    process = Unclosable()
    stubborn._stop(process)  # type: ignore[arg-type]
    assert process.killed


def test_a_deployment_builds_the_isolated_command_and_the_service_option(tmp_path):
    executable = tmp_path / "claude"
    executable.write_text("#!/bin/sh\n", encoding="utf-8")
    executable.chmod(0o700)
    deployment = JudgeConfig(executable=executable, cwd=tmp_path, model="small-model")
    check = condition_check(deployment)
    command = check._ask.__self__._command  # type: ignore[attr-defined]
    assert command[:3] == [str(executable), "--model", "small-model"]
    assert "--json-schema" in command and "--system-prompt" in command
    assert _condition_check(Namespace(condition_judge_config=None)) is None
    path = tmp_path / "judge.json"
    path.write_text(deployment.model_dump_json(), encoding="utf-8")
    assert isinstance(_condition_check(Namespace(condition_judge_config=path)), ConditionCheck)
    path.write_text("{}", encoding="utf-8")
    with pytest.raises(SetupError, match="deployment is invalid"):
        _condition_check(Namespace(condition_judge_config=path))
    assert condition_judge.SCHEMA.startswith('{"type":"object"')
