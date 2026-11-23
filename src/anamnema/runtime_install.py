"""Apply pinned model assets and same-user runtime units, then verify their real transports."""

from pathlib import Path
from typing import Self

from pydantic import Field, model_validator

from anamnema.archive_models import digest
from anamnema.extraction_config import extraction_config
from anamnema.ingest_config import archive_settings
from anamnema.llama_build import LlamaBuild, build_llama
from anamnema.llama_provider import LlamaProvider
from anamnema.model_runtime import ModelRuntime, model_units
from anamnema.native_commands import NativeRunner
from anamnema.private_files import PrivateFiles
from anamnema.readiness import wait_ready
from anamnema.resource_download import DownloadPolicy, PinnedResource, ResourceDownloads
from anamnema.search_services import CPUEncoder, CPUReranker, LocalJSON, load_search_services
from anamnema.secret_service import SecretService
from anamnema.service_config import load_service
from anamnema.setup_config import Nonempty, SetupError, StrictModel, load_config
from anamnema.user_services import UserServiceInstaller, UserServices
from anamnema.worker_unit import worker_unit_policy


class ModelAsset(StrictModel):
    directory: Path
    resource: PinnedResource


class RuntimeInstallation(StrictModel):
    instance: Nonempty
    model_root: Path
    runtime: ModelRuntime
    assets: tuple[ModelAsset, ...] = ()
    build: LlamaBuild | None = None
    manager: UserServices
    downloads: DownloadPolicy
    extraction_file: Path
    notes_identity_file: Path
    readiness_seconds: float = Field(gt=0, allow_inf_nan=False)
    poll_seconds: float = Field(gt=0, allow_inf_nan=False)

    @model_validator(mode="after")
    def bounded_coordinates(self) -> Self:
        if (
            not self.model_root.is_absolute()
            or not self.extraction_file.is_absolute()
            or not self.notes_identity_file.is_absolute()
            or self.poll_seconds > self.readiness_seconds
        ):
            raise ValueError("runtime installation coordinates or readiness bounds are invalid")
        destinations = [asset.directory / asset.resource.filename for asset in self.assets]
        if len(destinations) != len(set(destinations)) or any(
            not path.is_relative_to(self.model_root) or ".." in path.parts for path in destinations
        ):
            raise ValueError("model asset destinations must be distinct and inside the model root")
        if self.build is not None and self.build.binary != self.runtime.llama_server:
            raise ValueError("runtime binary must be the explicitly configured build artifact")
        return self


class RuntimeInstaller:
    def __init__(self, config: RuntimeInstallation, runner: NativeRunner) -> None:
        self.config, self.runner = config, runner
        self.setup = load_config(config.runtime.setup_file)
        self.archive = archive_settings(config.runtime.archive_file)
        self.search = load_search_services(config.runtime.search_file)
        self.service = load_service(config.runtime.service_file)
        self.execution = extraction_config(config.extraction_file)
        self.secrets = SecretService(self.setup.keyring, self.archive)
        self.units = UserServiceInstaller(config.manager, worker_unit_policy())
        if config.runtime.encoder.alias != self.search.encoder_model:
            raise SetupError("encoder runtime alias differs from the client model")
        if (config.runtime.encoder.credential.entry, config.runtime.encoder.credential.field) != (
            self.search.encoder_keyring_entry,
            self.search.token_field,
        ):
            raise SetupError("encoder runtime credential differs from the client selector")
        if self.service.bind != self.setup.services.reranker:
            raise SetupError("reranker service bind differs from the configured runtime")
        access = [item for item in self.service.access if item.instance == config.instance]
        if len(access) != 1 or (access[0].entry, access[0].field) != (
            self.search.reranker_keyring_entry,
            self.search.token_field,
        ):
            raise SetupError("reranker instance credential differs from its client selector")
        if config.runtime.notes is not None:
            if (
                self.execution.model_identity != "local-file"
                or config.notes_identity_file != config.runtime.notes.file
            ):
                raise SetupError("local notes runtime must use its exact local-file identity")
            selected = config.runtime.notes.credential
            if (selected.entry, selected.field) != (
                self.execution.model_keyring_entry,
                self.execution.model_keyring_field,
            ):
                raise SetupError("notes runtime credential differs from the client selector")
        elif (
            config.runtime.notes_tunnel is not None
            and self.execution.model_identity != "operator-attested-remote"
        ):
            raise SetupError("remote notes require explicit operator-attested model identity")

    def plan(self) -> dict[str, object]:
        """No network, secret lookup, directory creation or native command in a plan."""
        return {
            "units": sorted(self.units.plan(model_units(self.config.runtime, self.setup))),
            "assets": [
                {
                    "file": item.resource.filename,
                    "bytes": item.resource.size_bytes,
                    "revision": item.resource.revision,
                    "license": str(item.resource.license_url),
                }
                for item in self.config.assets
            ],
            "source_build": self.config.build is not None,
            "notes_identity": self.execution.model_identity,
            "ready": False,
        }

    def apply(self, *, allow_downloads: bool) -> dict[str, int | bool]:
        config = self.config
        if config.build is not None:
            build_llama(config.build, self.runner, allow_download=allow_downloads)
        downloaded = 0
        for asset in config.assets:
            downloads = ResourceDownloads(asset.directory, config.downloads)
            try:
                if allow_downloads:
                    downloaded += not downloads.fetch(asset.resource)[1]
                else:
                    downloads.inspect(asset.resource)
            except OSError as exc:
                raise SetupError("required model artifact is unavailable; cache retained") from exc
        self._credentials()
        tunnel = config.runtime.notes_tunnel
        if tunnel is not None:
            from anamnema.model_attestation import read_attestation

            attestation = read_attestation(
                config.notes_identity_file, self.execution.max_attestation_bytes
            )
            if attestation.ssh_host_key_sha256 != tunnel.expected_host_key_sha256:
                raise SetupError("model attestation and tunnel host key differ")
            private = PrivateFiles(0o700, 0o600, ".host-key-")
            private.directory(tunnel.known_hosts_file.parent)
            expected = tunnel.known_hosts().encode()
            if tunnel.known_hosts_file.exists() or tunnel.known_hosts_file.is_symlink():
                if (
                    private.read(tunnel.known_hosts_file, self.config.manager.max_state_bytes)
                    != expected
                ):
                    raise SetupError("model tunnel trust file differs; not replaced")
            else:
                private.save(tunnel.known_hosts_file, expected)
        units = model_units(config.runtime, self.setup)
        self.units.apply(units)
        wait_ready(self.verify, config.readiness_seconds, config.poll_seconds, "model runtimes")
        return {"ready": True, "units": len(units), "downloaded": downloaded}

    def _credentials(self) -> None:
        refs = {
            (
                self.config.runtime.encoder.credential.entry,
                self.config.runtime.encoder.credential.field,
            )
        }
        refs.update((entry.entry, entry.field) for entry in self.service.access)
        if self.execution.model_keyring_entry and self.execution.model_keyring_field:
            refs.add((self.execution.model_keyring_entry, self.execution.model_keyring_field))
        for entry, field in refs:
            self.secrets.lookup(entry, field)

    def verify(self) -> dict[str, object]:
        """Synthetic health inputs only; no notes, archive or private conversation is sent."""
        token = self.secrets.lookup(self.search.encoder_keyring_entry, self.search.token_field)
        encoder = CPUEncoder(
            LocalJSON(self.setup.services.encoder, self.search, token, self.search.timeout_seconds)
        )
        encoder.encode(["synthetic model readiness"], passage=False)
        token = self.secrets.lookup(self.search.reranker_keyring_entry, self.search.token_field)
        ranker = CPUReranker(
            LocalJSON(
                self.setup.services.reranker, self.search, token, self.search.rerank_timeout_seconds
            )
        )
        ranker.scores("synthetic readiness", ["synthetic model readiness"])
        token = None
        if self.execution.model_keyring_entry and self.execution.model_keyring_field:
            token = self.secrets.lookup(
                self.execution.model_keyring_entry, self.execution.model_keyring_field
            )
        notes = LlamaProvider(
            self.setup.services.notes, self.execution, self.config.notes_identity_file, token
        )
        return {
            "notes_identity": notes.identity,
            "encoder_and_reranker_verified": True,
            "profile": digest(self.config.model_dump(mode="json")),
        }
