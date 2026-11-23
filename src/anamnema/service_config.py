"""Nonsecret service routes, resource limits and instance-keyring selectors."""

from importlib.resources import files
from pathlib import Path
from typing import Annotated, Literal, Self

from pydantic import Field, ValidationError, model_validator

from anamnema.search_services import Route
from anamnema.setup_config import Endpoint, Nonempty, SetupError, StrictModel


class Access(StrictModel):
    """Only the keyring selector is configuration; never the bearer itself."""

    instance: Nonempty
    entry: Nonempty
    field: Nonempty


class ServiceSettings(StrictModel):
    """Every data and health route is authenticated independently of a proxy."""

    schema_version: Literal[1]
    bind: Endpoint
    health_path: Route
    openapi_path: Route
    search_path: Route
    note_path: Route
    source_path: Route
    correction_path: Route
    history_path: Route
    entity_path: Route
    card_path: Route
    unmerge_path: Route
    lesson_path: Route
    max_request_bytes: Annotated[int, Field(gt=0)]
    max_response_bytes: Annotated[int, Field(gt=0)]
    body_timeout_seconds: Annotated[float, Field(gt=0, allow_inf_nan=False)]
    requests_per_period: Annotated[int, Field(gt=0)]
    rate_period_seconds: Annotated[float, Field(gt=0, allow_inf_nan=False)]
    max_inflight: Annotated[int, Field(gt=0)]
    access: tuple[Access, ...]

    @model_validator(mode="after")
    def unambiguous(self) -> Self:
        """Reject route collisions and duplicate instance credentials."""
        routes = [
            self.health_path,
            self.openapi_path,
            self.search_path,
            self.note_path,
            self.source_path,
            self.correction_path,
            self.history_path,
            self.entity_path,
            self.card_path,
            self.unmerge_path,
            self.lesson_path,
        ]
        if len(set(routes)) != len(routes):
            raise ValueError("service routes must be distinct")
        if len({entry.instance for entry in self.access}) != len(self.access):
            raise ValueError("service access repeats an instance")
        return self


def load_service(path: Path | None = None) -> ServiceSettings:
    """An empty access list is a planning default and cannot start a service."""
    try:
        text = (
            path.read_text(encoding="utf-8")
            if path
            else files("anamnema")
            .joinpath("config/memory_service.json")
            .read_text(encoding="utf-8")
        )
        return ServiceSettings.model_validate_json(text)
    except (OSError, UnicodeError, ValidationError) as exc:
        raise SetupError(f"invalid service configuration ({type(exc).__name__})") from exc
