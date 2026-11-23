"""Complete producer limits and explicit fresh-worker deployment, without live settings reads."""

import json
from importlib.resources import files
from pathlib import Path
from typing import Literal, Self

from pydantic import Field, ValidationError, model_validator

from anamnema.setup_config import Nonempty, SetupError, StrictModel
from anamnema.worker_registry import RegistryConfig


class SnapshotProducerPolicy(StrictModel):
    """Native keys and worker arguments are owned by this producer, not global hooks."""

    max_transcript_bytes: int = Field(gt=0)
    max_dialogue_bytes: int = Field(gt=0)
    max_dialogue_files: int = Field(gt=0)
    max_header_bytes: int = Field(gt=0)
    max_output_bytes: int = Field(gt=0)
    read_chunk_bytes: int = Field(gt=0)
    timeout_seconds: float = Field(gt=0, allow_inf_nan=False)
    entry_cut_mark: str
    required_headings: tuple[Nonempty, ...]
    image_markers: tuple[Nonempty, ...]
    allowed_triggers: tuple[Nonempty, ...]
    hook_fields: dict[Literal["event", "session", "transcript", "trigger", "cwd"], Nonempty]
    hook_event: Nonempty
    native: dict[
        Literal["subtype", "boundary", "model", "synthetic", "session", "summary", "timestamp"],
        Nonempty,
    ]
    worker_arguments: tuple[str, ...]
    model_option: Literal["--model"]
    directory_option: Literal["--add-dir"]
    prompt_separator: Literal["--"]

    @model_validator(mode="after")
    def isolated(self) -> Self:
        """Keep the fixed security floor even with an explicit policy override."""
        arguments = self.worker_arguments
        required = {
            "--permission-mode": "dontAsk",
            "--tools": "Read",
            "--output-format": "text",
        }
        paired = {*required, "--settings", "--setting-sources", "--effort"}
        switches = {"--print", "--no-session-persistence", "--no-chrome"}
        remaining = iter(arguments)
        seen: set[str] = set()
        for flag in remaining:
            if flag in seen or flag not in paired | switches:
                raise ValueError("snapshot worker arguments repeat or add an unsafe option")
            seen.add(flag)
            if flag in paired:
                value = next(remaining, None)
                if value is None or value.startswith("--"):
                    raise ValueError("snapshot worker argument value is missing")
        try:
            if any(
                arguments.count(flag) != 1 or arguments[arguments.index(flag) + 1] != value
                for flag, value in required.items()
            ):
                raise ValueError("snapshot worker must be isolated and read-only")
            settings = json.loads(arguments[arguments.index("--settings") + 1])
            if settings != {"disableAllHooks": True}:
                raise ValueError("snapshot worker hooks must be disabled")
            if not {"--print", "--no-session-persistence", "--no-chrome"}.issubset(arguments):
                raise ValueError("snapshot worker cannot persist or attach to a live session")
            if arguments[arguments.index("--setting-sources") + 1] not in ("user", ""):
                raise ValueError("snapshot worker cannot load project settings")
        except (IndexError, json.JSONDecodeError) as exc:
            raise ValueError("snapshot worker arguments are incomplete") from exc
        if (
            not self.required_headings
            or not self.allowed_triggers
            or len(self.native) != 7
            or len(self.hook_fields) != 5
        ):
            raise ValueError("snapshot producer protocol is incomplete")
        return self


class SnapshotWorkerConfig(StrictModel):
    """No inferred executable, input root, cwd, or private model preference."""

    executable: Path
    cwd: Path
    input_root: Path
    model: Nonempty | None = None
    registry: RegistryConfig | None = None

    @model_validator(mode="after")
    def explicit_paths(self) -> Self:
        """The operator must choose real absolute paths; no shell or home expansion."""
        if any(not p.is_absolute() for p in (self.executable, self.cwd, self.input_root)):
            raise ValueError("snapshot worker paths must be explicit and absolute")
        if not self.executable.is_file() or not self.cwd.is_dir():
            raise ValueError("snapshot worker executable/cwd is unavailable")
        if any(
            c in str(p) for p in (self.cwd, self.input_root, self.executable) for c in ("\n", "\r")
        ):
            raise ValueError("snapshot worker paths must fit single lines")
        return self


def producer_policy(path: Path | None = None) -> SnapshotProducerPolicy:
    """Load public limits without reading native settings or credentials."""
    try:
        raw = (
            path.read_text(encoding="utf-8")
            if path
            else files("anamnema")
            .joinpath("config/snapshot_producer.json")
            .read_text(encoding="utf-8")
        )
        return SnapshotProducerPolicy.model_validate_json(raw)
    except (OSError, ValidationError) as exc:
        raise SetupError(f"invalid snapshot producer policy ({type(exc).__name__})") from exc


def worker_config(path: Path) -> SnapshotWorkerConfig:
    """Load only the explicit worker deployment file."""
    try:
        return SnapshotWorkerConfig.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, ValidationError) as exc:
        raise SetupError(f"invalid snapshot worker configuration ({type(exc).__name__})") from exc
