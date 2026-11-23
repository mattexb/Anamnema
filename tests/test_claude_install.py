"""Hook registration owns only its receipt's groups and survives an interrupted write."""

import json
import os
from pathlib import Path
from typing import Any

import pytest

from anamnema.claude_install import (
    ClaudeHookInstaller,
    ContinuityHooks,
    HookReceipt,
    claude_hook_policy,
    continuity_bindings,
)
from anamnema.hook_merge import HookBinding
from anamnema.setup_config import SetupError

FOREIGN = {"hooks": [{"type": "command", "command": "foreign-tool", "timeout": 3}]}


def _bindings(tmp_path: Path, producer: float = 600.0) -> tuple[HookBinding, ...]:
    hooks = ContinuityHooks(
        executable=tmp_path / "bin" / "anamnema",
        continuity_config=tmp_path / "continuity.json",
        worker_config=tmp_path / "worker.json",
        producer_timeout_seconds=producer,
    )
    return continuity_bindings(hooks, claude_hook_policy())


def _setup(tmp_path: Path) -> tuple[ClaudeHookInstaller, Path]:
    claude = tmp_path / "claude"
    claude.mkdir()
    settings = claude / "settings.json"
    settings.write_text(
        json.dumps({"model": "opus", "hooks": {"Stop": [FOREIGN]}}), encoding="utf-8"
    )
    settings.chmod(0o644)
    return ClaudeHookInstaller(settings, tmp_path / "state", claude_hook_policy()), settings


def _read(settings: Path) -> dict[str, Any]:
    return json.loads(settings.read_text(encoding="utf-8"))


def test_bindings_name_each_event_and_outlast_the_snapshot_worker(tmp_path: Path) -> None:
    start, stop, precompact = _bindings(tmp_path)
    assert (start.event, stop.event, precompact.event) == ("SessionStart", "Stop", "PreCompact")
    assert start.command[1:3] == ("continuity", "start")
    assert "--worker-config" in precompact.command
    assert precompact.timeout_seconds == 600.0 + claude_hook_policy().precompact_margin_seconds
    with pytest.raises(ValueError, match="absolute"):
        ContinuityHooks(
            executable=Path("anamnema"),
            continuity_config=tmp_path / "c.json",
            worker_config=tmp_path / "w.json",
            producer_timeout_seconds=1.0,
        )


def test_apply_keeps_foreign_settings_repeats_cleanly_and_remove_restores(tmp_path: Path) -> None:
    installer, settings = _setup(tmp_path)
    desired = _bindings(tmp_path)
    assert installer.plan(desired)["changed"] is True
    assert installer.apply(desired) == {"changed": True, "hooks": 3}
    document = _read(settings)
    assert document["model"] == "opus"
    assert document["hooks"]["Stop"][0] == FOREIGN
    assert len(document["hooks"]["Stop"]) == 2
    assert os.stat(settings).st_mode & 0o777 == 0o644
    assert installer.apply(desired) == {"changed": False, "hooks": 3}
    upgraded = _bindings(tmp_path, producer=900.0)
    assert installer.apply(upgraded)["changed"] is True
    assert _read(settings)["hooks"]["PreCompact"][0]["hooks"][0]["timeout"] == 960.0
    assert installer.remove() == {"removed": 3}
    assert _read(settings) == {"model": "opus", "hooks": {"Stop": [FOREIGN]}}
    assert installer.remove() == {"removed": 0}


def test_an_edited_owned_hook_refuses_and_writes_nothing(tmp_path: Path) -> None:
    installer, settings = _setup(tmp_path)
    installer.apply(_bindings(tmp_path))
    document = _read(settings)
    document["hooks"]["SessionStart"][0]["hooks"][0]["timeout"] = 1
    settings.write_text(json.dumps(document), encoding="utf-8")
    before = settings.read_bytes()
    with pytest.raises(SetupError, match="neither|changed"):
        installer.apply(_bindings(tmp_path, producer=900.0))
    assert settings.read_bytes() == before


@pytest.mark.parametrize("landed", [True, False])
def test_an_interrupted_write_resolves_to_the_set_the_file_holds(
    tmp_path: Path, landed: bool
) -> None:
    installer, settings = _setup(tmp_path)
    old = _bindings(tmp_path)
    new = _bindings(tmp_path, producer=900.0)
    installer.apply(old)
    if landed:
        installer.apply(new)
    receipt = HookReceipt(
        version=1, settings=str(settings), phase="writing", previous=old, desired=new
    )
    installer.receipt.write_text(receipt.model_dump_json(), encoding="utf-8")
    assert installer.apply(new)["changed"] is (not landed)
    assert installer.remove() == {"removed": 3}
    assert _read(settings) == {"model": "opus", "hooks": {"Stop": [FOREIGN]}}


def test_settings_changed_between_read_and_write_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    installer, settings = _setup(tmp_path)
    original = installer._read_settings  # pyright: ignore[reportPrivateUsage]
    calls = {"n": 0}

    def racing() -> tuple[dict[str, Any], bytes | None]:
        calls["n"] += 1
        if calls["n"] == 2:
            settings.write_text(json.dumps({"model": "sonnet"}), encoding="utf-8")
        return original()

    monkeypatch.setattr(installer, "_read_settings", racing)
    with pytest.raises(SetupError, match="changed during install"):
        installer.apply(_bindings(tmp_path))
    assert _read(settings) == {"model": "sonnet"}


def test_a_missing_settings_file_is_created_private_and_links_are_refused(
    tmp_path: Path,
) -> None:
    claude = tmp_path / "claude"
    claude.mkdir()
    settings = claude / "settings.json"
    installer = ClaudeHookInstaller(settings, tmp_path / "state", claude_hook_policy())
    installer.apply(_bindings(tmp_path))
    assert os.stat(settings).st_mode & 0o777 == 0o600
    assert set(_read(settings)["hooks"]) == {"SessionStart", "Stop", "PreCompact"}
    linked = claude / "linked.json"
    linked.symlink_to(settings)
    with pytest.raises(SetupError, match="symlink"):
        ClaudeHookInstaller(linked, tmp_path / "state2", claude_hook_policy()).plan(())


def test_a_receipt_for_another_settings_file_or_bad_json_is_refused(tmp_path: Path) -> None:
    installer, settings = _setup(tmp_path)
    installer.apply(_bindings(tmp_path))
    other = tmp_path / "claude" / "other.json"
    with pytest.raises(SetupError, match="another settings file"):
        ClaudeHookInstaller(other, tmp_path / "state", claude_hook_policy()).plan(())
    settings.write_text("{not json", encoding="utf-8")
    with pytest.raises(SetupError, match="not valid JSON"):
        installer.plan(())


def test_a_fresh_login_without_the_settings_directory_gets_one_private_level(
    tmp_path: Path,
) -> None:
    # VM1015, 2026-11-23: Claude Code makes ~/.claude on its first run, so a fresh install had none.
    settings = tmp_path / "claude" / "settings.json"
    installer = ClaudeHookInstaller(settings, tmp_path / "state", claude_hook_policy())
    assert installer.apply(_bindings(tmp_path))["changed"] is True
    assert os.stat(settings.parent).st_mode & 0o777 == 0o700
    assert set(_read(settings)["hooks"]) == {"SessionStart", "Stop", "PreCompact"}
    deep = tmp_path / "missing" / "claude" / "settings.json"
    with pytest.raises(SetupError, match="location does not exist"):
        ClaudeHookInstaller(deep, tmp_path / "state3", claude_hook_policy()).apply(
            _bindings(tmp_path)
        )
    assert not (tmp_path / "missing").exists()
    blocked = tmp_path / "file-in-the-way"
    blocked.write_text("", encoding="utf-8")
    with pytest.raises(SetupError, match="not a directory"):
        ClaudeHookInstaller(blocked / "settings.json", tmp_path / "s4", claude_hook_policy()).apply(
            _bindings(tmp_path)
        )
