"""Pinned host keys and fixed SSH argv; no network or account configuration changes."""

import base64
import hashlib
import json
import shutil
import struct
import subprocess

import pytest
from pydantic import ValidationError

from anamnema.setup_config import Endpoint
from anamnema.ssh_tunnel import ModelTunnel


def tunnel(tmp_path):
    algorithm = b"ssh-ed25519"
    raw = struct.pack(">I", len(algorithm)) + algorithm + struct.pack(">I", 32) + b"\x02" * 32
    return ModelTunnel(
        host="gpu.example.org",
        user="operator",
        port=22,
        model_port=18083,
        private_key=tmp_path / "operator-key",
        known_hosts_file=tmp_path / "known_hosts",
        public_host_key="ssh-ed25519 " + base64.b64encode(raw).decode(),
        expected_host_key_sha256="SHA256:"
        + base64.b64encode(hashlib.sha256(raw).digest()).decode().rstrip("="),
        ssh=tmp_path / "ssh",
        alive_seconds=30,
        missed_keepalives=3,
    )


def test_tunnel_is_pure_and_does_not_inherit_config_or_forward_agent(tmp_path):
    config = tunnel(tmp_path)
    unit = config.unit(Endpoint(host="127.0.0.1", port=19083), tmp_path)
    assert unit.command[1:5] == ("-F", "/dev/null", "-N", "-T")
    assert "127.0.0.1:19083:127.0.0.1:18083" in unit.command
    for fixed in (
        "ForwardAgent=no",
        "BatchMode=yes",
        "StrictHostKeyChecking=yes",
        "ExitOnForwardFailure=yes",
    ):
        assert fixed in unit.command
    assert config.known_hosts() == config.host_alias + " " + config.public_host_key + "\n"
    assert not config.known_hosts_file.exists() and not config.private_key.exists()


def test_native_parser_accepts_the_pinned_known_hosts_file(tmp_path):
    config = tunnel(tmp_path)
    config.known_hosts_file.write_text(config.known_hosts())
    parser = shutil.which("ssh-keygen")
    assert parser is not None, "ssh-keygen is required for native trust-file parsing"
    result = subprocess.run(  # noqa: S603 - native offline parser, synthetic public key only
        [parser, "-l", "-f", str(config.known_hosts_file)],
        capture_output=True,
        text=True,
        check=False,
        timeout=5,
    )
    assert result.returncode == 0 and config.expected_host_key_sha256 in result.stdout
    args = config.unit(Endpoint(host="::1", port=19083), tmp_path).command
    assert "[::1]:19083:127.0.0.1:18083" in args


@pytest.mark.parametrize(
    "changes",
    [
        {"host": "-oProxyCommand=bad"},
        {"host": "host\nother"},
        {"expected_host_key_sha256": "SHA256:" + "A" * 43},
        {"public_host_key": "ssh-ed25519 not-base64"},
        {"private_key": "relative"},
        {"public_host_key": "@cert-authority ssh-ed25519 AAAA"},
    ],
)
def test_invalid_tunnel_refuses_before_any_connection(tmp_path, changes):
    raw = tunnel(tmp_path).model_dump(mode="json")
    with pytest.raises(ValidationError):
        ModelTunnel.model_validate_json(json.dumps({**raw, **changes}))
