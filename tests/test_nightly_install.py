"""The nightly oneshot and timer parse natively, never restart, and own only their two files."""

import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from anamnema.nightly_install import NightlyInstaller, nightly_timer_policy, render_nightly
from anamnema.setup_config import SetupError


def _bodies(tmp_path: Path, **policy: Any) -> dict[str, str]:
    executable = tmp_path / "bin with space" / "anamnema"
    executable.parent.mkdir(exist_ok=True)
    executable.write_text(f"#!{sys.executable}\nraise SystemExit(0)\n", encoding="utf-8")
    executable.chmod(0o700)
    return render_nightly(
        executable,
        tmp_path / "nightly %h.json",
        ("--config", str(tmp_path / "setup.json")),
        tmp_path,
        nightly_timer_policy().model_copy(update=policy),
    )


@pytest.fixture
def installer(tmp_path: Path) -> NightlyInstaller:
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
    return NightlyInstaller(tmp_path / "units", tmp_path / "state", manager, nightly_timer_policy())


def _calls(tmp_path: Path) -> list[list[str]]:
    path = tmp_path / "calls"
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def test_the_oneshot_and_timer_pass_the_native_parser_and_never_restart(tmp_path: Path) -> None:
    verifier = shutil.which("systemd-analyze")
    assert verifier is not None, "systemd-analyze is required for native unit verification"
    bodies = _bodies(tmp_path)
    service, timer = bodies["anamnema-nightly.service"], bodies["anamnema-nightly.timer"]
    assert "Type=oneshot" in service and "Restart" not in service and "[Install]" not in service
    assert '/nightly %%h.json"' in service
    assert "OnCalendar=*-*-* 03:30:00" in timer and "WantedBy=timers.target" in timer
    for name, body in bodies.items():
        (tmp_path / name).write_text(body, encoding="utf-8")
    completed = subprocess.run(  # noqa: S603 - resolved native parser, synthetic units
        [verifier, "--man=no", "--generators=no", "verify", *(str(tmp_path / n) for n in bodies)],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert completed.returncode == 0, completed.stderr


def test_apply_repeat_upgrade_and_remove_touch_only_owned_files(
    installer: NightlyInstaller, tmp_path: Path
) -> None:
    bodies = _bodies(tmp_path)
    assert installer.plan(bodies)["changed"] is True
    assert installer.apply(bodies)["changed"] is True
    assert _calls(tmp_path) == [
        ["--user", "daemon-reload"],
        ["--user", "enable", "--now", "anamnema-nightly.timer"],
    ]
    assert installer.apply(bodies)["changed"] is False
    later = _bodies(tmp_path, on_calendar="*-*-* 04:00:00")
    assert installer.apply(later)["changed"] is True
    foreign = tmp_path / "units" / "someone-else.service"
    foreign.write_text("[Service]\n", encoding="utf-8")
    assert installer.remove() == {"removed": 2}
    assert sorted(p.name for p in (tmp_path / "units").iterdir()) == ["someone-else.service"]
    assert ["--user", "disable", "--now", "anamnema-nightly.timer"] in _calls(tmp_path)
    assert installer.remove() == {"removed": 0}


def test_a_foreign_or_hand_edited_unit_is_refused_without_change(
    installer: NightlyInstaller, tmp_path: Path
) -> None:
    bodies = _bodies(tmp_path)
    units = tmp_path / "units"
    units.mkdir(mode=0o700)
    (units / "anamnema-nightly.timer").write_text("[Timer]\n", encoding="utf-8")
    with pytest.raises(SetupError, match="did not write"):
        installer.apply(bodies)
    (units / "anamnema-nightly.timer").unlink()
    installer.apply(bodies)
    edited = units / "anamnema-nightly.service"
    edited.write_text(edited.read_text(encoding="utf-8") + "# mine\n", encoding="utf-8")
    with pytest.raises(SetupError, match="edited by hand"):
        installer.apply(_bodies(tmp_path, on_calendar="daily"))
    assert edited.read_text(encoding="utf-8").endswith("# mine\n")


def test_a_refused_systemctl_keeps_the_writing_receipt_and_a_retry_finishes(
    installer: NightlyInstaller, tmp_path: Path
) -> None:
    bodies = _bodies(tmp_path)
    (tmp_path / "fail").write_text("", encoding="utf-8")
    with pytest.raises(SetupError, match="refused"):
        installer.apply(bodies)
    assert json.loads(installer.receipt.read_text(encoding="utf-8"))["phase"] == "writing"
    (tmp_path / "fail").unlink()
    assert installer.apply(bodies)["changed"] is True
    assert json.loads(installer.receipt.read_text(encoding="utf-8"))["phase"] == "active"


def test_relative_paths_and_separators_are_refused(tmp_path: Path) -> None:
    policy = nightly_timer_policy()
    with pytest.raises(SetupError, match="absolute"):
        render_nightly(Path("anamnema"), tmp_path / "n.json", (), tmp_path, policy)
    with pytest.raises(SetupError, match="';'"):
        render_nightly(tmp_path / "a", tmp_path / "n.json", (";",), tmp_path, policy)
