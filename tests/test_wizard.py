"""Wizard choices, explicit saving and failure handling without installation."""

from __future__ import annotations

import io
import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from anamnema import cli, setup_answers
from anamnema.setup_answers import save_answers
from anamnema.setup_config import SetupError, load_config, parse_config
from anamnema.setup_wizard import Questions, WizardCancelled, collect, review


def questions(*answers: str) -> tuple[Questions, io.StringIO]:
    output = io.StringIO()
    return Questions(io.StringIO("\n".join(answers) + "\n"), output), output


def user_config():
    prompt, _ = questions("no", "user", "alex", "manual", "api", "no")
    return collect(load_config(), prompt, lambda: "alex")


def test_user_path_preserves_separate_package_accounts_without_installing() -> None:
    config = user_config()
    assert config.choices.service_scope == "user"
    assert config.choices.accounts.application == "alex"
    assert config.choices.accounts.models == "alex"
    assert config.choices.accounts.archive is None and config.choices.accounts.graph is None
    assert config.choices.keyring_setup == "manual" and config.choices.keyring_owner == "alex"
    assert config.choices.run_when_logged_out is False and config.backend == "api"


def test_system_path_keeps_package_accounts_distinct_and_guided_unverified() -> None:
    prompt, output = questions("yes", "system", "alex", "models", "", "", "guided", "vulkan", "no")
    config = collect(load_config(), prompt, lambda: "alex")
    assert config.choices.service_scope == "system" and config.choices.run_when_logged_out
    assert config.choices.accounts.application == "alex"
    assert config.choices.accounts.models == "models"
    assert config.choices.accounts.archive is None and config.choices.accounts.graph is None
    assert config.choices.keyring_setup == "guided"
    assert "Guided apply is not built yet" in output.getvalue()


def test_invalid_choice_account_and_path_are_reprompted_without_echo() -> None:
    prompt, output = questions(
        "not-a-choice",
        "no",
        "user",
        "root",
        "invented/secret",
        "alex",
        "manual",
        "api",
        "yes",
        "relative",
        "~/config",
        "~/data",
        "~/state",
        "~/models",
    )
    config = collect(load_config(), prompt, lambda: "alex")
    assert config.paths.data == "~/data" and config.choices.accounts.application == "alex"
    assert "invented/secret" not in output.getvalue()
    assert "Choose one" in output.getvalue() and "absolute path" in output.getvalue()


def test_resume_defaults_preserve_saved_choices() -> None:
    original = user_config()
    prompt, _ = questions("", "", "", "", "", "")
    assert collect(original, prompt, lambda: "other") == original


def test_review_precedes_save_question_and_defaults_to_no(tmp_path: Path) -> None:
    prompt, output = questions("")
    assert not review(user_config(), prompt, tmp_path / "answers.json")
    rendered = output.getvalue()
    assert rendered.index("Proposed nonsecret configuration") < rendered.index("Save these")
    assert not list(tmp_path.iterdir())
    prompt, _ = questions()
    assert not review(user_config(), prompt, None)


def test_eof_cancels_instead_of_accepting_defaults() -> None:
    prompt = Questions(io.StringIO(""), io.StringIO())
    with pytest.raises(WizardCancelled):
        collect(load_config(), prompt, lambda: "alex")


def test_saved_answers_roundtrip_and_have_restricted_permissions(tmp_path: Path) -> None:
    path = tmp_path / "answers.json"
    config = user_config()
    save_answers(path, config)
    assert load_config(path) == config
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert list(tmp_path.iterdir()) == [path]
    assert "password" not in path.read_text(encoding="utf-8").lower()


@pytest.mark.parametrize("kind", ["file", "directory", "symlink", "broken-symlink"])
def test_save_never_overwrites_any_existing_destination(tmp_path: Path, kind: str) -> None:
    path, target = tmp_path / "answers.json", tmp_path / "original"
    target.write_text("keep", encoding="utf-8")
    if kind == "file":
        path.write_text("existing", encoding="utf-8")
    elif kind == "directory":
        path.mkdir()
    else:
        path.symlink_to(target if kind == "symlink" else tmp_path / "absent")
    with pytest.raises(SetupError, match="already exists"):
        save_answers(path, user_config())
    assert target.read_text(encoding="utf-8") == "keep"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["answers.json", "original"]


def test_concurrent_creation_is_not_overwritten_and_temp_is_removed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "answers.json"

    def concurrent(source: Path, target: Path, **kwargs: object) -> None:
        target.write_text("concurrent", encoding="utf-8")
        raise FileExistsError

    monkeypatch.setattr(setup_answers.os, "link", concurrent)
    with pytest.raises(SetupError, match="FileExistsError"):
        save_answers(path, user_config())
    assert path.read_text(encoding="utf-8") == "concurrent"
    assert list(tmp_path.iterdir()) == [path]


def test_failed_write_cannot_publish_partial_answers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def interrupted(descriptor: int) -> None:
        raise OSError("simulated failure")

    monkeypatch.setattr(setup_answers.os, "fsync", interrupted)
    with pytest.raises(SetupError, match="OSError"):
        save_answers(tmp_path / "answers.json", user_config())
    assert not list(tmp_path.iterdir())
    with pytest.raises(SetupError):
        save_answers(tmp_path / "missing/answers.json", user_config())


@pytest.mark.parametrize("change", ["keyring-owner", "missing-application", "root-owner"])
def test_contradictory_account_configuration_is_rejected(change: str) -> None:
    raw = user_config().model_dump(mode="json")
    if change == "keyring-owner":
        raw["choices"]["keyring_owner"] = "different"
    elif change == "root-owner":
        raw["choices"]["keyring_owner"] = "root"
    else:
        raw["choices"]["accounts"]["application"] = None
    with pytest.raises(SetupError):
        parse_config(json.dumps(raw))


def test_user_application_profile_can_keep_distinct_daemon_accounts() -> None:
    raw = user_config().model_dump(mode="json")
    raw["choices"]["accounts"].update(models="models", archive="mongodb", graph="neo4j")
    config = parse_config(json.dumps(raw))
    assert config.choices.accounts.application == "alex"
    assert config.choices.accounts.models == "models" and config.choices.keyring_owner == "alex"


def test_cli_refuses_save_on_dry_run(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    path = tmp_path / "answers.json"
    assert cli.main(["setup", "--dry-run", "--save", str(path)]) == 2
    assert not path.exists() and "requires --wizard" in capsys.readouterr().err


def test_manual_guide_is_packaged_and_needs_no_configuration(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    def forbidden(*args: object) -> None:
        pytest.fail("the guide must not inspect the machine or load configuration")

    monkeypatch.setattr(cli, "load_config", forbidden)
    monkeypatch.setattr(cli, "inspect_host", forbidden)
    assert cli.main(["setup", "--keyring-guide"]) == 0
    assert "Manual Keyring Setup" in capsys.readouterr().out


def test_missing_detected_account_is_explained_and_can_be_supplied() -> None:
    def missing() -> str:
        raise OSError

    prompt, output = questions("no", "user", "alex", "manual", "api", "no")
    config = collect(load_config(), prompt, missing)
    assert config.choices.accounts.application == "alex"
    assert "Cannot detect your account" in output.getvalue()


def test_cli_wizard_save_and_resume_share_configuration(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "answers.json"
    monkeypatch.setattr(sys, "stdin", io.StringIO("no\nuser\nalex\nmanual\napi\nno\nyes\n"))
    assert cli.main(["setup", "--wizard", "--save", str(path)]) in (0, 2)
    assert json.loads(capsys.readouterr().out)["answers_saved"]
    assert cli.main(["setup", "--config", str(path)]) in (0, 2)
    report = json.loads(capsys.readouterr().out)
    assert report["mode"] == "dry-run" and report["choices"]["keyring_owner"] == "alex"


def test_cli_eof_and_keyboard_interrupt_do_not_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "answers.json"
    monkeypatch.setattr(sys, "stdin", io.StringIO(""))
    assert cli.main(["setup", "--wizard", "--save", str(path)]) == 130

    def interrupted(*args: object) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(cli, "collect", interrupted)
    assert cli.main(["setup", "--wizard", "--save", str(path)]) == 130
    assert not path.exists() and "cancelled" in capsys.readouterr().err


@pytest.mark.parametrize("confirm", ["yes", "no"])
def test_real_command_reviews_and_only_saves_explicitly_confirmed_answers(
    tmp_path: Path, confirm: str
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    path = tmp_path / "answers.json"
    command = str(Path(sys.executable).with_name("anamnema"))
    done = subprocess.run(  # noqa: S603 - installed local entry point and test-case arguments
        [command, "setup", "--wizard", "--save", str(path)],
        input="\n".join(["no", "user", "alex", "manual", "api", "no", confirm]) + "\n",
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
        cwd=tmp_path,
        env=os.environ | {"HOME": str(home), "PYTHONDONTWRITEBYTECODE": "1"},
    )
    assert done.returncode in (0, 2) and "Traceback" not in done.stderr
    report = json.loads(done.stdout)
    assert report["mode"] == "wizard" and report["ready_to_install"] is False
    assert report["answers_saved"] is (confirm == "yes")
    assert path.exists() is (confirm == "yes") and not list(home.iterdir())
    if path.exists():
        assert load_config(path) == user_config()
