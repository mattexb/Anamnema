"""Actual CLI/HTTP/Mongo workflow with synthetic isolated services and no live keyring."""

import hashlib
import json
import os
import subprocess
import sys
import time
from types import SimpleNamespace

import pytest
from extraction_fixtures import completion
from pymongo.errors import PyMongoError

from anamnema.archive_models import canonical
from anamnema.binding import bindings
from anamnema.chunks import chunks
from anamnema.cli import main
from anamnema.extraction_config import extraction_config
from anamnema.extraction_store import ExtractionStore
from anamnema.extractor import Extractor
from anamnema.ingest_config import archive_settings
from anamnema.llama_provider import LlamaProvider
from anamnema.notes_policy import notes_policy
from anamnema.processing import prepare
from anamnema.setup_config import Endpoint, SetupError, load_config
from anamnema.speech_policy import speech_policy
from anamnema.substrates.claude import ClaudeAdapter
from anamnema.substrates.claude_speech import ClaudeSpeechReader


@pytest.fixture
def workflow(tmp_path, disposable_mongo, model_service):
    uri, client = disposable_mongo
    database = "extract_" + tmp_path.name.replace("-", "_")
    database = database[:60]
    archive = archive_settings().model_dump(mode="json")
    archive["database"] = database
    archive_path = tmp_path / "archive.json"
    archive_path.write_text(json.dumps(archive), encoding="utf-8")
    keyring = tmp_path / "fixture-keyring"
    keyring.write_text(
        f"#!{sys.executable}\nimport sys\nsys.stdout.write({uri!r})\n", encoding="utf-8"
    )
    keyring.chmod(0o700)
    setup = load_config().model_dump(mode="json")
    setup["services"]["archive"]["port"] = client.address[1]
    setup["services"]["notes"]["port"] = model_service.port
    setup["keyring"]["executable"] = str(keyring)
    setup_path = tmp_path / "setup.json"
    setup_path.write_text(json.dumps(setup), encoding="utf-8")
    notes = notes_policy().model_dump(mode="json")
    notes["chunk_characters"] = 100
    notes_path = tmp_path / "notes.json"
    notes_path.write_text(json.dumps(notes), encoding="utf-8")
    execution = extraction_config().model_dump(mode="json")
    execution["pause_every"] = 0
    execution_path = tmp_path / "execution.json"
    execution_path.write_text(json.dumps(execution), encoding="utf-8")
    source = tmp_path / "session.jsonl"
    source.write_text(
        "\n".join(
            json.dumps(row)
            for row in [
                {
                    "type": "user",
                    "timestamp": "2026-01-01",
                    "uuid": "same",
                    "message": "Keep original sources for every note we write in this project.",
                },
                {
                    "type": "assistant",
                    "timestamp": "2026-01-02",
                    "uuid": "same",
                    "message": "The original conversation remains available to check each note.",
                },
            ]
        ),
        encoding="utf-8",
    )
    shared = [
        "--file",
        str(source),
        "--instance",
        "fixture",
        "--session",
        "s",
        "--closed",
        "--config",
        str(setup_path),
        "--archive-config",
        str(archive_path),
    ]
    command = [sys.executable, "-I", "-m", "anamnema"]

    def arguments(import_source=False):
        args = ["import" if import_source else "process", *shared]
        if not import_source:
            args += [
                "--notes-config",
                str(notes_path),
                "--extraction-config",
                str(execution_path),
                "--model-file",
                str(model_service.model_file),
            ]
        return args

    def run(*flags, import_source=False):
        return subprocess.run(  # noqa: S603 - installed CLI, own fixtures, no shell
            [*command, *arguments(import_source), *flags],
            cwd=tmp_path,
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
            env={**os.environ, "HOME": str(tmp_path)},
        )

    return {
        "run": run,
        "args": arguments(),
        "db": client[database],
        "model": model_service,
        "source": source,
        "notes": notes,
        "notes_path": notes_path,
        "execution": execution,
        "execution_path": execution_path,
        "setup": setup,
        "setup_path": setup_path,
        "keyring": keyring,
    }


def imported(workflow):
    result = workflow["run"]("--apply", import_source=True)
    assert result.returncode == 0, result.stderr


def test_actual_cli_import_extract_readback_replay_and_changed_options(workflow):
    imported(workflow)
    original = workflow["source"].read_bytes()
    first = workflow["run"]("--apply")
    assert first.returncode == 0, first.stderr
    report = json.loads(first.stdout)
    assert report["model_calls"] == report["created_chunks"] == 2
    assert report["archive_verified"] is True
    assert report["notes"] == 2 and report["flagged_notes"] == 0
    stored = list(workflow["db"].notes_chunks.find())
    assert len(stored) == 2
    assert all(document["notes"][0]["source_ids"] for document in stored)
    assert workflow["db"].extraction_attempts.find_one()["state"] == "success"
    again = workflow["run"]("--apply")
    assert again.returncode == 0, again.stderr
    assert json.loads(again.stdout)["reused_chunks"] == 2
    assert json.loads(again.stdout)["model_calls"] == 0
    assert len(workflow["model"].calls) == 2
    assert list(workflow["db"].notes_chunks.find()) == stored
    workflow["notes"]["model_options"]["temperature"] = 0.2
    workflow["notes_path"].write_text(json.dumps(workflow["notes"]), encoding="utf-8")
    changed = workflow["run"]("--apply")
    assert changed.returncode == 0, changed.stderr
    assert json.loads(changed.stdout)["created_chunks"] == 2
    assert json.loads(changed.stdout)["run"] != report["run"]
    assert len(workflow["model"].calls) == 4
    assert all(body["temperature"] == 0.2 for body in workflow["model"].calls[-2:])
    assert workflow["source"].read_bytes() == original
    assert "The project keeps" not in first.stdout


def test_no_archived_source_means_no_model_or_saved_notes(workflow):
    result = workflow["run"]("--apply")
    assert result.returncode == 2
    assert "read-back is incomplete" in result.stderr
    assert workflow["model"].requests == []
    assert workflow["db"].notes_chunks.count_documents({}) == 0


def test_dry_run_does_not_access_unusable_keyring_or_model(workflow):
    workflow["keyring"].unlink()
    result = workflow["run"]()
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["mode"] == "dry-run"
    assert workflow["model"].requests == []
    assert workflow["db"].notes_chunks.count_documents({}) == 0


def test_partial_failure_preserves_verified_chunks_and_resumes_only_missing(workflow):
    imported(workflow)
    model = workflow["model"]
    good = completion(
        json.dumps(
            {
                "concepts": [
                    {
                        "text": "The project keeps original source records.",
                        "kind": "fact",
                        "rows": [0],
                        "ruled_by": 0,
                    }
                ]
            }
        )
    )
    model.chat_replies.extend(
        [good, (500, b"never expose this error body"), (500, b"private body")]
    )
    failed = workflow["run"]("--apply")
    assert failed.returncode == 2
    assert "never expose" not in failed.stderr and "private body" not in failed.stderr
    assert "HTTP 500" in failed.stderr
    chunks = list(workflow["db"].notes_chunks.find())
    assert len(chunks) == 1
    assert workflow["db"].extraction_attempts.find_one()["state"] == "failed"
    resumed = workflow["run"]("--apply")
    assert resumed.returncode == 0, resumed.stderr
    report = json.loads(resumed.stdout)
    assert report["reused_chunks"] == report["created_chunks"] == report["model_calls"] == 1
    assert workflow["db"].notes_chunks.find_one({"_id": chunks[0]["_id"]}) == chunks[0]
    assert workflow["db"].notes_chunks.count_documents({}) == 2


def test_malformed_reply_is_retried_at_window_end(workflow):
    imported(workflow)
    workflow["model"].chat_replies.append(completion('{"concepts":[]}'))
    result = workflow["run"]("--apply")
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["model_calls"] == 3
    assert workflow["db"].notes_chunks.count_documents({}) == 2


def test_context_overflow_never_reaches_chat_or_saves_empty_success(workflow):
    imported(workflow)
    workflow["model"].token_count = 8192
    result = workflow["run"]("--apply")
    assert result.returncode == 2
    assert workflow["model"].calls == []
    assert workflow["db"].notes_chunks.count_documents({}) == 0
    assert workflow["db"].extraction_attempts.find_one()["state"] == "failed"


def test_corrupt_saved_note_is_visible_not_silently_reused_or_repaired(workflow):
    imported(workflow)
    assert workflow["run"]("--apply").returncode == 0
    collection = workflow["db"].notes_chunks
    row = collection.find_one()
    collection.update_one({"_id": row["_id"]}, {"$set": {"notes.0.text": "corrupted"}})
    before = list(collection.find())
    calls = len(workflow["model"].calls)
    result = workflow["run"]("--apply")
    assert result.returncode == 2
    assert "source references differ" in result.stderr
    assert list(collection.find()) == before
    assert len(workflow["model"].calls) == calls


def test_equivalent_options_and_schedule_changes_reuse_the_same_notes(workflow):
    imported(workflow)
    assert workflow["run"]("--apply").returncode == 0
    workflow["notes"]["model_options"] = dict(
        reversed(list(workflow["notes"]["model_options"].items()))
    )
    workflow["notes_path"].write_text(json.dumps(workflow["notes"]), encoding="utf-8")
    workflow["execution"].update(timeout_seconds=12.0, pause_every=1, pause_seconds=0.0)
    workflow["execution_path"].write_text(json.dumps(workflow["execution"]), encoding="utf-8")
    result = workflow["run"]("--apply")
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["model_calls"] == 0
    assert len(workflow["model"].calls) == 2


def test_changed_model_file_hash_gets_a_new_run(workflow):
    imported(workflow)
    first = workflow["run"]("--apply")
    assert first.returncode == 0, first.stderr
    workflow["model"].model_file.write_bytes(b"GGUF another synthetic fixture model")
    second = workflow["run"]("--apply")
    assert second.returncode == 0, second.stderr
    assert json.loads(first.stdout)["run"] != json.loads(second.stdout)["run"]
    assert json.loads(second.stdout)["created_chunks"] == 2


def test_output_secrets_are_sanitized_before_storage(workflow):
    imported(workflow)
    content = {
        "concepts": [
            {
                "text": "The operator gave password=example-value for a temporary test.",
                "kind": "fact",
                "rows": [0],
                "ruled_by": 0,
            }
        ]
    }
    workflow["model"].chat_replies.append(completion(json.dumps(content)))
    result = workflow["run"]("--apply")
    assert result.returncode == 0, result.stderr
    documents = list(workflow["db"].notes_chunks.find())
    assert "example-value" not in canonical(documents[0]).decode()
    assert any("<redacted-secret>" in document["answer"] for document in documents)
    assert (
        hashlib.sha256(workflow["source"].read_bytes()).hexdigest()
        == json.loads(result.stdout)["source_sha256"]
    )


def components(workflow):
    notes = notes_policy(workflow["notes_path"])
    speech = speech_policy()
    config = extraction_config(workflow["execution_path"])
    adapter = ClaudeAdapter(archive_settings())
    snapshot = adapter.read(workflow["source"], "fixture", "s", closed=True)
    reader = ClaudeSpeechReader(speech)
    prepared = []
    for window in prepare(snapshot).windows:
        rows = reader.read(window).rows
        prepared.append((window, chunks(rows, bindings(rows, notes), notes)))
    provider = LlamaProvider(
        Endpoint(host="127.0.0.1", port=workflow["model"].port),
        config,
        workflow["model"].model_file,
    )
    store = ExtractionStore(workflow["db"], config, notes, speech)
    return provider, store, prepared


def test_keyboard_interruption_records_state_and_preserves_first_chunk(workflow, monkeypatch):
    imported(workflow)
    provider, store, prepared = components(workflow)
    original = provider.generate
    calls = 0

    def interrupt(body):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise KeyboardInterrupt
        return original(body)

    monkeypatch.setattr(provider, "generate", interrupt)
    extractor = Extractor(provider, store)
    with pytest.raises(KeyboardInterrupt):
        extractor.run(prepared)
    attempt = workflow["db"].extraction_attempts.find_one({"_id": extractor.attempt_id})
    assert attempt["state"] == "interrupted"
    assert attempt["error_category"] == "KeyboardInterrupt"
    assert attempt["created_chunks"] == 1
    first = workflow["db"].notes_chunks.find_one()
    monkeypatch.setattr(provider, "generate", original)
    resumed = Extractor(provider, store).run(prepared)
    assert resumed["reused_chunks"] == resumed["created_chunks"] == resumed["model_calls"] == 1
    assert workflow["db"].notes_chunks.find_one({"_id": first["_id"]}) == first


def test_slow_success_is_saved_then_run_stops_and_can_resume(workflow):
    imported(workflow)
    workflow["execution"]["slow_chunk_seconds"] = 0.001
    workflow["execution_path"].write_text(json.dumps(workflow["execution"]), encoding="utf-8")
    workflow["model"].chat_delay = 0.02
    result = workflow["run"]("--apply")
    assert result.returncode == 2
    assert "slow-call limit" in result.stderr
    assert len(workflow["model"].calls) == 1
    attempt = workflow["db"].extraction_attempts.find_one()
    assert attempt["created_chunks"] == attempt["notes"] == 1
    assert attempt["state"] == "failed" and attempt["error_category"] == "SlowChunk"
    first = workflow["db"].notes_chunks.find_one()
    workflow["execution"]["slow_chunk_seconds"] = 180.0
    workflow["execution_path"].write_text(json.dumps(workflow["execution"]), encoding="utf-8")
    resumed = workflow["run"]("--apply")
    assert resumed.returncode == 0, resumed.stderr
    assert json.loads(resumed.stdout)["reused_chunks"] == 1
    assert workflow["db"].notes_chunks.find_one({"_id": first["_id"]}) == first


def test_persistent_bad_output_cannot_be_a_successful_empty_extraction(workflow):
    imported(workflow)
    workflow["model"].overrides["/v1/chat/completions"] = completion('{"concepts":[]}')
    result = workflow["run"]("--apply")
    assert result.returncode == 2
    assert len(workflow["model"].calls) == 4
    assert workflow["db"].notes_chunks.count_documents({}) == 0
    attempt = workflow["db"].extraction_attempts.find_one()
    assert attempt["state"] == "failed" and attempt["failed_chunks"] == 2
    assert len(attempt["unresolved_chunks"]) == 2
    assert len(attempt["model_failures"]) == 4
    assert all("notes schema" in failed["reason"] for failed in attempt["unresolved_chunks"])


def test_empty_eligible_window_never_contacts_model(workflow):
    row = {"type": "user", "timestamp": "2026-01-01", "isMeta": True, "message": "host context"}
    workflow["source"].write_text(json.dumps(row), encoding="utf-8")
    imported(workflow)
    result = workflow["run"]("--apply")
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["model_calls"] == 0
    assert json.loads(result.stdout)["notes_generated"] is False
    assert workflow["model"].requests == []


@pytest.mark.parametrize("backend", ["api", "cuda"])
def test_unimplemented_backend_fails_before_keyring_or_model_access(workflow, backend):
    workflow["setup"]["backend"] = backend
    workflow["setup_path"].write_text(json.dumps(workflow["setup"]), encoding="utf-8")
    workflow["keyring"].unlink()
    result = workflow["run"]("--apply")
    assert result.returncode == 2
    assert "local CPU/Vulkan" in result.stderr
    assert workflow["model"].requests == []


def test_collection_collision_cannot_replace_transcript_rows(workflow):
    workflow["execution"]["chunks_collection"] = "conversations"
    workflow["execution_path"].write_text(json.dumps(workflow["execution"]), encoding="utf-8")
    workflow["keyring"].unlink()
    result = workflow["run"]("--apply")
    assert result.returncode == 2
    assert "overlap archive" in result.stderr
    assert workflow["db"].conversations.count_documents({}) == 0
    assert workflow["model"].requests == []


def test_model_path_mismatch_is_not_a_claim_of_correct_model_identity(workflow, tmp_path):
    imported(workflow)
    other = tmp_path / "other.gguf"
    other.write_bytes(b"not the explicit fixture model")
    workflow["model"].overrides["/props"] = (
        200,
        {
            "model_path": str(other),
            "model_alias": "other",
            "build_info": "fixture",
            "default_generation_settings": {"n_ctx": 8192},
        },
    )
    result = workflow["run"]("--apply")
    assert result.returncode == 2
    assert "path differs" in result.stderr
    assert workflow["model"].calls == []
    assert workflow["db"].notes_chunks.count_documents({}) == 0


def test_model_change_during_generation_cannot_save_under_the_old_run(workflow):
    imported(workflow)
    model = workflow["model"]
    model.after_chat = lambda: setattr(model, "context", model.context + 1)
    result = workflow["run"]("--apply")
    assert result.returncode == 2
    assert "identity changed" in result.stderr
    assert len(model.calls) == 1
    assert workflow["db"].notes_chunks.count_documents({}) == 0
    assert workflow["db"].extraction_attempts.find_one()["state"] == "failed"


def test_storage_failure_is_not_retried_as_a_model_failure(workflow, monkeypatch):
    imported(workflow)
    provider, store, prepared = components(workflow)
    collection_type = type(workflow["db"].notes_chunks)
    original = collection_type.update_one

    def fail_notes(collection, *args, **kwargs):
        if collection.name == store.config.chunks_collection:
            raise PyMongoError("synthetic private driver details")
        return original(collection, *args, **kwargs)

    monkeypatch.setattr(collection_type, "update_one", fail_notes)
    extractor = Extractor(provider, store)
    with pytest.raises(SetupError, match="saving notes failed") as caught:
        extractor.run(prepared)
    assert "private driver" not in str(caught.value)
    assert len(workflow["model"].calls) == 1
    assert workflow["db"].notes_chunks.count_documents({}) == 0
    assert workflow["db"].extraction_attempts.find_one()["state"] == "failed"


def test_failed_readback_does_not_count_success_and_later_resume_verifies_it(workflow, monkeypatch):
    imported(workflow)
    provider, store, prepared = components(workflow)
    collection_type = type(workflow["db"].notes_chunks)
    original = collection_type.find_one

    def missing(collection, *args, **kwargs):
        if collection.name == store.config.chunks_collection:
            return None
        return original(collection, *args, **kwargs)

    monkeypatch.setattr(collection_type, "find_one", missing)
    extractor = Extractor(provider, store)
    with pytest.raises(SetupError, match="read-back is missing"):
        extractor.run(prepared)
    attempt = workflow["db"].extraction_attempts.find_one({"_id": extractor.attempt_id})
    assert attempt["state"] == "failed" and attempt["created_chunks"] == 0
    assert workflow["db"].notes_chunks.count_documents({}) == 1
    monkeypatch.setattr(collection_type, "find_one", original)
    result = Extractor(provider, store).run(prepared)
    assert result["reused_chunks"] == result["created_chunks"] == 1


def test_worker_pacing_applies_only_to_generation_not_cached_replay(workflow, monkeypatch):
    imported(workflow)
    provider, store, prepared = components(workflow)
    store.config = store.config.model_copy(update={"pause_every": 1, "pause_seconds": 0.001})
    sleeps = []
    monkeypatch.setattr(
        "anamnema.extractor.time", SimpleNamespace(sleep=sleeps.append, monotonic=time.monotonic)
    )
    report = Extractor(provider, store).run(prepared)
    assert report["model_calls"] == 2
    assert sleeps.count(0.001) == 1
    sleeps.clear()
    assert Extractor(provider, store).run(prepared)["model_calls"] == 0
    assert sleeps == []


def test_optional_model_auth_uses_fixture_keyring_without_persisting_token(workflow):
    imported(workflow)
    token = "fixture-model-credential"  # noqa: S105 - synthetic fixture, not a real credential
    uri = workflow["db"].client.address
    script = (
        f"#!{sys.executable}\nimport sys\n"
        f"sys.stdout.write({token!r} if 'models/notes' in sys.argv else 'mongodb://{uri[0]}:{uri[1]}')\n"
    )
    workflow["keyring"].write_text(script, encoding="utf-8")
    workflow["execution"].update(model_keyring_entry="models/notes", model_keyring_field="token")
    workflow["execution_path"].write_text(json.dumps(workflow["execution"]), encoding="utf-8")
    result = workflow["run"]("--apply")
    assert result.returncode == 0, result.stderr
    assert all(
        header.get("Authorization") == "Bearer " + token for header in workflow["model"].headers
    )
    assert token not in result.stdout + result.stderr
    assert token not in json.dumps(list(workflow["db"].notes_chunks.find()))
    assert token not in json.dumps(list(workflow["db"].extraction_attempts.find()))


def test_successful_attempt_retains_reproducible_nonsecret_provenance(workflow):
    imported(workflow)
    result = workflow["run"]("--apply")
    assert result.returncode == 0, result.stderr
    attempt = workflow["db"].extraction_attempts.find_one()
    assert (
        attempt["model"]["sha256"]
        == hashlib.sha256(workflow["model"].model_file.read_bytes()).hexdigest()
    )
    assert attempt["notes_policy"]["model_options"] == workflow["notes"]["model_options"]
    assert attempt["request_shape"]["response_format"]["json_schema"]["schema"]
    assert attempt["prompt_template"].endswith("SLICE:\n")
    assert all(
        row["input_text"] and row["input_sha256"] for row in workflow["db"].notes_chunks.find()
    )


def test_main_dispatch_runs_the_same_archive_verified_apply_path(workflow, capsys):
    imported(workflow)
    assert main([*workflow["args"], "--apply"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["archive_verified"] is True
    assert report["model_calls"] == report["created_chunks"] == 2
    assert workflow["db"].notes_chunks.count_documents({}) == 2
