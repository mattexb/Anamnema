"""Archive policy validation and explicit configuration selection."""

import json
from pathlib import Path

import pytest

from anamnema import cli, import_cli
from anamnema.ingest_config import archive_settings
from anamnema.setup_config import SetupError, load_config


@pytest.mark.parametrize("problem", ["missing", "duplicate", "empty"])
def test_collection_contract_is_complete_and_distinct(tmp_path: Path, problem: str) -> None:
    raw = archive_settings().model_dump(mode="json")
    if problem == "missing":
        raw["collections"].pop("receipt")
    elif problem == "duplicate":
        raw["collections"]["receipt"] = raw["collections"]["conversation"]
    else:
        raw["collections"]["receipt"] = ""
    path = tmp_path / "archive.json"
    path.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(SetupError, match="validated"):
        archive_settings(path)


def test_explicit_archive_policy_roundtrip(tmp_path: Path) -> None:
    config = archive_settings()
    path = tmp_path / "archive.json"
    path.write_text(config.model_dump_json(), encoding="utf-8")
    assert archive_settings(path) == config
    with pytest.raises(SetupError):
        archive_settings(tmp_path / "missing")


def test_main_import_apply_uses_shared_config_and_verified_backend(
    tmp_path: Path,
    disposable_mongo,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    uri, client = disposable_mongo
    setup = load_config().model_dump(mode="json")
    setup["services"]["archive"]["port"] = client.address[1]
    config = tmp_path / "setup.json"
    config.write_text(json.dumps(setup), encoding="utf-8")
    source = tmp_path / "source.jsonl"
    source.write_text(
        '{"type":"user","timestamp":"2026-01-01","uuid":"main-fixture"}', encoding="utf-8"
    )
    monkeypatch.setattr(import_cli, "keyring_uri", lambda *args: uri)
    assert (
        cli.main(
            [
                "import",
                "--file",
                str(source),
                "--instance",
                "main-cli-fixture",
                "--session",
                "session",
                "--apply",
                "--config",
                str(config),
            ]
        )
        == 0
    )
    report = json.loads(capsys.readouterr().out)
    assert report["verified"] and report["inserted"] == 1
