"""Strict, nonsecret configuration for the setup planning foundation."""

from __future__ import annotations

from importlib.resources import files
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from anamnema.setup_choices import SetupChoices

Nonempty = Annotated[str, Field(min_length=1)]
Port = Annotated[int, Field(ge=1, le=65535)]


class SetupError(ValueError):
    """Setup cannot safely interpret an input or perform the requested operation."""


class StrictModel(BaseModel):
    """Reject unknown fields and implicit conversion of configuration values."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class Target(StrictModel):
    """The reviewed installation target, not a generic Linux support promise."""

    system: Literal["Linux"]
    distribution: Literal["debian"]
    version: Literal["13"]
    mode: Literal["native"]
    python_minimum: tuple[int, int]


class Paths(StrictModel):
    """Prospective application folders; planning never creates them."""

    config: Nonempty
    data: Nonempty
    state: Nonempty
    models: Nonempty

    @field_validator("*")
    @classmethod
    def explicit_path(cls, value: str) -> str:
        """Require unambiguous absolute or current-user-relative destinations."""
        if not (value.startswith("~/") or Path(value).is_absolute()):
            raise ValueError("paths must be absolute or start with ~/")
        if ".." in Path(value).parts:
            raise ValueError("paths must not contain parent traversal")
        return value


class Endpoint(StrictModel):
    """A local bind location, deliberately without URI-embedded credentials."""

    host: Literal["127.0.0.1", "::1", "localhost"]
    port: Port


class Services(StrictModel):
    """Independent services in the single-machine architecture."""

    archive: Endpoint
    graph: Endpoint
    encoder: Endpoint
    reranker: Endpoint
    notes: Endpoint


class Keyring(StrictModel):
    """Namespace and executable check, not secret entries or an unlock probe."""

    provider: Literal["secret-service"]
    namespace: Nonempty
    executable: Nonempty


class Requirements(StrictModel):
    """Executable discovery only; no command listed here is executed."""

    common: tuple[Nonempty, ...]
    vulkan: tuple[Nonempty, ...]
    api: tuple[Nonempty, ...]


class SetupConfig(StrictModel):
    """Versioned setup inputs; the model-location switch is reserved from day one."""

    schema_version: Literal[1]
    substrate: Literal["claude"]
    backend: Literal["vulkan", "cuda", "api"]
    target: Target
    paths: Paths
    services: Services
    keyring: Keyring
    requirements: Requirements
    choices: SetupChoices = Field(default_factory=SetupChoices)

    @model_validator(mode="after")
    def distinct_ports(self) -> SetupConfig:
        """Avoid two planned services competing for the same local listener."""
        services = self.services
        ports = [
            endpoint.port
            for endpoint in (
                services.archive,
                services.graph,
                services.encoder,
                services.reranker,
                services.notes,
            )
        ]
        if len(set(ports)) != len(ports):
            raise ValueError("local service ports must be distinct")
        return self


def load_config(path: Path | None = None) -> SetupConfig:
    """Load the packaged defaults or a complete explicit configuration document."""
    try:
        text = (
            path.read_text(encoding="utf-8")
            if path is not None
            else files("anamnema").joinpath("config/setup.json").read_text(encoding="utf-8")
        )
        return parse_config(text)
    except (OSError, UnicodeError) as exc:
        raise SetupError(f"setup configuration cannot be read ({type(exc).__name__})") from exc


def parse_config(text: str) -> SetupConfig:
    """Validate saved/wizard/manual inputs with one value-redacting error path."""
    try:
        return SetupConfig.model_validate_json(text)
    except ValidationError as exc:
        # Error input/context may contain credentials supplied in the wrong field.
        problems: list[str] = []
        for error in exc.errors(include_input=False, include_context=False, include_url=False):
            location = ".".join(map(str, error["loc"]))
            if error["type"] == "extra_forbidden":
                location = "<unknown field>"
            problems.append(f"{location or '<document>'}: {error['type']}")
        raise SetupError("invalid setup configuration: " + "; ".join(problems)) from exc
