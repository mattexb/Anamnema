"""Explicit pinned SSH loopback forwarding for an operator's existing model host."""

import base64
import binascii
import hashlib
from pathlib import Path
from typing import Self

from pydantic import Field, model_validator

from anamnema.archive_models import digest
from anamnema.setup_config import Endpoint, StrictModel
from anamnema.worker_unit import WorkerUnit


class ModelTunnel(StrictModel):
    host: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9.:-]*$")
    user: str = Field(pattern=r"^[a-z_][a-z0-9_-]*$")
    port: int = Field(gt=0, le=65535)
    model_port: int = Field(gt=0, le=65535)
    private_key: Path
    public_host_key: str = Field(min_length=1, max_length=16384)
    expected_host_key_sha256: str = Field(pattern=r"^SHA256:[A-Za-z0-9+/]{43}$")
    known_hosts_file: Path
    ssh: Path
    alive_seconds: int = Field(gt=0)
    missed_keepalives: int = Field(gt=0)

    @model_validator(mode="after")
    def pinned_host(self) -> Self:
        for path in (self.private_key, self.known_hosts_file, self.ssh):
            if not path.is_absolute() or ".." in path.parts:
                raise ValueError("SSH tunnel files must be explicit absolute paths")
        parts = self.public_host_key.split()
        if (
            len(parts) != 2
            or self.public_host_key != " ".join(parts)
            or parts[0] not in {"ssh-ed25519", "ecdsa-sha2-nistp256", "ssh-rsa"}
        ):
            raise ValueError("SSH tunnel requires one plain public host key without a comment")
        try:
            key = base64.b64decode(parts[1], validate=True)
        except (binascii.Error, ValueError) as exc:
            raise ValueError("SSH host key is not valid base64") from exc
        length = int.from_bytes(key[:4], "big")
        if len(key) <= 4 + length or key[4 : 4 + length] != parts[0].encode("ascii"):
            raise ValueError("SSH public key algorithm differs from its wire format")
        fingerprint = "SHA256:" + base64.b64encode(hashlib.sha256(key).digest()).decode().rstrip(
            "="
        )
        if fingerprint != self.expected_host_key_sha256:
            raise ValueError("SSH host key differs from the attested fingerprint")
        return self

    @property
    def host_alias(self) -> str:
        return "anamnema-model-" + digest([self.host, self.port])

    def known_hosts(self) -> str:
        """Dedicated trust file, never an edit to the operator's global SSH settings."""
        return self.host_alias + " " + self.public_host_key + "\n"

    def unit(self, endpoint: Endpoint, cwd: Path) -> WorkerUnit:
        """No agent forwarding, proxy command, public bind or inherited SSH configuration."""
        bind = "[::1]" if endpoint.host == "::1" else "127.0.0.1"
        command = (
            str(self.ssh),
            "-F",
            "/dev/null",
            "-N",
            "-T",
            "-p",
            str(self.port),
            "-i",
            str(self.private_key),
            "-o",
            "BatchMode=yes",
            "-o",
            "IdentitiesOnly=yes",
            "-o",
            "StrictHostKeyChecking=yes",
            "-o",
            "ExitOnForwardFailure=yes",
            "-o",
            "ForwardAgent=no",
            "-o",
            "PermitLocalCommand=no",
            "-o",
            "HostKeyAlias=" + self.host_alias,
            "-o",
            "UserKnownHostsFile=" + str(self.known_hosts_file),
            "-o",
            "GlobalKnownHostsFile=/dev/null",
            "-o",
            "ServerAliveInterval=" + str(self.alive_seconds),
            "-o",
            "ServerAliveCountMax=" + str(self.missed_keepalives),
            "-L",
            f"{bind}:{endpoint.port}:127.0.0.1:{self.model_port}",
            self.user + "@" + self.host,
        )
        return WorkerUnit(
            description="Anamnema authenticated model tunnel",
            command=command,
            cwd=cwd,
            scope="user",
        )
