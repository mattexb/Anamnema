"""The actual setup path plans safely, rejects bad inputs and never installs."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from anamnema import cli, setup_plan
from anamnema.setup_config import SetupConfig, SetupError, load_config
from anamnema.setup_plan import Host, inspect_host, plan, required_commands


def good_host(config: SetupConfig) -> Host:
    return Host("Linux", "debian", "13", (3, 14), frozenset(required_commands(config)))


def write_config(tmp_path: Path, raw: dict[str, object]) -> Path:
    path = tmp_path / "setup.json"
    path.write_text(json.dumps(raw), encoding="utf-8")
    return path


def test_packaged_defaults_are_local_nonsecret_and_target_debian_native() -> None:
    config = load_config()
    assert (config.target.distribution, config.target.version, config.target.mode) == (
        "debian",
        "13",
        "native",
    )
    assert config.backend == "vulkan" and config.substrate == "claude"
    assert config.keyring.namespace == "anamnema"
    assert all(
        endpoint["host"] == "127.0.0.1" for endpoint in config.services.model_dump().values()
    )


@pytest.mark.parametrize("port", [0, 65536, "27017", True])
def test_invalid_or_coerced_ports_are_refused(tmp_path: Path, port: object) -> None:
    raw = load_config().model_dump(mode="json")
    raw["services"]["archive"]["port"] = port
    with pytest.raises(SetupError, match="invalid setup configuration"):
        load_config(write_config(tmp_path, raw))


@pytest.mark.parametrize("value", ["relative/place", "~/../other", "/data/../other", ""])
def test_ambiguous_paths_are_refused(tmp_path: Path, value: str) -> None:
    raw = load_config().model_dump(mode="json")
    raw["paths"]["data"] = value
    with pytest.raises(SetupError):
        load_config(write_config(tmp_path, raw))


def test_remote_hosts_and_duplicate_local_ports_are_refused(tmp_path: Path) -> None:
    raw = load_config().model_dump(mode="json")
    raw["services"]["archive"]["host"] = "example.invalid"
    with pytest.raises(SetupError):
        load_config(write_config(tmp_path, raw))
    raw = load_config().model_dump(mode="json")
    raw["services"]["archive"]["port"] = raw["services"]["graph"]["port"]
    with pytest.raises(SetupError, match="value_error"):
        load_config(write_config(tmp_path, raw))


@pytest.mark.parametrize("kind", ["unknown-key", "wrong-type", "broken-json"])
def test_config_errors_do_not_echo_secret_shaped_input(tmp_path: Path, kind: str) -> None:
    bait = "invented-secret-not-for-output"
    raw = load_config().model_dump(mode="json")
    if kind == "unknown-key":
        raw[bait] = bait
    else:
        raw["services"]["archive"]["port"] = bait
    path = write_config(tmp_path, raw)
    if kind == "broken-json":
        path.write_text('{"secret": "' + bait, encoding="utf-8")
    with pytest.raises(SetupError) as error:
        load_config(path)
    assert bait not in str(error.value)


def test_unreadable_and_incomplete_configuration_fail(tmp_path: Path) -> None:
    with pytest.raises(SetupError, match="FileNotFoundError"):
        load_config(tmp_path / "missing.json")
    with pytest.raises(SetupError, match="missing"):
        load_config(write_config(tmp_path, {"backend": "api"}))
    path = tmp_path / "binary.json"
    path.write_bytes(bytes([255]))
    with pytest.raises(SetupError, match="UnicodeDecodeError"):
        load_config(path)


def test_plan_order_is_explicit_and_cannot_claim_installation() -> None:
    config = load_config()
    report = plan(config, good_host(config))
    assert report["prerequisites_found"] is True
    assert report["ready_to_install"] is False
    assert [step["name"] for step in report["steps"]] == [
        "prerequisites",
        "keyring",
        "archive",
        "graph",
        "models",
        "application",
        "claude",
        "schedule",
        "verification",
    ]
    assert all(step["status"] == "not-implemented" for step in report["steps"])
    assert report["not_checked"] and report["blockers"]


@pytest.mark.parametrize(
    "system,distro,version,python",
    [
        ("Darwin", "unknown", "unknown", (3, 14)),
        ("Linux", "ubuntu", "24.04", (3, 14)),
        ("Linux", "debian", "12", (3, 14)),
        ("Linux", "debian", "13", (3, 13)),
    ],
)
def test_wrong_platform_or_python_is_not_ready(
    system: str, distro: str, version: str, python: tuple[int, int]
) -> None:
    config = load_config()
    host = Host(system, distro, version, python, frozenset(required_commands(config)))
    report = plan(config, host)
    assert report["prerequisites_found"] is False
    assert any(check["status"] == "unsupported" for check in report["checks"])


def test_api_mode_does_not_require_a_gpu_command_and_cuda_is_unsupported() -> None:
    config = load_config().model_copy(update={"backend": "api"})
    assert "vulkaninfo" not in required_commands(config)
    assert plan(config, good_host(config))["prerequisites_found"] is True
    config = config.model_copy(update={"backend": "cuda"})
    assert plan(config, good_host(config))["prerequisites_found"] is False


def test_missing_commands_are_named() -> None:
    config = load_config()
    report = plan(config, Host("Linux", "debian", "13", (3, 14), frozenset()))
    assert report["prerequisites_found"] is False
    assert {check["name"] for check in report["checks"] if check["status"] == "missing"} == {
        "claude",
        "secret-tool",
        "vulkaninfo",
    }


def test_host_inspection_only_discovers_not_executes(monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden(*args: object, **kwargs: object) -> None:
        pytest.fail("setup inspection must not run commands")

    monkeypatch.setattr(subprocess, "run", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(setup_plan.platform, "system", lambda: "Linux")
    monkeypatch.setattr(
        setup_plan.platform, "freedesktop_os_release", lambda: {"ID": "debian", "VERSION_ID": "13"}
    )
    monkeypatch.setattr(setup_plan.shutil, "which", lambda name: f"/fixture/{name}")
    config = load_config()
    host = inspect_host(config)
    assert host.distribution == "debian" and host.version == "13"
    assert host.commands == frozenset(required_commands(config))


def test_unavailable_os_metadata_is_visible(monkeypatch: pytest.MonkeyPatch) -> None:
    def unavailable() -> dict[str, str]:
        raise OSError("unavailable")

    monkeypatch.setattr(setup_plan.platform, "system", lambda: "Linux")
    monkeypatch.setattr(setup_plan.platform, "freedesktop_os_release", unavailable)
    report = plan(load_config(), inspect_host(load_config()))
    assert report["prerequisites_found"] is False
    assert "unknown" in report["checks"][0]["detail"]


def test_apply_fails_before_any_inspection_or_config_read(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def forbidden(*args: object) -> None:
        pytest.fail("apply must stop before inspecting or loading anything")

    monkeypatch.setattr(cli, "inspect_host", forbidden)
    monkeypatch.setattr(cli, "load_config", forbidden)
    assert cli.main(["setup", "--apply"]) == 2
    output = capsys.readouterr()
    assert output.out == "" and "not implemented" in json.loads(output.err)["error"]


@pytest.mark.parametrize("backend,expected", [("vulkan", 0), ("api", 0), ("cuda", 2)])
def test_cli_uses_selected_backend(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], backend: str, expected: int
) -> None:
    monkeypatch.setattr(cli, "inspect_host", good_host)
    assert cli.main(["setup", "--dry-run", "--backend", backend]) == expected
    report = json.loads(capsys.readouterr().out)
    assert report["backend"] == backend and report["ready_to_install"] is False


@pytest.mark.parametrize("args", [["--backend", "cuda"], ["--apply"], ["--backend", "api"]])
def test_installed_command_leaves_user_home_and_settings_unchanged(
    tmp_path: Path, args: list[str]
) -> None:
    home = tmp_path / "home"
    settings = home / ".claude/settings.json"
    settings.parent.mkdir(parents=True)
    settings.write_text('{"hooks": {"existing": "keep"}}', encoding="utf-8")
    before = {
        str(path.relative_to(home)): None if path.is_dir() else path.read_bytes()
        for path in home.rglob("*")
    }
    env = os.environ | {"HOME": str(home), "PYTHONDONTWRITEBYTECODE": "1"}
    command = str(Path(sys.executable).with_name("anamnema"))
    done = subprocess.run(  # noqa: S603 - installed local entry point and fixed test-case arguments
        [command, "setup", *args],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    assert done.returncode in (0, 2)
    assert "Traceback" not in done.stderr
    assert {
        str(path.relative_to(home)): None if path.is_dir() else path.read_bytes()
        for path in home.rglob("*")
    } == before
    if "--apply" in args:
        assert done.returncode == 2 and "not implemented" in json.loads(done.stderr)["error"]
    else:
        report = json.loads(done.stdout)
        assert report["mode"] == "dry-run" and report["ready_to_install"] is False
