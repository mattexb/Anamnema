"""No inferred identity, permissive modes or malformed native protocol configuration."""

import json
import os

import pytest
from pydantic import ValidationError

from anamnema.continuity_config import ContinuityConfig, continuity_config, continuity_policy
from anamnema.continuity_state import source_text
from anamnema.setup_config import SetupError


@pytest.mark.parametrize(
    "change",
    [
        {"state_root": "relative"},
        {"state_root": "/"},
        {"identity_files": []},
        {"identity_files": ["relative"]},
        {"identity_files": ["/example", "/example"]},
        {"instance": "bad\ninstance"},
    ],
)
def test_deployment_refuses_implicit_paths_or_unsafe_identity(tmp_path, change):
    good = {
        "instance": "example",
        "state_root": str(tmp_path / "private"),
        "identity_files": [str(tmp_path / "identity")],
    }
    with pytest.raises(ValidationError):
        ContinuityConfig.model_validate_json(json.dumps({**good, **change}))


@pytest.mark.parametrize(
    "change",
    [
        {"file_mode": 0o644},
        {"directory_mode": 0o755},
        {"state_file": "../outside"},
        {"state_file": ".lock"},
        {"state_file": ".pending-state"},
        {"lock_file": ".pending-lock"},
        {"native_events": {"start": "SessionStart"}},
        {"native_fields": {}},
        {"session_pattern": "["},
        {"token_bytes": 0},
        {"temporary_prefix": "a"},
        {"temporary_prefix": ".[a-f]"},
        {"spool_directory": "state.json"},
    ],
)
def test_complete_policy_validation_does_not_relax_safety(tmp_path, change):
    file = tmp_path / "policy.json"
    file.write_text(
        json.dumps({**continuity_policy().model_dump(mode="json"), **change}), encoding="utf-8"
    )
    with pytest.raises(SetupError, match="invalid continuity policy"):
        continuity_policy(file)


def test_missing_config_and_policy_are_visible(tmp_path):
    with pytest.raises(SetupError, match="configuration"):
        continuity_config(tmp_path / "missing")
    with pytest.raises(SetupError, match="policy"):
        continuity_policy(tmp_path / "missing")


def test_source_symlink_fifo_and_oversize_are_not_read_as_identity(tmp_path):
    target = tmp_path / "identity"
    target.write_text("Synthetic identity", encoding="utf-8")
    link = tmp_path / "link"
    link.symlink_to(target)
    with pytest.raises(OSError):
        source_text(link, 100)
    fifo = tmp_path / "fifo"
    os.mkfifo(fifo)
    with pytest.raises(SetupError, match="regular"):
        source_text(fifo, 100)
    with pytest.raises(SetupError, match="bound"):
        source_text(target, 1)
