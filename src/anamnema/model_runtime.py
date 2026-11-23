"""First-profile CPU model units; no GPU driver setup or implicit model selection."""

from pathlib import Path
from typing import Self

from pydantic import Field, model_validator

from anamnema.setup_config import Nonempty, SetupConfig, StrictModel
from anamnema.ssh_tunnel import ModelTunnel
from anamnema.worker_unit import WorkerUnit


class RuntimeToken(StrictModel):
    entry: Nonempty
    field: Nonempty

    @model_validator(mode="after")
    def reference_only(self) -> Self:
        if ":" in self.field or any(
            ord(character) < 33 or ord(character) > 126
            for value in (self.entry, self.field)
            for character in value
        ):
            raise ValueError("runtime credential selectors must be plain nonsecret references")
        return self


class CPUModel(StrictModel):
    file: Path
    alias: Nonempty
    credential: RuntimeToken
    context: int = Field(gt=0)
    batch: int = Field(gt=0)
    micro_batch: int = Field(gt=0)
    threads: int = Field(gt=0)

    @model_validator(mode="after")
    def bounded_batch(self) -> Self:
        if self.micro_batch > self.batch or self.batch > self.context:
            raise ValueError("model batch bounds must fit its context")
        return self


class ModelRuntime(StrictModel):
    executable: Path
    llama_server: Path
    cwd: Path
    setup_file: Path
    archive_file: Path
    encoder: CPUModel
    notes: CPUModel | None
    notes_tunnel: ModelTunnel | None = None
    external_notes: bool = False
    reranker_directory: Path
    service_file: Path
    search_file: Path
    onnx_file: Path

    @model_validator(mode="after")
    def explicit_paths(self) -> Self:
        if sum((self.notes is not None, self.notes_tunnel is not None, self.external_notes)) != 1:
            raise ValueError("select exactly one local notes runtime or authenticated notes tunnel")
        paths = (
            self.executable,
            self.llama_server,
            self.cwd,
            self.setup_file,
            self.archive_file,
            self.encoder.file,
            self.reranker_directory,
            self.service_file,
            self.search_file,
            self.onnx_file,
        )
        if self.notes is not None:
            paths += (self.notes.file,)
        if any(not path.is_absolute() or ".." in path.parts for path in paths):
            raise ValueError("model runtime paths must be explicit absolute paths")
        return self


def model_units(config: ModelRuntime, setup: SetupConfig) -> dict[str, WorkerUnit]:
    """Pure argv assembly; bearer values are resolved by the running user unit, not here."""
    units: dict[str, WorkerUnit] = {}
    for name, model, endpoint in (
        ("encoder", config.encoder, setup.services.encoder),
        ("notes", config.notes, setup.services.notes),
    ):
        if model is None:
            continue
        arguments = (
            str(config.llama_server),
            "--model",
            str(model.file),
            "--alias",
            model.alias,
            "--host",
            endpoint.host,
            "--port",
            str(endpoint.port),
            "--device",
            "none",
            "--n-gpu-layers",
            "0",
            "--ctx-size",
            str(model.context),
            "--batch-size",
            str(model.batch),
            "--ubatch-size",
            str(model.micro_batch),
            "--threads",
            str(model.threads),
            "--parallel",
            "1",
        )
        if name == "encoder":
            arguments += ("--embedding", "--pooling", "mean", "--embd-normalize", "2")
        else:
            arguments += ("--jinja",)
        command = (
            str(config.executable),
            "keyring",
            "run",
            "--config",
            str(config.setup_file),
            "--archive-config",
            str(config.archive_file),
            "--secret",
            "LLAMA_API_KEY=" + model.credential.entry + ":" + model.credential.field,
            "--apply",
            "--",
            *arguments,
        )
        units[f"anamnema-{name}.service"] = WorkerUnit(
            description=f"Anamnema CPU {name} runtime",
            command=command,
            cwd=config.cwd,
            scope="user",
        )
    if config.notes_tunnel is not None:
        units["anamnema-notes-tunnel.service"] = config.notes_tunnel.unit(
            setup.services.notes, config.cwd
        )
    units["anamnema-reranker.service"] = WorkerUnit(
        description="Anamnema CPU reranker runtime",
        command=(
            str(config.executable),
            "serve-reranker",
            "--config",
            str(config.setup_file),
            "--archive-config",
            str(config.archive_file),
            "--service-config",
            str(config.service_file),
            "--search-config",
            str(config.search_file),
            "--onnx-config",
            str(config.onnx_file),
            "--model-dir",
            str(config.reranker_directory),
        ),
        cwd=config.cwd,
        scope="user",
    )
    return units
