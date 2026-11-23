"""Exact credential pipes with a temporary fake keyring; never contact the user's service."""

import hashlib
import io
import json
import os
import subprocess
import sys
import warnings

import pytest

from anamnema.archive_access import keyring_secret
from anamnema.cli import main
from anamnema.ingest_config import archive_settings
from anamnema.secret_service import SecretService
from anamnema.setup_config import load_config

WORKER = """import json, os, pathlib, sys
os.umask(0o077)
root = pathlib.Path(__file__).parent
args = sys.argv[1:]
record = root / "arguments.json"
history = json.loads(record.read_text(encoding="utf-8")) if record.exists() else []
history.append(args)
record.write_text(json.dumps(history), encoding="utf-8")
mode = (root / "mode.txt").read_text(encoding="utf-8")
value = root / "fixture-secret.txt"
if args[0] == "store":
    incoming = sys.stdin.read()
    if mode == "fail-store":
        print(incoming, file=sys.stderr)
        sys.exit(3)
    value.write_text(incoming, encoding="utf-8")
elif args[0] == "lookup":
    if not value.exists() or mode == "fail-lookup":
        print("synthetic-private-error-detail", file=sys.stderr)
        sys.exit(1)
    sys.stdout.write("different" if mode == "mismatch" else value.read_text(encoding="utf-8"))
"""


@pytest.fixture
def keyring(tmp_path):
    executable = tmp_path / "fixture-keyring"
    executable.write_text(f"#!{sys.executable}\n" + WORKER, encoding="utf-8")
    executable.chmod(0o700)
    (tmp_path / "mode.txt").write_text("normal", encoding="utf-8")
    config = load_config().model_copy(
        update={"keyring": load_config().keyring.model_copy(update={"executable": str(executable)})}
    )
    path = tmp_path / "setup.json"
    path.write_text(config.model_dump_json(), encoding="utf-8")
    return config, path, tmp_path


def command(path, action="put", *extra):
    return [
        "keyring",
        action,
        "--config",
        str(path),
        "--entry",
        "fixture/local",
        "--field",
        "password",
        *extra,
    ]


def test_actual_store_and_verify_preserve_whitespace_without_printing_or_argv_values(
    keyring, monkeypatch, capsys
):
    config, path, root = keyring
    value = "  synthetic canary with trailing whitespace \n"
    monkeypatch.setattr("sys.stdin", io.StringIO(value))
    assert main(command(path, "put", "--apply", "--confirm-replace", "--stdin")) == 0
    output = capsys.readouterr()
    assert json.loads(output.out)["verified"] is True
    assert value not in output.out + output.err
    assert (root / "fixture-secret.txt").read_text(encoding="utf-8") == value
    assert keyring_secret(config, archive_settings(), "fixture/local", "password") == value
    assert value not in (root / "arguments.json").read_text(encoding="utf-8")
    assert main(command(path, "verify", "--apply")) == 0
    assert json.loads(capsys.readouterr().out)["available"] is True


def test_preview_and_missing_replace_confirmation_never_read_value_or_start_tool(
    keyring, monkeypatch, capsys
):
    _, path, root = keyring
    stream = io.StringIO("synthetic-value")
    monkeypatch.setattr("sys.stdin", stream)
    assert main(command(path)) == 0
    assert json.loads(capsys.readouterr().out)["keyring_accessed"] is False
    assert main(command(path, "put", "--apply", "--stdin")) == 2
    assert "confirm-replace" in json.loads(capsys.readouterr().err)["error"]
    assert stream.tell() == 0 and not (root / "arguments.json").exists()


@pytest.mark.parametrize("mode", ["fail-store", "fail-lookup", "mismatch"])
def test_failures_redact_tool_output_and_never_claim_verified(keyring, monkeypatch, capsys, mode):
    _, path, root = keyring
    (root / "mode.txt").write_text(mode, encoding="utf-8")
    value = "synthetic-private-value"
    monkeypatch.setattr("sys.stdin", io.StringIO(value))
    assert main(command(path, "put", "--apply", "--confirm-replace", "--stdin")) == 2
    output = capsys.readouterr()
    assert (
        not output.out
        and value not in output.err
        and "synthetic-private-error-detail" not in output.err
    )
    assert "error" in json.loads(output.err)


def test_oversize_multibyte_value_refuses_before_subprocess(keyring, monkeypatch, capsys):
    config, path, root = keyring
    policy = root / "keyring-policy.json"
    policy.write_text(
        '{"max_value_bytes":4,"max_attribute_characters":512,"max_injections":16,"label_prefix":"Anamnema"}',
        encoding="utf-8",
    )
    config = config.model_copy(
        update={"keyring": config.keyring.model_copy(update={"policy": policy})}
    )
    path.write_text(config.model_dump_json(), encoding="utf-8")
    monkeypatch.setattr("sys.stdin", io.StringIO("èèè"))
    assert main(command(path, "put", "--apply", "--confirm-replace", "--stdin")) == 2
    assert "byte limit" in json.loads(capsys.readouterr().err)["error"]
    assert not (root / "arguments.json").exists()


def test_non_tty_and_failed_hidden_prompt_have_no_echo_fallback(keyring, monkeypatch, capsys):
    _, path, root = keyring
    stream = io.StringIO("synthetic-secret")
    monkeypatch.setattr("sys.stdin", stream)
    assert main(command(path, "put", "--apply", "--confirm-replace")) == 2
    assert "terminal" in json.loads(capsys.readouterr().err)["error"]
    monkeypatch.setattr(stream, "isatty", lambda: True)

    def warning(*args, **kwargs):
        from getpass import GetPassWarning

        warnings.warn("echo fallback", GetPassWarning, stacklevel=1)
        pytest.fail("warning must refuse before reading input")

    monkeypatch.setattr("anamnema.keyring_cli.getpass.getpass", warning)
    assert main(command(path, "put", "--apply", "--confirm-replace")) == 2
    assert "no echo fallback" in json.loads(capsys.readouterr().err)["error"]
    assert stream.tell() == 0 and not (root / "arguments.json").exists()


def test_low_level_lookup_uses_the_same_exact_secret_service(keyring):
    config, _, root = keyring
    value = "\n synthetic whitespace \n"
    (root / "fixture-secret.txt").write_text(value, encoding="utf-8")
    assert (
        SecretService(config.keyring, archive_settings()).lookup("fixture/local", "password")
        == value
    )


def test_run_executes_child_with_exact_secret_only_in_environment(keyring):
    _, path, root = keyring
    value = " synthetic environment canary \n"
    (root / "fixture-secret.txt").write_text(value, encoding="utf-8")
    child = root / "child.py"
    child.write_text(
        "import hashlib, json, os, sys\n"
        'value = os.environ["FIXTURE_SECRET"].encode()\n'
        'print(json.dumps({"digest": hashlib.sha256(value).hexdigest(), '
        '"argv": sys.argv[1:]}))\n',
        encoding="utf-8",
    )
    result = subprocess.run(  # noqa: S603 - explicit fixture Python and synthetic child only
        [
            sys.executable,
            "-m",
            "anamnema",
            "keyring",
            "run",
            "--config",
            str(path),
            "--apply",
            "--secret",
            "FIXTURE_SECRET=fixture/local:password",
            "--",
            sys.executable,
            str(child),
            "synthetic-argument",
        ],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=10,
    )
    assert json.loads(result.stdout) == {
        "digest": hashlib.sha256(value.encode()).hexdigest(),
        "argv": ["synthetic-argument"],
    }
    assert value not in result.stdout + result.stderr
    assert value not in (root / "arguments.json").read_text(encoding="utf-8")
    assert "FIXTURE_SECRET" not in os.environ


@pytest.mark.parametrize("references", [[], ["bad"], ["A=fixture/local:password"] * 2])
def test_run_invalid_references_refuse_before_keyring_lookup(keyring, capsys, references):
    _, path, root = keyring
    arguments = ["keyring", "run", "--config", str(path), "--apply"]
    for reference in references:
        arguments.extend(["--secret", reference])
    assert main([*arguments, "--", sys.executable]) == 2
    assert "error" in json.loads(capsys.readouterr().err)
    assert not (root / "arguments.json").exists()
