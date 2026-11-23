"""Resumable private installation with a fixture manager, never host systemd changes."""

import json
import sys

import pytest

from anamnema.setup_config import SetupError
from anamnema.user_services import UserServiceInstaller, UserServices
from anamnema.worker_unit import WorkerUnit, worker_unit_policy


@pytest.fixture
def installation(tmp_path):
    manager = tmp_path / "fixture-systemctl"
    manager.write_text(
        f"#!{sys.executable}\n"
        "import json, pathlib, sys\n"
        "root = pathlib.Path(__file__).parent\n"
        "with (root / 'calls').open('a') as out:\n"
        "    out.write(json.dumps(sys.argv[1:]) + '\\n')\n"
        "raise SystemExit(int((root / 'fail').exists()))\n",
        encoding="utf-8",
    )
    manager.chmod(0o700)
    config = UserServices(
        units_directory=tmp_path / "units",
        state_directory=tmp_path / "state",
        systemctl=manager,
        command_seconds=2.0,
        max_state_bytes=100000,
        max_units=4,
    )
    unit = WorkerUnit(
        description="Synthetic worker",
        command=(sys.executable, "-c", "pass"),
        cwd=tmp_path,
        scope="user",
    )
    return UserServiceInstaller(config, worker_unit_policy()), {"anamnema-fixture.service": unit}


def calls(installer):
    path = installer.config.systemctl.parent / "calls"
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def test_plan_has_no_files_or_commands_and_apply_verifies_active(installation):
    installer, units = installation
    rendered = installer.plan(units)
    assert "KillMode=control-group" in rendered["anamnema-fixture.service"]
    assert not installer.config.units_directory.exists() and not installer.receipt.exists()
    assert not calls(installer)
    assert installer.apply(units) == {"active": True, "units": 1, "unchanged": False}
    assert [call[1] for call in calls(installer)] == [
        "daemon-reload",
        "enable",
        "restart",
        "is-active",
    ]
    path = installer.config.units_directory / "anamnema-fixture.service"
    assert path.read_text() == rendered[path.name] and path.stat().st_mode & 0o777 == 0o600
    assert installer.receipt.stat().st_mode & 0o777 == 0o600
    assert installer.apply(units)["unchanged"] is True
    assert calls(installer)[-1] == ["--user", "is-active", "--quiet", path.name]
    assert len(calls(installer)) == 5


@pytest.mark.parametrize("kind", ["file", "symlink", "directory"])
def test_unowned_target_is_never_adopted_or_overwritten(installation, kind):
    installer, units = installation
    installer.config.units_directory.mkdir()
    installer.config.units_directory.chmod(0o755)
    target = installer.config.units_directory / next(iter(units))
    if kind == "file":
        target.write_text("foreign")
    elif kind == "symlink":
        target.symlink_to(installer.config.systemctl)
    else:
        target.mkdir()
    with pytest.raises(SetupError, match="unowned"):
        installer.apply(units)
    assert not installer.receipt.exists() and not calls(installer)
    if kind == "file":
        assert target.read_text() == "foreign"


def test_activation_failure_keeps_installed_receipt_and_retries(installation):
    installer, units = installation
    fail = installer.config.systemctl.parent / "fail"
    fail.touch()
    with pytest.raises(SetupError, match="receipt retained"):
        installer.apply(units)
    assert json.loads(installer.receipt.read_text())["phase"] == "installed"
    target = installer.config.units_directory / next(iter(units))
    previous = target.stat().st_mtime_ns
    fail.unlink()
    installer.apply(units)
    assert target.stat().st_mtime_ns == previous
    assert json.loads(installer.receipt.read_text())["phase"] == "active"


def test_interrupted_file_install_replays_only_exact_prepared_plan(installation, monkeypatch):
    installer, units = installation
    save = installer.files.save
    target = installer.config.units_directory / next(iter(units))

    def interrupted(path, content, **kwargs):
        save(path, content, **kwargs)
        if path == target:
            raise InterruptedError("synthetic stop after first unit")

    monkeypatch.setattr(installer.files, "save", interrupted)
    with pytest.raises(InterruptedError):
        installer.apply(units)
    assert json.loads(installer.receipt.read_text())["phase"] == "prepared"
    monkeypatch.setattr(installer.files, "save", save)
    changed = {
        name: unit.model_copy(update={"description": "Changed"}) for name, unit in units.items()
    }
    with pytest.raises(SetupError, match="finish the interrupted"):
        installer.apply(changed)
    assert installer.apply(units)["active"] is True


def test_upgrade_edited_or_missing_owned_file_refuses_then_clean_upgrade_works(installation):
    installer, units = installation
    installer.apply(units)
    target = installer.config.units_directory / next(iter(units))
    original = target.read_bytes()
    target.write_text("operator edited")
    with pytest.raises(SetupError, match="edited"):
        installer.apply(units)
    target.unlink()
    with pytest.raises(SetupError, match="missing"):
        installer.apply(units)
    target.write_bytes(original)
    target.chmod(0o600)
    changed = {
        name: unit.model_copy(update={"description": "Changed"}) for name, unit in units.items()
    }
    assert installer.apply(changed)["unchanged"] is False
    assert "Description=Changed" in target.read_text()


def test_changed_names_or_receipt_scope_refuse(installation):
    installer, units = installation
    installer.apply(units)
    with pytest.raises(SetupError, match="changing owned service names"):
        installer.apply({"anamnema-other.service": next(iter(units.values()))})
    raw = json.loads(installer.receipt.read_text())
    raw["scope"] = "foreign"
    installer.files.save(installer.receipt, json.dumps(raw).encode())
    with pytest.raises(SetupError, match="scope differs"):
        installer.apply(units)


def test_plan_refuses_system_scope_invalid_names_and_bounds(installation):
    installer, units = installation
    unit = next(iter(units.values()))
    for proposed in ({}, {"foreign.service": unit}):
        with pytest.raises(SetupError):
            installer.plan(proposed)
    with pytest.raises(SetupError, match="logged-in user"):
        installer.plan({"anamnema-fixture.service": unit.model_copy(update={"scope": "system"})})
    installer.config = installer.config.model_copy(update={"max_state_bytes": 1})
    with pytest.raises(SetupError, match="receipt exceeds"):
        installer.plan(units)
    assert not installer.receipt.exists()


def test_entire_plan_is_preflighted_before_first_file_write(installation):
    installer, units = installation
    unit = next(iter(units.values()))
    units["anamnema-second.service"] = unit
    installer.config.units_directory.mkdir(mode=0o700)
    foreign = installer.config.units_directory / "anamnema-second.service"
    foreign.write_text("foreign")
    with pytest.raises(SetupError, match="unowned"):
        installer.apply(units)
    assert not (installer.config.units_directory / "anamnema-fixture.service").exists()
    assert not installer.receipt.exists() and not calls(installer)


def test_existing_publicly_writable_directory_is_not_repaired(installation):
    installer, units = installation
    installer.config.units_directory.mkdir()
    installer.config.units_directory.chmod(0o777)
    with pytest.raises(SetupError, match="not publicly writable"):
        installer.apply(units)
    assert installer.config.units_directory.stat().st_mode & 0o777 == 0o777
    assert not calls(installer)


def test_manager_timeout_retains_retryable_installation(installation):
    installer, units = installation
    manager = installer.config.systemctl
    manager.write_text(f"#!{sys.executable}\nimport time\ntime.sleep(2)\n")
    installer.config = installer.config.model_copy(update={"command_seconds": 0.05})
    with pytest.raises(SetupError, match="deadline"):
        installer.apply(units)
    assert json.loads(installer.receipt.read_text())["phase"] == "installed"


def test_abort_prepared_first_install_removes_only_owned_files(installation, monkeypatch):
    installer, units = installation
    save = installer.files.save
    target = installer.config.units_directory / next(iter(units))

    def stopped(path, content, **kwargs):
        save(path, content, **kwargs)
        if path == installer.receipt:
            raise InterruptedError("synthetic stop before first unit write")

    monkeypatch.setattr(installer.files, "save", stopped)
    with pytest.raises(InterruptedError):
        installer.apply(units)
    monkeypatch.setattr(installer.files, "save", save)
    foreign = installer.config.units_directory / "foreign.service"
    foreign.write_text("foreign")
    assert installer.rollback() == {"rolled_back": True, "active": False, "units": 0}
    assert not target.exists() and not installer.receipt.exists()
    assert foreign.read_text() == "foreign"
    changed = {
        name: unit.model_copy(update={"description": "new renderer"})
        for name, unit in units.items()
    }
    assert installer.apply(changed)["active"] is True


def test_failed_upgrade_keeps_old_bodies_and_rollback_restores_them(installation):
    installer, units = installation
    installer.apply(units)
    target = installer.config.units_directory / next(iter(units))
    old = target.read_text()
    changed = {
        name: unit.model_copy(update={"description": "broken upgrade"})
        for name, unit in units.items()
    }
    fail = installer.config.systemctl.parent / "fail"
    fail.touch()
    with pytest.raises(SetupError, match="receipt retained"):
        installer.apply(changed)
    receipt = json.loads(installer.receipt.read_text())
    assert receipt["previous"][target.name] == old
    assert receipt["desired"][target.name] != old
    fail.unlink()
    assert installer.rollback()["active"] is True
    assert target.read_text() == old
    assert installer.apply(units)["unchanged"] is True


def test_interrupted_rollback_is_resumable_and_edited_targets_refuse(installation, monkeypatch):
    installer, units = installation
    installer.apply(units)
    target = installer.config.units_directory / next(iter(units))
    old = target.read_bytes()
    changed = {
        name: unit.model_copy(update={"description": "upgrade"}) for name, unit in units.items()
    }
    fail = installer.config.systemctl.parent / "fail"
    fail.touch()
    with pytest.raises(SetupError):
        installer.apply(changed)
    fail.unlink()
    save = installer.files.save

    def interrupted(path, content, **kwargs):
        save(path, content, **kwargs)
        if path == target:
            raise InterruptedError("synthetic stop after old body restored")

    monkeypatch.setattr(installer.files, "save", interrupted)
    with pytest.raises(InterruptedError):
        installer.rollback()
    assert target.read_bytes() == old
    monkeypatch.setattr(installer.files, "save", save)
    with pytest.raises(SetupError, match="finish explicit"):
        installer.apply(units)
    assert installer.rollback()["active"] is True
    target.write_text("operator edit")
    with pytest.raises(SetupError, match="edited"):
        installer.remove()
    assert target.read_text() == "operator edit"


def test_remove_retries_after_unlink_and_keeps_foreign_units(installation, monkeypatch):
    installer, units = installation
    installer.apply(units)
    foreign = installer.config.units_directory / "foreign.service"
    foreign.write_text("foreign")
    command = installer._command

    def interrupted(*arguments, **kwargs):
        if arguments == ("daemon-reload",):
            raise SetupError("synthetic stop after unlink")
        command(*arguments, **kwargs)

    monkeypatch.setattr(installer, "_command", interrupted)
    with pytest.raises(SetupError, match="synthetic stop"):
        installer.remove()
    assert json.loads(installer.receipt.read_text())["phase"] == "removing"
    monkeypatch.setattr(installer, "_command", command)
    assert installer.remove() == {"removed": True, "units": 1}
    assert not installer.receipt.exists() and foreign.read_text() == "foreign"
    assert installer.remove() == {"removed": False, "units": 0}
