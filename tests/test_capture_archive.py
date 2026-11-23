"""Real Mongo read-back and exact journal retirement, including interruption and retry races."""

import io
import json
import subprocess
import sys

import pytest
from pymongo.errors import OperationFailure
from test_capture_queue import job
from test_capture_queue import queue as queue
from test_ingestion import make_archive

from anamnema.capture_archive import (
    FailedCapture,
    MongoCaptureFailures,
    archive_capture_failure,
    capture_archive_policy,
)
from anamnema.cli import main
from anamnema.setup_config import SetupError, load_config


@pytest.fixture
def archive(disposable_mongo):
    return MongoCaptureFailures(make_archive(disposable_mongo), capture_archive_policy())


def failed(queue):
    request = job(queue)
    queue.enqueue(request)
    queue.failed(request, "configuration_changed")
    return queue.inspect().failed[-1]


def test_verified_archive_frees_failure_quota_keeps_exact_pin_and_replays(queue, archive):
    queue.policy = queue.policy.model_copy(update={"max_failed": 1})
    request = failed(queue)
    result = archive_capture_failure(queue, archive, request.id, "Retain this failure for review.")
    assert result["retired"] is True and not queue.inspect().failed
    kept = archive.get(queue.deployment.instance, request.id)
    assert kept is not None and kept.job == request
    assert kept.reason == "Retain this failure for review."
    assert queue.inspect().history[-1].summary["archive"] == kept.checksum
    assert archive_capture_failure(queue, archive, request.id, "Repeat command")["already_archived"]
    assert len(queue.inspect().history) == 1
    failed(queue)  # The terminal quota is available again without erasing the archived request.


def test_stop_after_archive_before_journal_retirement_replays_exactly(queue, archive, monkeypatch):
    request = failed(queue)
    original = queue.retire

    def stop(*args):
        raise SetupError("fixture stop after read-back")

    monkeypatch.setattr(queue, "retire", stop)
    with pytest.raises(SetupError, match="fixture stop"):
        archive_capture_failure(queue, archive, request.id, "Investigate original failure")
    assert queue.inspect().failed == (request,) and archive.get("fixture", request.id) is not None
    monkeypatch.setattr(queue, "retire", original)
    assert archive_capture_failure(queue, archive, request.id, "Investigate original failure")[
        "retired"
    ]
    assert archive.archive.database[archive.policy.collection].count_documents({}) == 1


def test_corrupt_archive_is_not_overwritten_and_exact_local_request_remains(queue, archive):
    request = failed(queue)
    document = FailedCapture(instance="fixture", job=request, reason="Keep exact input")
    archive.keep(document)
    collection = archive.archive.database[archive.policy.collection]
    collection.update_one({"_id": document.identity}, {"$set": {"job.attempts": 999}})
    with pytest.raises(SetupError, match="checksum"):
        archive_capture_failure(queue, archive, request.id, document.reason)
    assert queue.inspect().failed == (request,)
    assert collection.find_one({"_id": document.identity})["job"]["attempts"] == 999


def test_mongo_runs_without_journal_lock_and_concurrent_retry_is_not_removed(
    queue, archive, monkeypatch
):
    request = failed(queue)
    original = archive.keep

    def concurrent_retry(document):
        original(document)
        queue.retry(request.id, "The input has been repaired")

    monkeypatch.setattr(archive, "keep", concurrent_retry)
    result = archive_capture_failure(queue, archive, request.id, "Keep terminal evidence")
    assert result["retired"] is False
    assert queue.inspect().pending[0].id == request.id and queue.inspect().pending[0].error is None


def test_driver_readback_failure_is_visible_and_never_retires_input(queue, archive, monkeypatch):
    request = failed(queue)

    def unavailable(*args):
        raise OperationFailure("fixture-sensitive-driver-detail")

    collection_type = type(archive.archive.database[archive.policy.collection])
    monkeypatch.setattr(collection_type, "find_one", unavailable)
    with pytest.raises(SetupError, match="OperationFailure") as exc:
        archive_capture_failure(queue, archive, request.id, "Keep terminal evidence")
    assert "fixture-sensitive-driver-detail" not in str(exc.value)
    assert queue.inspect().failed == (request,)


def test_retry_and_new_terminal_failure_during_archive_keep_both_failure_versions(
    queue, archive, monkeypatch
):
    first = failed(queue)
    keep = archive.keep

    def retry_and_fail(document):
        keep(document)
        queue.retry(first.id, "Repair and retry original input")
        queue.failed(queue.inspect().pending[0], "configuration_changed")

    monkeypatch.setattr(archive, "keep", retry_and_fail)
    with pytest.raises(SetupError, match="changed while archiving"):
        archive_capture_failure(queue, archive, first.id, "Keep first terminal attempt")
    second = queue.inspect().failed[0]
    assert second != first
    monkeypatch.setattr(archive, "keep", keep)
    assert archive_capture_failure(queue, archive, second.id, "Keep second terminal attempt")[
        "retired"
    ]
    assert archive.get("fixture", first.id, first).job == first
    assert archive.get("fixture", second.id, second).job == second
    assert archive.archive.database[archive.policy.collection].count_documents({}) == 2


def test_actual_apply_cli_uses_only_fixture_keyring_and_disposable_mongo(queue, archive, tmp_path):
    request = failed(queue)
    port = archive.archive.database.client.address[1]
    keyring = tmp_path / "fixture-keyring"
    keyring.write_text(
        f"#!{sys.executable}\nimport sys\nsys.stdout.write('mongodb://127.0.0.1:{port}')\n",
        encoding="utf-8",
    )
    keyring.chmod(0o700)
    setup = load_config().model_dump(mode="json")
    setup["keyring"]["executable"] = str(keyring)
    setup["services"]["archive"]["port"] = port
    queue.deployment.config.write_text(json.dumps(setup), encoding="utf-8")
    archive_path = tmp_path / "archive.json"
    settings = archive.archive.settings.model_copy(
        update={"database": archive.archive.database.name}
    )
    archive_path.write_text(settings.model_dump_json(), encoding="utf-8")
    deployment = queue.deployment.model_copy(update={"archive_config": archive_path})
    deployment_path = tmp_path / "deployment.json"
    deployment_path.write_text(deployment.model_dump_json(), encoding="utf-8")
    result = subprocess.run(  # noqa: S603 - current interpreter and isolated fixture-only config
        [
            sys.executable,
            "-m",
            "anamnema",
            "capture-events",
            "archive-failure",
            "--deployment",
            str(deployment_path),
            "--job",
            request.id,
            "--reason",
            "Investigate exact terminal evidence",
            "--apply",
        ],
        cwd=tmp_path,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=15,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["retired"] is True and not queue.inspect().failed
    assert archive.get("fixture", request.id).job == request


def test_cli_default_preview_opens_no_secrets_or_stores(queue, tmp_path, capsys, monkeypatch):
    deployment = tmp_path / "deployment.json"
    deployment.write_text(queue.deployment.model_dump_json(), encoding="utf-8")
    monkeypatch.setattr("sys.stdin", io.StringIO(""))
    assert (
        main(
            [
                "capture-events",
                "archive-failure",
                "--deployment",
                str(deployment),
                "--job",
                "0" * 32,
                "--reason",
                "Preview only",
            ]
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out) == {
        "mode": "dry-run",
        "terminal_requests": 0,
        "archived": False,
    }
    assert not queue.deployment.state_root.exists()


def test_foreign_corruption_and_document_bound_never_retire_or_repair(queue, archive):
    request = failed(queue)
    document = FailedCapture(instance="fixture", job=request, reason="Keep exact input")
    archive.keep(document)
    collection = archive.archive.database[archive.policy.collection]
    collection.update_one({"_id": document.identity}, {"$set": {"instance": "foreign"}})
    with pytest.raises(SetupError):
        archive_capture_failure(queue, archive, request.id, document.reason)
    assert queue.inspect().failed == (request,)
    assert collection.find_one({"_id": document.identity})["instance"] == "foreign"
    archive.archive.settings = archive.archive.settings.model_copy(
        update={"max_document_bytes": 100}
    )
    with pytest.raises(SetupError, match="document bound"):
        archive_capture_failure(queue, archive, request.id, document.reason)
    assert queue.inspect().failed == (request,)


@pytest.mark.parametrize("identity,reason", [("invalid", "Valid reason"), ("0" * 32, " ")])
def test_invalid_cli_archival_input_fails_before_secret_or_network(
    queue, tmp_path, monkeypatch, capsys, identity, reason
):
    def forbidden(*args, **kwargs):
        pytest.fail("invalid request attempted a secret or network operation")

    monkeypatch.setattr("anamnema.capture_event_cli.keyring_uri", forbidden)
    monkeypatch.setattr("anamnema.capture_event_cli.connect", forbidden)
    path = tmp_path / "deployment.json"
    path.write_text(queue.deployment.model_dump_json(), encoding="utf-8")
    assert (
        main(
            [
                "capture-events",
                "archive-failure",
                "--deployment",
                str(path),
                "--job",
                identity,
                "--reason",
                reason,
                "--apply",
            ]
        )
        == 2
    )
    assert "error" in json.loads(capsys.readouterr().err)
    assert not queue.deployment.state_root.exists()
