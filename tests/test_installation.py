"""Installer composes real configuration/renderers; effects are injected for recovery tests."""

import json
import sys
from pathlib import Path

import pytest
from test_runtime_install import installer as installer

from anamnema.briefing_refresh import RefreshDeployment
from anamnema.capture_queue_config import CaptureDeployment, capture_queue_policy
from anamnema.continuity_config import ContinuityConfig
from anamnema.install_profile import InstallProfile
from anamnema.installation import NativeInstaller
from anamnema.judge_config import JudgeConfig
from anamnema.memory_config import load_memory
from anamnema.nightly import NightlyBriefings, NightlyConfig
from anamnema.onnx_ranker import load_onnx
from anamnema.recall_hook import RecallDeployment
from anamnema.service_config import Access, load_service
from anamnema.setup_config import SetupError
from anamnema.snapshot_policy import SnapshotWorkerConfig
from anamnema.worker_registry import RegistryConfig


@pytest.fixture
def installation(installer, tmp_path):
    runtime, config = installer, installer.config
    r = config.runtime
    r.onnx_file.write_text(load_onnx().model_dump_json())

    def save(name, value):
        path = tmp_path / (name + ".json")
        path.write_text(value.model_dump_json())
        return path

    memory_file = save("memory", load_memory())
    service_file = save(
        "memory-service",
        load_service().model_copy(
            update={"access": (Access(instance="fixture", entry="memory/local", field="token"),)}
        ),
    )
    identity = tmp_path / "identity.txt"
    identity.write_text("Synthetic installation identity.")
    continuity_file = save(
        "continuity",
        ContinuityConfig(
            instance="fixture", state_root=tmp_path / "continuity", identity_files=(identity,)
        ),
    )
    worker_file = save(
        "snapshot-worker",
        SnapshotWorkerConfig(
            executable=Path(sys.executable),
            cwd=tmp_path,
            input_root=tmp_path / "snapshots",
        ),
    )
    capture_file = save(
        "capture-deployment",
        CaptureDeployment(
            instance="fixture",
            state_root=tmp_path / "capture",
            cwd=tmp_path,
            transcript_roots=(tmp_path / "transcripts",),
            config=r.setup_file,
            archive_config=r.archive_file,
            memory_config=memory_file,
            search_config=r.search_file,
        ),
    )
    capture_policy_file = save("capture-policy", capture_queue_policy())
    recall_file = save(
        "recall-deployment",
        RecallDeployment(
            instance="fixture",
            cwd=tmp_path,
            config=r.setup_file,
            archive_config=r.archive_file,
            service_config=service_file,
        ),
    )
    refresh_file = save(
        "refresh-deployment",
        RefreshDeployment(
            instance="fixture",
            briefing_root=tmp_path / "briefings",
            stack_root=tmp_path / "stacks",
            state_directory=tmp_path / "nightly",
            continuity_config=continuity_file,
        ),
    )
    nightly_file = save(
        "nightly",
        NightlyConfig(
            instance="fixture",
            executable=r.executable,
            state_directory=tmp_path / "nightly",
            common=(
                "--config",
                str(r.setup_file),
                "--archive-config",
                str(r.archive_file),
                "--memory-config",
                str(memory_file),
            ),
            model_file=config.notes_identity_file,
            card_model_file=config.notes_identity_file,
            lesson_model_file=config.notes_identity_file,
            extraction_config=config.extraction_file,
            search_config=r.search_file,
            link_limit=10,
            naming_rounds=1,
            max_cards=5,
            lesson_batch=5,
            stage_deadline_seconds=30.0,
            max_output_bytes=100000,
            history=2,
        ),
    )
    dpkg = tmp_path / "fixture-dpkg"
    dpkg.write_text(f"#!{sys.executable}\nraise SystemExit(1)\n")
    dpkg.chmod(0o700)
    profile = InstallProfile(
        schema_version=1,
        instance="fixture",
        state_root=tmp_path / "installation",
        database_state=tmp_path / "database-state",
        runtime=config,
        native_commands=runtime.runner.policy,
        dpkg_query=dpkg,
        memory_file=memory_file,
        memory_service_file=service_file,
        continuity_file=continuity_file,
        snapshot_worker_file=worker_file,
        capture_deployment_file=capture_file,
        capture_policy_file=capture_policy_file,
        recall_deployment_file=recall_file,
        refresh_deployment_file=refresh_file,
        nightly_file=nightly_file,
        claude_settings=tmp_path / "settings.json",
    )
    profile_file = save("installation-profile", profile)
    return NativeInstaller(profile, profile_file)


def test_plan_keeps_settings_units_and_journals_absent(installation, monkeypatch):
    monkeypatch.setattr(installation.databases.probe, "exists", lambda *_: False)
    monkeypatch.setattr(installation.databases.probe, "nonempty_directory", lambda *_: False)
    monkeypatch.setattr(
        installation.runtime.secrets, "lookup", lambda *_: pytest.fail("secret in plan")
    )
    result = installation.plan()
    assert result["complete"] is False and result["mode"] == "dry-run"
    assert result["databases"]["mongodb"]["state"] == "absent"
    assert not installation.profile.state_root.exists()
    assert not installation.profile.claude_settings.exists()
    assert not installation.profile.runtime.manager.units_directory.exists()


def mocked_actions(installation, monkeypatch, calls, fail=None):
    monkeypatch.setattr(
        type(installation.runner),
        "__call__",
        lambda *_args, **_kwargs: pytest.fail("unmocked native command"),
    )
    for name in (
        "platform",
        "prerequisites",
        "databases",
        "models",
        "schema",
        "services",
        "hooks",
        "schedule",
        "verify",
    ):

        def action(*_, stage=name):
            calls.append(stage)
            if stage == fail:
                raise SetupError("synthetic failure")
            return {"verified": True}

        monkeypatch.setattr(installation, "_" + name, action)


def test_failure_stops_dependents_and_retry_replays_only_the_failed_effect(
    installation, monkeypatch
):
    calls = []
    mocked_actions(installation, monkeypatch, calls, fail="models")
    with pytest.raises(SetupError, match="synthetic failure"):
        installation.apply(allow_downloads=False)
    assert calls == ["platform", "prerequisites", "databases", "models"]
    calls.clear()
    mocked_actions(installation, monkeypatch, calls)
    report = installation.apply(allow_downloads=False)
    assert report["complete"] is True
    assert calls[:3] == ["models", "schema", "services"]
    assert "release_ready" not in report and "native_claude_acceptance" not in report


def test_explicit_pre_login_checkpoint_is_never_reported_as_complete(installation, monkeypatch):
    calls = []
    mocked_actions(installation, monkeypatch, calls)
    report = installation.apply(allow_downloads=False, through="services")
    assert report["complete"] is False
    assert calls == ["platform", "prerequisites", "databases", "models", "schema", "services"]


def test_configuration_change_is_refused_before_another_effect(installation, monkeypatch):
    calls = []
    mocked_actions(installation, monkeypatch, calls)
    installation.profile.runtime.runtime.archive_file.write_text("changed configuration")
    with pytest.raises(SetupError, match="configuration changed"):
        installation.apply(allow_downloads=False)
    assert not calls


@pytest.mark.parametrize(
    "field",
    ["card_model_file", "lesson_model_file", "common", "extraction_config", "search_config"],
)
def test_nightly_cannot_change_the_installed_models_or_profiles(installation, field, tmp_path):
    path = installation.profile.nightly_file
    data = json.loads(path.read_bytes())
    data[field] = (
        ["--config", str(tmp_path / "other.json")]
        if field == "common"
        else str(tmp_path / "other.json")
    )
    path.write_text(json.dumps(data))
    with pytest.raises(SetupError, match="nightly"):
        NativeInstaller(installation.profile, installation.profile_path)


def configure_registry(installation, registry, *, worker=True):
    path = installation.profile.continuity_file
    current = ContinuityConfig.model_validate_json(path.read_bytes())
    path.write_text(current.model_copy(update={"registry": registry}).model_dump_json())
    if worker:
        path = installation.profile.snapshot_worker_file
        current_worker = SnapshotWorkerConfig.model_validate_json(path.read_bytes())
        path.write_text(current_worker.model_copy(update={"registry": registry}).model_dump_json())


def test_explicit_shared_registry_is_bound_without_creating_its_state(installation):
    registry = RegistryConfig(
        records_dir=installation.profile.state_root / "worker-records",
        folder_roots=(installation.snapshot_worker.input_root,),
    )
    configure_registry(installation, registry)
    checked = NativeInstaller(installation.profile, installation.profile_path)
    assert checked.snapshot_worker.registry == checked.continuity.registry == registry
    assert not registry.records_dir.exists()


@pytest.mark.parametrize("mismatch", ["missing_worker", "records", "roots", "judge"])
def test_installer_refuses_split_registry_or_cleanup_ownership(installation, mismatch, tmp_path):
    registry = RegistryConfig(
        records_dir=(
            tmp_path / "other-records"
            if mismatch == "records"
            else installation.profile.state_root / "worker-records"
        ),
        folder_roots=(
            (tmp_path / "other-inputs",)
            if mismatch == "roots"
            else (installation.snapshot_worker.input_root,)
        ),
    )
    configure_registry(installation, registry, worker=mismatch != "missing_worker")
    if mismatch == "judge":
        judge_file = tmp_path / "judge.json"
        judge_file.write_text(
            JudgeConfig(
                executable=Path(sys.executable), cwd=tmp_path, model="fixture"
            ).model_dump_json()
        )
        path = installation.profile.nightly_file
        current = NightlyConfig.model_validate_json(path.read_bytes())
        path.write_text(
            current.model_copy(
                update={
                    "briefings": NightlyBriefings(
                        briefing_root=tmp_path / "briefings",
                        stack_root=tmp_path / "stacks",
                        judge_config=judge_file,
                        max_rebuilds=1,
                    )
                }
            ).model_dump_json()
        )
    with pytest.raises(SetupError, match="registr|cleanup roots"):
        NativeInstaller(installation.profile, installation.profile_path)
