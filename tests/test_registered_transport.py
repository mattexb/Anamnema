"""Real guarded launches preserve transport and release intents at the owning boundary."""

import json
import sys
from concurrent.futures import ThreadPoolExecutor

import pytest
from test_snapshot_worker import worker as worker

from anamnema.setup_config import SetupError
from anamnema.snapshot_worker import run_command
from anamnema.worker_registry import RegistryConfig


@pytest.fixture
def registry(tmp_path):
    return RegistryConfig(records_dir=tmp_path / "records", folder_roots=(tmp_path / "inputs",))


def test_registered_exec_keeps_binary_pipes_and_arms_kernel_parent_death(worker, registry):
    command = [
        sys.executable,
        "-c",
        "import ctypes,json,os,sys; "
        "v=ctypes.c_int(); assert ctypes.CDLL(None).prctl(2,ctypes.byref(v),0,0,0)==0; "
        "sys.stderr.buffer.write(b'private\\xff'); "
        "print(json.dumps({'signal':v.value,'pid':os.getpid(),"
        "'input':sys.stdin.buffer.read().hex()}))",
    ]
    result = run_command(
        command,
        worker,
        worker.policy,
        "guard fixture",
        input_data=b"\x00\xff",
        input_limit=2,
        registry=registry,
    )
    assert result.code == 0 and result.stderr == b"private\xff"
    assert json.loads(result.stdout)["signal"] == 9
    assert json.loads(result.stdout)["input"] == "00ff"
    assert not list(registry.records_dir.glob("*.json"))


def test_threaded_launches_have_independent_intents_and_cleanup(worker, registry):
    command = [sys.executable, "-c", "import os,time; time.sleep(0.1); print(os.getpid())"]

    def execute(_):
        return run_command(command, worker, worker.policy, "thread fixture", registry=registry)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(execute, range(2)))
    assert all(result.code == 0 for result in results)
    assert len({result.stdout for result in results}) == 2
    assert not list(registry.records_dir.glob("*.json"))


def test_bad_command_does_not_leave_a_folderless_intent(worker, registry):
    result = run_command(
        ["/missing-synthetic-worker"], worker, worker.policy, "missing fixture", registry=registry
    )
    assert result.code != 0
    assert not list(registry.records_dir.glob("*.json"))


def test_invalid_record_scope_refuses_before_launch(worker, registry, tmp_path):
    with pytest.raises(SetupError, match="differs"):
        run_command(
            [sys.executable, "-c", "raise AssertionError('must not run')"],
            worker,
            worker.policy,
            "scope fixture",
            registry=registry,
            record=tmp_path / "foreign.json",
        )
