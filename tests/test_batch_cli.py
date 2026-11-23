"""Actual local CLI preparation, resource assembly, resumable engines and offline status."""

import json
from pathlib import Path

import pytest
from test_memory_workflow import _cli_options
from test_memory_workflow import memory_workflow as memory_workflow

from anamnema.batch_config import WindowBatchConfig
from anamnema.batch_prepare import PreparedInputs, preparation_policy
from anamnema.cli import main


def deployment(tmp_path, model_file, owner="fixture", **changes):
    config = WindowBatchConfig(
        instance=owner,
        inputs_root=tmp_path / "inputs",
        jobs_root=tmp_path / "jobs",
        model_file=model_file,
        **changes,
    )
    path = tmp_path / "batch.json"
    path.write_text(config.model_dump_json(), encoding="utf-8")
    return path, config


def test_prepare_and_run_previews_never_load_setup_secrets_or_start_services(tmp_path, capsys):
    path, config = deployment(
        tmp_path, tmp_path / "absent-model.gguf", setup_config=tmp_path / "absent-setup.json"
    )
    source = tmp_path / "source.jsonl"
    source.write_text(
        json.dumps(
            {
                "type": "user",
                "timestamp": "2026-01-01T10:00:00Z",
                "message": "Synthetic owned decision.",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    args = [
        "batch-window",
        "prepare",
        "--deployment",
        str(path),
        "--file",
        str(source),
        "--session",
        "session-one",
    ]
    assert main(args) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["closed_windows"] == 0 and report["pending_records"] == 1
    assert not config.inputs_root.exists() and not config.jobs_root.exists()
    assert main([*args, "--closed"]) == 0
    assert json.loads(capsys.readouterr().out)["closed_windows"] == 1
    assert not config.inputs_root.exists()
    original = source.read_bytes()
    assert main([*args, "--closed", "--apply"]) == 0
    identity = json.loads(capsys.readouterr().out)["inputs"][0]
    assert source.read_bytes() == original
    assert main(["batch-window", "run", "--deployment", str(path), "--input", identity]) == 0
    assert json.loads(capsys.readouterr().out)["infrastructure_accessed"] is False
    assert not config.jobs_root.exists()


@pytest.mark.parametrize("memory_workflow", ["unloaded-three"], indirect=True)
def test_actual_cli_runs_core_resumes_and_reads_status_without_inputs_or_model(
    memory_workflow, model_service, tmp_path, capsys
):
    owner, original, engine, server, _, _, _, archive = memory_workflow
    options = _cli_options(owner, engine, server, archive, tmp_path, model_service.port)
    paths = {
        name: Path(options[options.index("--" + name) + 1])
        for name in ("config", "archive-config", "memory-config", "search-config")
    }
    setup = json.loads(paths["config"].read_text(encoding="utf-8"))
    setup["backend"] = "cpu"
    paths["config"].write_text(json.dumps(setup), encoding="utf-8")
    path, config = deployment(
        tmp_path,
        model_service.model_file,
        owner,
        setup_config=paths["config"],
        archive_config=paths["archive-config"],
        memory_config=paths["memory-config"],
        search_config=paths["search-config"],
    )
    source = tmp_path / "source.jsonl"
    source.write_text(json.dumps(original.source) + "\n", encoding="utf-8")
    assert (
        main(
            [
                "batch-window",
                "prepare",
                "--deployment",
                str(path),
                "--file",
                str(source),
                "--session",
                "session-one",
                "--closed",
                "--apply",
            ]
        )
        == 0
    )
    identity = json.loads(capsys.readouterr().out)["inputs"][0]
    arguments = ["batch-window", "run", "--deployment", str(path), "--input", identity, "--apply"]
    assert main(arguments) == 0
    first = json.loads(capsys.readouterr().out)
    assert first["complete"] and first["nightly_complete"] is False and first["raw_payload_removed"]
    assert [stage["name"] for stage in first["stages"]] == ["archive", "extract", "store"]
    calls = len(model_service.calls)
    assert main([*arguments, "--resume", first["job"]]) == 0
    assert json.loads(capsys.readouterr().out)["complete"] and len(model_service.calls) == calls
    PreparedInputs(config.inputs_root, preparation_policy()).path(owner, identity).unlink()
    model_service.model_file.unlink()
    paths["config"].unlink()
    assert main(["batch-window", "status", "--deployment", str(path), "--job", first["job"]]) == 0
    assert json.loads(capsys.readouterr().out)["complete"] and len(model_service.calls) == calls


def test_unknown_job_status_is_visible_and_creates_nothing(tmp_path, capsys):
    path, config = deployment(tmp_path, tmp_path / "absent-model.gguf")
    assert main(["batch-window", "status", "--deployment", str(path), "--job", "a" * 64]) == 2
    assert "absent or invalid" in json.loads(capsys.readouterr().err)["error"]
    assert not config.jobs_root.exists() and not config.inputs_root.exists()
