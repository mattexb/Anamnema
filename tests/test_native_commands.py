"""Installer uses the same bounded/reaped transport as native workers, with raw key bytes."""

import os
import socket
import sys

import pytest

from anamnema.native_commands import NativeCommandPolicy, NativeProbe, NativeRunner
from anamnema.setup_config import SetupError


@pytest.fixture
def runner(tmp_path):
    return NativeRunner(
        tmp_path,
        NativeCommandPolicy(
            max_output_bytes=1048576,
            max_input_bytes=1048576,
            read_chunk_bytes=65536,
            timeout_seconds=2.0,
        ),
    )


def test_binary_stdin_stdout_and_nonzero_exit_are_preserved(runner):
    raw = b"\x00\xff\xfe signing-key fixture\n\n"
    result = runner(
        [
            sys.executable,
            "-c",
            "import sys; sys.stdout.buffer.write(sys.stdin.buffer.read()); sys.exit(7)",
        ],
        raw,
        2.0,
        None,
    )
    assert result.code == 7 and result.stdout == raw


def test_secret_environment_is_child_only_and_stderr_never_returned(runner):
    result = runner(
        [
            sys.executable,
            "-c",
            "import os, sys; sys.stderr.write('withheld'); "
            "sys.stdout.write(os.environ['ANAMNEMA_NATIVE_TEST'])",
        ],
        None,
        2.0,
        {"ANAMNEMA_NATIVE_TEST": "synthetic"},
    )
    assert result.stdout == b"synthetic" and "ANAMNEMA_NATIVE_TEST" not in os.environ


def test_oversize_input_and_timeout_refuse(runner):
    with pytest.raises(SetupError, match="byte bound"):
        runner([sys.executable, "-c", "pass"], b"x" * 1048577, 2.0, None)
    with pytest.raises(SetupError, match="timed out"):
        runner([sys.executable, "-c", "import time; time.sleep(60)"], None, 0.1, None)


def test_probe_is_read_only_and_symlinks_are_not_empty_data(runner, tmp_path):
    executable = tmp_path / "fixture-dpkg-query"
    executable.write_text(f"#!{sys.executable}\nimport sys\nsys.stdout.write('installed')\n")
    executable.chmod(0o700)
    probe = NativeProbe(runner, executable)
    assert probe.package_installed("fixture")
    assert not probe.exists(tmp_path / "absent")
    assert not probe.nonempty_directory(tmp_path / "absent")
    folder = tmp_path / "data"
    folder.mkdir()
    assert not probe.nonempty_directory(folder)
    link = tmp_path / "link"
    link.symlink_to(folder)
    assert probe.nonempty_directory(link)
    (folder / "anything").touch()
    assert probe.nonempty_directory(folder)


def test_readiness_probe_contacts_only_loopback_without_login(runner, tmp_path):
    probe = NativeProbe(runner, tmp_path / "not-used")
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        port = listener.getsockname()[1]
        assert probe.listening("127.0.0.1", port)
    assert not probe.listening("127.0.0.1", port)
    with pytest.raises(SetupError, match="loopback"):
        probe.listening("192.0.2.10", port)
