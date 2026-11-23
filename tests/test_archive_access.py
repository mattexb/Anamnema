"""Project-only credential routing, secret-safe failures and installed import."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from pymongo.errors import OperationFailure

from anamnema import archive_access, cli, secret_service
from anamnema.archive_access import connect, keyring_uri
from anamnema.ingest_config import archive_settings
from anamnema.setup_config import SetupError, load_config


def test_neutral_keyring_attributes_and_session_bus(monkeypatch: pytest.MonkeyPatch) -> None:
    captured = {}

    def runner(command, **kwargs):
        captured.update(command=command, **kwargs)
        return subprocess.CompletedProcess(command, 0, "mongodb://127.0.0.1:27017", "")

    monkeypatch.setattr(secret_service.subprocess, "run", runner)
    monkeypatch.delenv("DBUS_SESSION_BUS_ADDRESS", raising=False)
    assert keyring_uri(load_config(), archive_settings()) == "mongodb://127.0.0.1:27017"
    assert captured["command"] == [
        "secret-tool",
        "lookup",
        "service",
        "anamnema",
        "entry",
        "archive/mongodb",
        "field",
        "uri",
    ]
    assert captured["check"] is False and captured["capture_output"] is True
    assert captured["env"]["DBUS_SESSION_BUS_ADDRESS"] == f"unix:path=/run/user/{os.getuid()}/bus"


@pytest.mark.parametrize("failure", ["empty", "returncode", "timeout", "missing", "encoding"])
def test_keyring_failures_never_expose_output(
    monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    bait = "synthetic-password-never-print"

    def runner(command, **kwargs):
        if failure == "timeout":
            raise subprocess.TimeoutExpired(command, 1, output=bait, stderr=bait)
        if failure == "missing":
            raise OSError(bait)
        if failure == "encoding":
            raise UnicodeDecodeError("utf-8", b"\xff", 0, 1, bait)
        return subprocess.CompletedProcess(command, 1 if failure == "returncode" else 0, "", bait)

    monkeypatch.setattr(secret_service.subprocess, "run", runner)
    with pytest.raises(SetupError) as error:
        keyring_uri(load_config(), archive_settings())
    assert bait not in str(error.value)


@pytest.mark.parametrize(
    "uri",
    [
        "mongodb://example.invalid",
        "mongodb+srv://example.invalid",
        "mongodb://127.0.0.1/other",
        "mongodb://127.0.0.1,127.0.0.1:27018",
        "mongodb://127.0.0.1/?proxyHost=example.invalid",
        "not-a-uri-synthetic-password",
    ],
)
def test_rejected_target_never_opens_client(monkeypatch: pytest.MonkeyPatch, uri: str) -> None:
    def forbidden(*args, **kwargs):
        pytest.fail("invalid/remote target must fail before opening Mongo")

    monkeypatch.setattr(archive_access, "MongoClient", forbidden)
    with pytest.raises(SetupError) as error:
        connect(uri, archive_settings())
    assert uri not in str(error.value)
    assert "SetupError" not in str(error.value)


def test_failed_ping_closes_client_and_redacts_driver_details(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = []

    class Client:
        admin = None

        def command(self, name):
            raise OperationFailure("synthetic-password-never-print")

        def close(self):
            calls.append("closed")

    client = Client()
    client.admin = client
    monkeypatch.setattr(archive_access, "MongoClient", lambda *args, **kwargs: client)
    with pytest.raises(SetupError, match="OperationFailure") as error:
        connect("mongodb://127.0.0.1", archive_settings())
    assert calls == ["closed"] and "synthetic-password" not in str(error.value)


def test_import_dry_run_never_loads_setup_or_credentials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from anamnema import import_cli

    def forbidden(*args, **kwargs):
        pytest.fail("dry-run must not use credentials or Mongo")

    monkeypatch.setattr(import_cli, "keyring_uri", forbidden)
    monkeypatch.setattr(import_cli, "load_config", forbidden)
    monkeypatch.setattr(import_cli, "connect", forbidden)
    path = tmp_path / "session.jsonl"
    path.write_text(
        '{"type":"user","timestamp":"2026-01-01","message":{"content":"synthetic"}}\n',
        encoding="utf-8",
    )
    assert (
        cli.main(["import", "--file", str(path), "--instance", "agent", "--session", "session"])
        == 0
    )
    report = json.loads(capsys.readouterr().out)
    assert report["mode"] == "dry-run" and not report["verified"]


def test_installed_import_uses_only_fixture_keyring_and_disposable_mongo(
    tmp_path: Path, disposable_mongo
) -> None:
    uri, client = disposable_mongo
    executable = tmp_path / "fake-keyring"
    executable.write_text(
        f"#!{sys.executable}\nimport sys\nsys.stdout.write({uri!r})\n", encoding="utf-8"
    )
    executable.chmod(0o700)
    config = load_config().model_dump(mode="json")
    config["keyring"]["executable"] = str(executable)
    config["services"]["archive"]["port"] = client.address[1]
    config_file = tmp_path / "setup.json"
    config_file.write_text(json.dumps(config), encoding="utf-8")
    source = tmp_path / "session.jsonl"
    source.write_text(
        json.dumps(
            {
                "type": "user",
                "uuid": "native",
                "timestamp": "2026-01-01",
                "message": {"content": "synthetic"},
            }
        )
        + "\n",
        encoding="utf-8",
    )
    original = source.read_bytes()
    command = [
        str(Path(sys.executable).with_name("anamnema")),
        "import",
        "--file",
        str(source),
        "--instance",
        "cli-fixture",
        "--session",
        "session",
        "--closed",
        "--apply",
        "--config",
        str(config_file),
    ]
    reports = []
    for _ in range(2):
        done = subprocess.run(  # noqa: S603 - local entry point, explicit fixture input/config
            command, cwd=tmp_path, capture_output=True, text=True, encoding="utf-8", check=False
        )
        assert done.returncode == 0, done.stderr
        reports.append(json.loads(done.stdout))
    assert reports[0]["inserted"] == 2 and reports[1]["inserted"] == 0
    assert reports[1]["already_present"] == 2 and reports[1]["verified"]
    assert source.read_bytes() == original
    assert client["anamnema"]["import_receipts"].count_documents({"instance": "cli-fixture"}) == 1
