"""Actual immutable compressed window files, no stores, services or native transcript writes."""

import hashlib
import json
import lzma
from dataclasses import replace

import pytest

from anamnema.archive_models import Record, canonical, digest
from anamnema.batch_prepare import PreparedInputs, preparation_policy
from anamnema.processing import Window
from anamnema.setup_config import SetupError


@pytest.fixture
def window():
    records = tuple(
        Record(
            "fixture",
            "claude",
            "session-one",
            "conversation",
            "2026-01-01T10:00:00Z",
            index + 1,
            {"message": f"Synthetic source {index}: \u2028 and whitespace  \n"},
        )
        for index in range(3)
    )
    boundary = Record(
        "fixture",
        "claude",
        "session-one",
        "summary",
        "2026-01-01T10:01:00Z",
        4,
        {"message": "Synthetic summary."},
    )
    return Window(boundary, records)


@pytest.fixture
def inputs(tmp_path):
    return PreparedInputs(tmp_path / "inputs", preparation_policy())


def test_roundtrip_identity_order_private_payload_and_exact_replay(inputs, window):
    path = inputs.save(window)
    identity = path.parent.name
    assert inputs.load("fixture", identity) == window
    first = {
        file.name: (file.read_bytes(), file.stat().st_mtime_ns)
        for file in path.parent.iterdir()
        if file.name != inputs.policy.lock_file
    }
    assert inputs.save(window) == path
    assert first == {
        file.name: (file.read_bytes(), file.stat().st_mtime_ns)
        for file in path.parent.iterdir()
        if file.name != inputs.policy.lock_file
    }
    manifest = inputs.manifest("fixture", identity)
    assert manifest.records == tuple(row.identity for row in (*window.records, window.boundary))
    assert "Synthetic source" not in path.read_text(encoding="utf-8")
    assert all(file.stat().st_mode & 0o777 == 0o600 for file in path.parent.iterdir())
    with pytest.raises(SetupError):
        inputs.load("foreign", identity)


def test_interrupted_manifest_commit_is_unreadable_then_replay_finishes(
    inputs, window, monkeypatch
):
    actual = inputs.files.save

    def stop(path, data, **kwargs):
        if path.name == inputs.policy.manifest_file:
            raise SetupError("fixture interruption")
        return actual(path, data, **kwargs)

    monkeypatch.setattr(inputs.files, "save", stop)
    with pytest.raises(SetupError, match="interruption"):
        inputs.save(window)
    folder = next((inputs.root / digest("fixture")).iterdir())
    assert (folder / inputs.policy.payload_file).is_file()
    with pytest.raises(SetupError, match="manifest"):
        inputs.load("fixture", folder.name)
    monkeypatch.setattr(inputs.files, "save", actual)
    assert inputs.load("fixture", inputs.save(window).parent.name) == window


def test_corrupt_payload_is_not_overwritten_by_replay(inputs, window):
    path = inputs.save(window)
    payload = path.parent / inputs.policy.payload_file
    inputs.files.save(payload, b"broken")
    with pytest.raises(SetupError, match="checksum"):
        inputs.load("fixture", path.parent.name)
    with pytest.raises(SetupError, match="no automatic replacement"):
        inputs.save(window)
    assert payload.read_bytes() == b"broken"


def test_manifest_identity_or_owner_edits_are_refused(inputs, window):
    path = inputs.save(window)
    values = json.loads(path.read_text(encoding="utf-8"))
    values["instance"] = "foreign"
    inputs.files.save(path, json.dumps(values).encode())
    with pytest.raises(SetupError, match="identity/owner"):
        inputs.manifest("fixture", path.parent.name)


def test_open_tail_and_mixed_scope_refuse_before_writing(inputs, window):
    with pytest.raises(SetupError, match="one closed window"):
        inputs.save(Window(window.records[-1], window.records[:-1]))
    other = Record(
        "foreign",
        "claude",
        "session-one",
        "conversation",
        "2026-01-01T10:00:00Z",
        1,
        {"message": "Foreign."},
    )
    with pytest.raises(SetupError, match="one instance"):
        inputs.save(Window(window.boundary, (other,)))
    assert not inputs.root.exists()


def test_bounds_refuse_before_creating_files(inputs, window):
    inputs.policy = inputs.policy.model_copy(update={"max_raw_bytes": 1})
    with pytest.raises(SetupError, match="bounds"):
        inputs.save(window)
    assert not inputs.root.exists()


def test_dictionary_memory_limit_is_enforced_without_changing_input(inputs, window):
    path = inputs.save(window)
    before = path.read_bytes()
    inputs.policy = inputs.policy.model_copy(update={"max_decoder_memory_bytes": 1})
    with pytest.raises(SetupError, match="cannot be recovered"):
        inputs.load("fixture", path.parent.name)
    assert path.read_bytes() == before


def test_self_consistent_manifest_cannot_hide_trailing_compressed_data(inputs, window):
    path = inputs.save(window)
    original = inputs.manifest("fixture", path.parent.name)
    payload = (path.parent / inputs.policy.payload_file).read_bytes() + lzma.compress(
        b"extra stream"
    )
    malicious = original.model_copy(
        update={
            "compressed_bytes": len(payload),
            "payload_sha256": hashlib.sha256(payload).hexdigest(),
        }
    )
    folder = inputs._folder("fixture", malicious.identity, create=True)
    inputs.files.save(folder / inputs.policy.payload_file, payload)
    inputs.files.save(
        folder / inputs.policy.manifest_file, canonical(malicious.model_dump(mode="json"))
    )
    with pytest.raises(SetupError, match="decompression or content bounds"):
        inputs.load("fixture", malicious.identity)


def test_nonfinite_record_refuses_with_no_raw_value_diagnostics_or_files(inputs, window):
    invalid = replace(window.records[0], source={"synthetic-private-field": float("nan")})
    with pytest.raises(SetupError, match="cannot be saved") as failure:
        inputs.save(Window(window.boundary, (invalid,)))
    assert "synthetic-private-field" not in str(failure.value)
    assert not inputs.root.exists()
