"""Explicit source-pinned CPU build in a private owned directory, safe to retry."""

import hashlib
import os
from importlib.resources import as_file, files
from pathlib import Path
from typing import Literal, Self

from pydantic import Field, ValidationError, model_validator

from anamnema.archive_models import digest
from anamnema.native_commands import NativeRunner
from anamnema.private_files import PrivateFiles
from anamnema.setup_config import SetupError, StrictModel


class LlamaBuild(StrictModel):
    root: Path
    shell: Path
    git: Path = Path("/usr/bin/git")
    cmake: Path = Path("/usr/bin/cmake")
    revision: str = Field(pattern=r"^[0-9a-f]{40}$")
    jobs: int = Field(gt=0)
    timeout_seconds: float = Field(gt=0, allow_inf_nan=False)
    max_artifact_bytes: int = Field(gt=0)
    max_artifacts: int = Field(gt=0)
    hash_block_bytes: int = Field(gt=0)
    max_receipt_bytes: int = Field(gt=0)

    @model_validator(mode="after")
    def coordinates(self) -> Self:
        if any(
            not p.is_absolute() or ".." in p.parts
            for p in (self.root, self.shell, self.git, self.cmake)
        ):
            raise ValueError("CPU build paths must be explicit absolute paths")
        if self.shell.name != "bash":
            raise ValueError("CPU build recipe requires an explicit Bash executable")
        return self

    @property
    def binary(self) -> Path:
        return self.root / "source/build/bin/llama-server"


class BuildReceipt(StrictModel):
    version: Literal[1]
    inputs: str
    phase: Literal["prepared", "ready"]
    artifacts: dict[str, str]


def _artifacts(config: LlamaBuild) -> dict[str, str]:
    root = config.binary.parent.resolve(strict=True)
    if not root.is_relative_to(config.root.resolve(strict=True)):
        raise SetupError("CPU build output directory leaves its owned root")
    if not os.access(config.binary, os.X_OK):
        raise SetupError("CPU build executable is unavailable")
    result: dict[str, str] = {}
    size = 0
    for path in sorted(root.iterdir()):
        if len(result) >= config.max_artifacts:
            raise SetupError("CPU build artifact count exceeds configured bound")
        target = path.resolve(strict=True)
        if not target.is_relative_to(root) or not target.is_file():
            raise SetupError("CPU build artifact leaves its owned output directory")
        hashed = hashlib.sha256()
        with target.open("rb") as stream:
            while block := stream.read(config.hash_block_bytes):
                size += len(block)
                if size > config.max_artifact_bytes:
                    raise SetupError("CPU build artifacts exceed configured byte bound")
                hashed.update(block)
        result[path.name] = digest(
            [hashed.hexdigest(), os.readlink(path) if path.is_symlink() else None]
        )
    if "llama-server" not in result:
        raise SetupError("CPU build did not produce llama-server")
    return result


def build_llama(config: LlamaBuild, runner: NativeRunner, *, allow_download: bool) -> Path:
    """Source downloads/builds are explicit; a complete replay only verifies local outputs."""
    if os.getuid() == 0:
        raise SetupError("CPU model build must run as the logged-in user, not root")
    private = PrivateFiles(0o700, 0o600, ".llama-build-")
    private.directory(config.root)
    receipt_path = config.root / "build.json"
    script_hash = hashlib.sha256(
        files("anamnema").joinpath("resources/build_cpu_llama.sh").read_bytes()
    ).hexdigest()
    identity = digest(
        {
            "profile": config.model_dump(
                mode="json", include={"root", "shell", "git", "cmake", "revision"}
            ),
            "uid": os.getuid(),
            "recipe": script_hash,
        }
    )
    with private.locked(config.root / ".lock"):
        try:
            receipt = BuildReceipt.model_validate_json(
                private.read(receipt_path, config.max_receipt_bytes)
            )
        except FileNotFoundError:
            if any(p.name != ".lock" for p in config.root.iterdir()):
                raise SetupError("CPU build directory is not owned by this installer") from None
            receipt = BuildReceipt(version=1, inputs=identity, phase="prepared", artifacts={})
        except ValidationError as exc:
            raise SetupError("CPU build receipt is invalid") from exc
        if receipt.inputs != identity:
            raise SetupError(
                "CPU build receipt belongs to different inputs; use a new build directory"
            )
        if receipt.phase == "ready":
            if _artifacts(config) != receipt.artifacts:
                raise SetupError("CPU build artifacts changed; refusing silent rebuild")
            return config.binary
        if not allow_download:
            raise SetupError("CPU source download/build needs explicit consent")
        private.save(receipt_path, receipt.model_dump_json().encode())
        with as_file(files("anamnema").joinpath("resources/build_cpu_llama.sh")) as script:
            result = runner(
                [
                    str(config.shell),
                    str(script),
                    str(config.root),
                    str(config.git),
                    str(config.cmake),
                    config.revision,
                    str(config.jobs),
                ],
                None,
                config.timeout_seconds,
                None,
            )
        if result.code:
            raise SetupError("CPU source build failed; prepared receipt retained for retry")
        receipt = BuildReceipt(
            version=1, inputs=identity, phase="ready", artifacts=_artifacts(config)
        )
        raw = receipt.model_dump_json().encode()
        if len(raw) > config.max_receipt_bytes:
            raise SetupError("CPU build receipt exceeds configured bound")
        private.save(receipt_path, raw)
        return config.binary
