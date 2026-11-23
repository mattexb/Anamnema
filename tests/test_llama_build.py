"""Owned build recovery and artifact checks; native compilation is a separate VM gate."""

import json
import subprocess
from importlib.resources import as_file, files
from pathlib import Path

import pytest

from anamnema.database_install import Ran
from anamnema.llama_build import LlamaBuild, build_llama
from anamnema.setup_config import SetupError


@pytest.fixture
def config(tmp_path):
    return LlamaBuild(
        root=tmp_path / "build",
        shell=Path("/bin/bash"),
        revision="5aeac37a35b44fde4bdffaec9159f2fcb253b622",
        jobs=2,
        timeout_seconds=30.0,
        max_artifact_bytes=4096,
        max_artifacts=10,
        hash_block_bytes=64,
        max_receipt_bytes=4096,
    )


def test_build_intent_is_durable_before_run_and_replay_is_local(config):
    calls = []

    def runner(argv, stdin, timeout, env):
        assert json.loads((config.root / "build.json").read_text())["phase"] == "prepared"
        assert stdin is None and env is None and timeout == config.timeout_seconds
        assert argv[-2:] == [config.revision, str(config.jobs)]
        calls.append(argv)
        config.binary.parent.mkdir(parents=True)
        config.binary.write_bytes(b"synthetic binary")
        config.binary.chmod(0o700)
        return Ran(0, b"not logged")

    assert build_llama(config, runner, allow_download=True) == config.binary
    assert build_llama(config, runner, allow_download=False) == config.binary
    tuned = config.model_copy(update={"jobs": 3, "timeout_seconds": 60.0, "hash_block_bytes": 8})
    assert build_llama(tuned, runner, allow_download=False) == config.binary
    assert len(calls) == 1
    config.binary.write_bytes(b"changed binary")
    with pytest.raises(SetupError, match="artifacts changed"):
        build_llama(config, runner, allow_download=True)
    assert len(calls) == 1


def test_foreign_build_and_changed_profile_refuse_without_running(config):
    config.root.mkdir(mode=0o700)
    (config.root / "foreign").touch()

    def runner(*_):
        pytest.fail("command on foreign root")

    with pytest.raises(SetupError, match="not owned"):
        build_llama(config, runner, allow_download=True)


def test_failed_build_keeps_prepared_receipt_for_retry(config):
    def failed(*_):
        return Ran(1, b"private diagnostic withheld")

    with pytest.raises(SetupError, match="prepared receipt retained"):
        build_llama(config, failed, allow_download=True)
    assert json.loads((config.root / "build.json").read_text())["phase"] == "prepared"
    with pytest.raises(SetupError, match="different inputs"):
        build_llama(config.model_copy(update={"revision": "0" * 40}), failed, allow_download=True)
    with pytest.raises(SetupError, match="explicit consent"):
        build_llama(config, failed, allow_download=False)


def test_recipe_has_native_shell_syntax_and_no_ui_download():
    with as_file(files("anamnema").joinpath("resources/build_cpu_llama.sh")) as script:
        result = subprocess.run(  # noqa: S603 - offline parser for the packaged project recipe
            ["/bin/bash", "-n", str(script)],
            check=False,
            capture_output=True,
            timeout=5,
        )
        assert result.returncode == 0
        body = script.read_text()
    assert "-DLLAMA_BUILD_UI=OFF -DLLAMA_USE_PREBUILT_UI=OFF" in body
    assert "-DGGML_VULKAN=OFF -DGGML_CUDA=OFF" in body
