"""Explicit remote identity assertion; never confused with hashing the server's model file."""

from pathlib import Path, PurePosixPath
from typing import Annotated, Literal, Self

from pydantic import Field, JsonValue, ValidationError, model_validator

from anamnema.archive_models import canonical
from anamnema.setup_config import Endpoint, Nonempty, SetupError, StrictModel


class RemoteModelAttestation(StrictModel):
    schema_version: Literal[1]
    verification: Literal["operator-attested-remote"]
    transport: Literal["ssh-loopback"]
    ssh_host_key_sha256: Annotated[str, Field(pattern=r"^SHA256:[A-Za-z0-9+/]{43}$")]
    endpoint: Endpoint
    model_path: Nonempty
    sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    alias: Nonempty
    build: JsonValue
    generation: dict[str, JsonValue]

    @model_validator(mode="after")
    def exact_identity(self) -> Self:
        path = PurePosixPath(self.model_path)
        context = self.generation.get("n_ctx")
        if (
            not path.is_absolute()
            or ".." in path.parts
            or any(ord(c) < 32 for c in self.model_path)
        ):
            raise ValueError("remote model path must be explicit and absolute")
        if self.build in (None, "", {}, []) or type(context) is not int or context <= 0:
            raise ValueError("remote attestation needs build and generation context")
        canonical(self.build)
        canonical(self.generation)
        return self


def read_attestation(path: Path, maximum: int) -> RemoteModelAttestation:
    try:
        with path.open("rb") as stream:
            raw = stream.read(maximum + 1)
        if len(raw) > maximum:
            raise SetupError("remote model attestation exceeds configured byte bound")
        return RemoteModelAttestation.model_validate_json(raw)
    except (OSError, ValidationError) as exc:
        raise SetupError("remote model attestation cannot be read or validated") from exc
