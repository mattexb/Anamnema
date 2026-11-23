"""Runtime integration planning and credential wiring without host-service changes."""

import pytest

from anamnema.extraction_config import extraction_config
from anamnema.ingest_config import archive_settings
from anamnema.model_runtime import CPUModel, ModelRuntime, RuntimeToken
from anamnema.native_commands import NativeCommandPolicy, NativeRunner
from anamnema.resource_download import DownloadPolicy
from anamnema.runtime_install import RuntimeInstallation, RuntimeInstaller
from anamnema.search_services import load_search_services
from anamnema.service_config import Access, load_service
from anamnema.setup_config import SetupError, load_config
from anamnema.user_services import UserServices


@pytest.fixture
def installer(tmp_path):
    setup = load_config().model_copy(update={"backend": "cpu"})
    search = load_search_services()
    archive = archive_settings()
    execution = extraction_config().model_copy(
        update={"model_keyring_entry": "notes/local", "model_keyring_field": "token"}
    )
    service = load_service().model_copy(
        update={
            "bind": setup.services.reranker,
            "access": (
                Access(
                    instance="fixture",
                    entry=search.reranker_keyring_entry,
                    field=search.token_field,
                ),
            ),
        }
    )
    paths = {}
    for name, value in (
        ("setup", setup),
        ("search", search),
        ("archive", archive),
        ("execution", execution),
        ("service", service),
    ):
        path = tmp_path / (name + ".json")
        path.write_text(value.model_dump_json())
        paths[name] = path
    encoder = CPUModel(
        file=tmp_path / "e5.gguf",
        alias=search.encoder_model,
        credential=RuntimeToken(entry=search.encoder_keyring_entry, field=search.token_field),
        context=512,
        batch=512,
        micro_batch=512,
        threads=2,
    )
    notes = encoder.model_copy(
        update={
            "alias": "notes",
            "file": tmp_path / "notes.gguf",
            "credential": RuntimeToken(entry="notes/local", field="token"),
        }
    )
    runtime = ModelRuntime(
        executable=tmp_path / "anamnema",
        llama_server=tmp_path / "llama-server",
        cwd=tmp_path,
        setup_file=paths["setup"],
        archive_file=paths["archive"],
        encoder=encoder,
        notes=notes,
        reranker_directory=tmp_path / "reranker",
        service_file=paths["service"],
        search_file=paths["search"],
        onnx_file=tmp_path / "onnx.json",
    )
    config = RuntimeInstallation(
        instance="fixture",
        model_root=tmp_path / "models",
        runtime=runtime,
        manager=UserServices(
            units_directory=tmp_path / "units",
            state_directory=tmp_path / "receipts",
            systemctl=tmp_path / "fixture-manager",
            command_seconds=2.0,
            max_state_bytes=100000,
            max_units=4,
        ),
        downloads=DownloadPolicy(chunk_bytes=1024, idle_seconds=1.0, transfer_seconds=10.0),
        extraction_file=paths["execution"],
        notes_identity_file=notes.file,
        readiness_seconds=1.0,
        poll_seconds=0.1,
    )
    runner = NativeRunner(
        tmp_path,
        NativeCommandPolicy(
            max_output_bytes=100000,
            max_input_bytes=100000,
            read_chunk_bytes=1024,
            timeout_seconds=2.0,
        ),
    )
    return RuntimeInstaller(config, runner)


def test_plan_is_read_only_with_no_secret_or_network_access(installer, monkeypatch):
    monkeypatch.setattr(installer.secrets, "lookup", lambda *_: pytest.fail("secret in plan"))
    report = installer.plan()
    assert report["ready"] is False and report["notes_identity"] == "local-file"
    assert len(report["units"]) == 3
    assert not installer.config.manager.state_directory.exists()
    assert not installer.config.manager.units_directory.exists()


def test_apply_checks_credentials_before_unit_changes_and_then_health(installer, monkeypatch):
    selected = []
    monkeypatch.setattr(
        installer.secrets,
        "lookup",
        lambda entry, field: selected.append((entry, field)) or "synthetic",
    )
    effects = []
    monkeypatch.setattr(installer.units, "apply", lambda units: effects.append(tuple(units)))
    monkeypatch.setattr(installer, "verify", lambda: {"verified": True})
    assert installer.apply(allow_downloads=False) == {"ready": True, "units": 3, "downloaded": 0}
    assert set(selected) == {
        ("encoder/local", "token"),
        ("notes/local", "token"),
        ("reranker/local", "token"),
    }
    assert len(effects) == 1


def test_missing_credentials_never_create_or_start_units(installer, monkeypatch):
    def missing(*_):
        raise SetupError("locked keyring")

    monkeypatch.setattr(installer.secrets, "lookup", missing)
    monkeypatch.setattr(installer.units, "apply", lambda *_: pytest.fail("unit before credential"))
    with pytest.raises(SetupError, match="locked keyring"):
        installer.apply(allow_downloads=False)
    assert not installer.config.manager.state_directory.exists()


def test_wrong_encoder_selector_is_refused_before_any_effect(installer):
    runtime = installer.config.runtime.model_copy(
        update={
            "encoder": installer.config.runtime.encoder.model_copy(
                update={"credential": RuntimeToken(entry="other", field="token")}
            )
        }
    )
    with pytest.raises(SetupError, match="encoder runtime credential"):
        RuntimeInstaller(installer.config.model_copy(update={"runtime": runtime}), installer.runner)
