"""CPU model-unit argv is explicit, loopback-only and secret-reference-only."""

import json

import pytest
from pydantic import ValidationError

from anamnema.model_runtime import CPUModel, ModelRuntime, RuntimeToken, model_units
from anamnema.setup_config import load_config
from anamnema.worker_unit import render_worker_unit, worker_unit_policy


@pytest.fixture
def runtime(tmp_path):
    model = CPUModel(
        file=tmp_path / "fixture.gguf",
        alias="fixture-model",
        credential=RuntimeToken(entry="model/fixture", field="token"),
        context=512,
        batch=512,
        micro_batch=256,
        threads=2,
    )
    return ModelRuntime(
        executable=tmp_path / "anamnema",
        llama_server=tmp_path / "llama-server",
        cwd=tmp_path,
        setup_file=tmp_path / "setup.json",
        archive_file=tmp_path / "archive.json",
        encoder=model,
        notes=model.model_copy(update={"context": 8192}),
        reranker_directory=tmp_path / "reranker",
        service_file=tmp_path / "service.json",
        search_file=tmp_path / "search.json",
        onnx_file=tmp_path / "onnx.json",
    )


def test_runtime_units_are_pure_cpu_only_and_use_runtime_keyring_lookup(runtime):
    units = model_units(runtime, load_config())
    assert len(units) == 3 and not runtime.executable.exists()
    for name in ("encoder", "notes"):
        unit = units[f"anamnema-{name}.service"]
        assert unit.scope == "user" and unit.account is None
        args = unit.command
        assert args[1:3] == ("keyring", "run") and "--apply" in args
        assert args[args.index("--secret") + 1] == "LLAMA_API_KEY=model/fixture:token"
        assert args[args.index("--device") + 1] == "none"
        assert args[args.index("--n-gpu-layers") + 1] == "0"
        assert args[args.index("--host") + 1] == "127.0.0.1"
        assert "--api-key" not in args
        text = render_worker_unit(unit, worker_unit_policy())
        assert "KillMode=control-group" in text and "Environment=" not in text
    assert "--embedding" in units["anamnema-encoder.service"].command
    assert "--jinja" in units["anamnema-notes.service"].command
    assert "serve-reranker" in units["anamnema-reranker.service"].command


@pytest.mark.parametrize(
    "change", [{"field": "token:other"}, {"entry": "line\nbreak"}, {"entry": ""}]
)
def test_runtime_reference_cannot_change_the_keyring_command(change):
    with pytest.raises(ValidationError):
        RuntimeToken.model_validate_json(json.dumps({"entry": "model", "field": "token", **change}))


def test_model_batch_and_paths_are_validated(runtime):
    raw = runtime.model_dump(mode="json")
    raw["encoder"]["micro_batch"] = 1024
    with pytest.raises(ValidationError):
        ModelRuntime.model_validate_json(json.dumps(raw))
    raw = runtime.model_dump(mode="json")
    raw["setup_file"] = "relative.json"
    with pytest.raises(ValidationError):
        ModelRuntime.model_validate_json(json.dumps(raw))
