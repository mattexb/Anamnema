"""Offline native unit parser checks; no unit is installed, enabled, started or stopped."""

import json
import shutil
import subprocess
import sys

import pytest
from pydantic import ValidationError

from anamnema.worker_unit import (
    WorkerUnit,
    WorkerUnitPolicy,
    render_worker_unit,
    worker_unit_policy,
)


@pytest.mark.parametrize("scope,account", [("user", None), ("system", "fixtureuser")])
def test_native_systemd_parser_accepts_owned_worker_unit_in_temp_folder(tmp_path, scope, account):
    verifier = shutil.which("systemd-analyze")
    assert verifier is not None, "systemd-analyze is required for native unit syntax verification"
    executable = tmp_path / "fixture worker"
    executable.write_text(f"#!{sys.executable}\nraise SystemExit(0)\n", encoding="utf-8")
    executable.chmod(0o700)
    directory = tmp_path / "folder with spaces %t"
    directory.mkdir()
    unit = WorkerUnit(
        description="Synthetic Anamnema capture worker",
        command=(str(executable), "literal $HOME", "literal %t", 'literal "quotes"', "back\\slash"),
        cwd=directory,
        scope=scope,
        account=account,
    )
    body = render_worker_unit(unit, worker_unit_policy())
    path = tmp_path / "fixture-anamnema.service"
    path.write_text(body, encoding="utf-8")
    completed = subprocess.run(  # noqa: S603 - resolved native parser, synthetic unit, no execution
        [verifier, "--man=no", "--generators=no", "verify", str(path)],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert completed.returncode == 0, completed.stderr
    assert "KillMode=control-group\n" in body and "SendSIGKILL=yes\n" in body
    assert 'ExecStart=:"' in body and "literal $HOME" in body and "literal %%t" in body
    assert "Restart=always\n" in body and "NoNewPrivileges=yes\n" in body
    assert "WorkingDirectory=" + str(directory).replace("%", "%%") + "\n" in body
    assert ("User=" + account in body) if account else "User=" not in body


@pytest.mark.parametrize(
    "changes",
    [
        {"scope": "system", "account": None},
        {"scope": "system", "account": "root"},
        {"scope": "user", "account": "fixtureuser"},
        {"command": ("relative-executable",)},
        {"command": ("/fixture/executable", ";")},
        {"description": "injection\nExecStart=/fixture/other"},
        {"description": "continuation\\"},
    ],
)
def test_unsafe_unit_inputs_refuse_before_rendering(tmp_path, changes):
    values = {
        "description": "fixture",
        "command": ("/fixture/executable",),
        "cwd": tmp_path,
        "scope": "user",
    }
    with pytest.raises(ValidationError):
        WorkerUnit.model_validate({**values, **changes})


def test_policy_does_not_allow_permission_or_target_injection():
    data = worker_unit_policy().model_dump(mode="json")
    for changes in ({"umask": "0000"}, {"user_target": "default.target\nOther=bad"}):
        with pytest.raises(ValidationError):
            WorkerUnitPolicy.model_validate_json(json.dumps({**data, **changes}))
