"""Binary diagnostics stay bounded and opt-in; the text-worker surface still withholds them."""

import sys

import pytest

from anamnema.native_commands import NativeCommandPolicy, NativeRunner
from anamnema.setup_config import SetupError
from anamnema.snapshot_worker import CommandResult, run_command, run_worker


@pytest.fixture
def worker(tmp_path):
    return NativeRunner(
        tmp_path,
        NativeCommandPolicy(
            max_input_bytes=1024,
            max_output_bytes=1024,
            read_chunk_bytes=64,
            timeout_seconds=5.0,
        ),
    )


def test_binary_stdout_stderr_and_failure_code_remain_separate(worker, capsys):
    command = [
        sys.executable,
        "-c",
        "import sys; sys.stdout.buffer.write(b'out\\x00'); "
        "sys.stderr.buffer.write(b'private\\xff\\n'); sys.exit(2)",
    ]
    result = run_command(command, worker, worker.policy, "fixture")
    assert result.code == 2 and result.stdout == b"out\x00"
    assert result.stderr == b"private\xff\n"
    assert "private" not in repr(result)
    assert capsys.readouterr() == ("", "")
    assert CommandResult(0, b"old caller").stderr == b""


def test_combined_output_limit_still_covers_both_channels(worker):
    command = [
        sys.executable,
        "-c",
        "import sys; sys.stdout.write('o' * 600); sys.stderr.write('e' * 600)",
    ]
    with pytest.raises(SetupError, match="output exceeds"):
        run_command(command, worker, worker.policy, "fixture")


def test_text_worker_keeps_failure_stderr_private(worker, capsys):
    command = [
        sys.executable,
        "-c",
        "import sys; sys.stderr.write('private fixture diagnostic'); sys.exit(2)",
    ]
    with pytest.raises(SetupError, match="output withheld") as error:
        run_worker(command, worker, worker.policy, "fixture")
    assert "private fixture diagnostic" not in str(error.value)
    assert capsys.readouterr() == ("", "")
