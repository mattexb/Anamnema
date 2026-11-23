"""Explicit fresh native-judge deployment and a fixed tool-free security floor."""

import json
from importlib.resources import files
from pathlib import Path
from typing import Literal, Self

from pydantic import Field, ValidationError, model_validator

from anamnema.setup_config import Nonempty, SetupError, StrictModel
from anamnema.worker_registry import RegistryConfig


class JudgePolicy(StrictModel):
    """A fresh print worker receives evidence through stdin, never filesystem tools."""

    max_input_bytes: int = Field(gt=0)
    max_output_bytes: int = Field(gt=0)
    read_chunk_bytes: int = Field(gt=0)
    timeout_seconds: float = Field(gt=0, allow_inf_nan=False)
    worker_arguments: tuple[str, ...]
    model_option: Literal["--model"]
    schema_option: Literal["--json-schema"]

    @model_validator(mode="after")
    def tool_free(self) -> Self:
        """Overrides cannot enable tools, project settings, hooks, MCP or session persistence."""
        pairs = {
            "--permission-mode": "dontAsk",
            "--tools": "",
            "--output-format": "json",
            "--setting-sources": "",
            "--settings": '{"disableAllHooks":true}',
            "--mcp-config": '{"mcpServers":{}}',
        }
        switches = {"--print", "--no-session-persistence", "--no-chrome", "--strict-mcp-config"}
        seen: set[str] = set()
        arguments = iter(self.worker_arguments)
        for flag in arguments:
            if flag in seen or flag not in pairs.keys() | switches:
                raise ValueError("judge arguments repeat or add an unsafe option")
            seen.add(flag)
            if flag in pairs:
                value = next(arguments, None)
                if flag in {"--settings", "--mcp-config"}:
                    try:
                        valid = json.loads(value or "null") == json.loads(pairs[flag])
                    except ValueError:
                        valid = False
                else:
                    valid = value == pairs[flag]
                if not valid:
                    raise ValueError("judge arguments weaken the tool-free security floor")
        if seen != pairs.keys() | switches:
            raise ValueError("judge arguments lack required isolation options")
        return self


class JudgeConfig(StrictModel):
    """An operator chooses the executable, dedicated working directory and model explicitly."""

    executable: Path
    cwd: Path
    model: Nonempty
    registry: RegistryConfig | None = None

    @model_validator(mode="after")
    def explicit_paths(self) -> Self:
        """No shell expansion, relative executable or implicit project working directory."""
        if not self.executable.is_absolute() or not self.cwd.is_absolute():
            raise ValueError("judge paths must be absolute")
        if not self.executable.is_file() or not self.cwd.is_dir():
            raise ValueError("judge executable or working directory is unavailable")
        if any(c in str(p) for p in (self.executable, self.cwd) for c in ("\n", "\r")):
            raise ValueError("judge paths must fit single lines")
        return self


def judge_policy(path: Path | None = None) -> JudgePolicy:
    """Read public limits without consulting native settings, credentials or services."""
    try:
        raw = (
            path.read_text(encoding="utf-8")
            if path
            else files("anamnema").joinpath("config/judge.json").read_text(encoding="utf-8")
        )
        return JudgePolicy.model_validate_json(raw)
    except (OSError, UnicodeError, ValidationError) as exc:
        raise SetupError("judge policy cannot be read or validated") from exc
