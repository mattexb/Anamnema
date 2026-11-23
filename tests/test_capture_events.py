"""Native request translation and actual queued CLI capture over isolated stores."""

import io
import json
import threading
import time
from pathlib import Path

import pytest
from search_fixtures import SearchHTTP
from test_marked_capture import record
from test_memory_workflow import _cli_options
from test_memory_workflow import memory_workflow as memory_workflow

from anamnema.archive_models import digest
from anamnema.capture_event_cli import _drain
from anamnema.capture_events import expectations_present, prepare_job, request_from_event
from anamnema.capture_queue import CaptureQueue
from anamnema.capture_queue_config import CaptureDeployment, capture_queue_policy
from anamnema.capture_worker import CapturePending, _ready, drain_capture, execute_job
from anamnema.cli import main
from anamnema.setup_config import SetupError, load_config


@pytest.fixture
def event_queue(tmp_path):
    config = tmp_path / "setup.json"
    config.write_text(load_config().model_dump_json(), encoding="utf-8")
    transcripts = tmp_path / "transcripts"
    transcripts.mkdir()
    (transcripts / "session.jsonl").write_text("", encoding="utf-8")
    deployment = CaptureDeployment(
        instance="fixture",
        state_root=tmp_path / "capture-state",
        cwd=tmp_path,
        transcript_roots=(transcripts,),
        config=config,
    )
    policy = capture_queue_policy().model_copy(update={"wait_seconds": 0.2, "poll_seconds": 0.01})
    return CaptureQueue(deployment, policy)


def payload(queue, message="fact: A queued note."):
    return {
        "hook_event_name": "Stop",
        "session_id": "session-one",
        "transcript_path": str(queue.deployment.transcript_roots[0] / "session.jsonl"),
        "cwd": str(queue.deployment.cwd),
        "last_assistant_message": message,
    }


def write_record(path, text, *, instance="fixture"):
    path.write_text(json.dumps(record(text, instance=instance).source) + "\n", encoding="utf-8")


@pytest.mark.parametrize("event", ["stop", "precompact"])
def test_event_selection_remains_on_original_range_after_later_turn_or_compaction(
    event_queue, event
):
    queue = event_queue
    path = queue.deployment.transcript_roots[0] / "session.jsonl"
    old = record("fact: The event's own note.", uuid="old-record")
    path.write_text(json.dumps(old.source) + "\n", encoding="utf-8")
    data = {
        **payload(queue, "fact: The event's own note."),
        "hook_event_name": queue.policy.native_events[event],
    }
    job = request_from_event(data, event, queue.deployment, queue.policy)
    assert job is not None
    if event == "stop":
        boundary = record("Next owner prompt.", type="user", uuid="new-prompt")
    else:
        boundary = record("Compaction summary.", type="user", isCompactSummary=True, uuid="summary")
    later = record("fact: The event's own note.", uuid="later-identical-words")
    path.write_text(
        "\n".join(json.dumps(row.source) for row in (old, boundary, later)) + "\n", encoding="utf-8"
    )
    selected = prepare_job(job, queue.deployment)
    assert [mark.source_id for mark in selected.marks] == [old.identity]


@pytest.mark.parametrize("fault", ["event", "cwd", "relative", "outside", "message", "session"])
def test_native_capture_rejects_invalid_coordinates_without_queue_or_stores(event_queue, fault):
    queue = event_queue
    data = payload(queue)
    key, value = {
        "event": ("hook_event_name", "SessionStart"),
        "cwd": ("cwd", "/wrong"),
        "relative": ("transcript_path", "relative.jsonl"),
        "outside": ("transcript_path", str(queue.deployment.cwd / "outside.jsonl")),
        "message": ("last_assistant_message", 1),
        "session": ("session_id", ""),
    }[fault]
    data[key] = value
    with pytest.raises(SetupError):
        request_from_event(data, "stop", queue.deployment, queue.policy)
    assert not queue.deployment.state_root.exists()


def test_empty_stop_does_not_create_state_and_expected_hashes_keep_multiplicity(event_queue):
    queue = event_queue
    assert (
        request_from_event(payload(queue, "ordinary text"), "stop", queue.deployment, queue.policy)
        is None
    )
    text = "fact: Repeated line.\nfact: Repeated line."
    job = request_from_event(payload(queue, text), "stop", queue.deployment, queue.policy)
    assert job is not None and len(job.expected) == 2 and job.expected[0] == job.expected[1]
    write_record(job.transcript, "fact: Repeated line.")
    assert not expectations_present(job, prepare_job(job, queue.deployment))
    write_record(job.transcript, text)
    assert expectations_present(job, prepare_job(job, queue.deployment))
    assert not queue.deployment.state_root.exists()


def test_waiting_for_native_flush_uses_real_records_then_configuration_change_refuses(event_queue):
    queue = event_queue
    job = request_from_event(payload(queue), "stop", queue.deployment, queue.policy)
    assert job is not None
    write_record(job.transcript, "No marker has been flushed yet.")

    def append_final():
        time.sleep(0.03)
        write_record(job.transcript, "fact: A queued note.")

    producer = threading.Thread(target=append_final)
    producer.start()
    try:
        prepared = _ready(job, queue.deployment, queue.policy, time.monotonic() + 1)
        assert len(prepared.marks) == 1
    finally:
        producer.join(timeout=2)
    config = json.loads(queue.deployment.config.read_text(encoding="utf-8"))
    config["services"]["notes"]["port"] += 1
    queue.deployment.config.write_text(json.dumps(config), encoding="utf-8")
    with pytest.raises(CapturePending, match="configuration_changed"):
        _ready(job, queue.deployment, queue.policy, time.monotonic() + 1)


def test_no_flush_is_pending_and_a_bad_job_does_not_poison_later_jobs(event_queue, monkeypatch):
    queue = event_queue
    first = request_from_event(payload(queue), "stop", queue.deployment, queue.policy)
    second = request_from_event(
        payload(queue, "fact: Another note."), "stop", queue.deployment, queue.policy
    )
    assert first is not None and second is not None
    write_record(first.transcript, "Not flushed.")
    with pytest.raises(CapturePending, match="unflushed"):
        _ready(first, queue.deployment, queue.policy, time.monotonic() + 1)
    queue.enqueue(first)
    queue.enqueue(second)

    def execute(job, *_):
        if job.id == first.id:
            raise CapturePending("unflushed")
        return {"inserted": 0, "already_present": 1}

    monkeypatch.setattr("anamnema.capture_worker.execute_job", execute)
    result = drain_capture(queue)
    assert result["failed"] == result["completed"] == result["pending"] == 1
    assert queue.inspect().pending[0].error == "unflushed"


def test_changed_child_source_selection_cannot_complete_a_request(event_queue, monkeypatch):
    queue = event_queue
    job = request_from_event(payload(queue), "stop", queue.deployment, queue.policy)
    assert job is not None
    write_record(job.transcript, "fact: A queued note.")
    result = {
        "mode": "apply",
        "marks": 1,
        "sources_verified": True,
        "source_marks_digest": digest(["not the prepared input"]),
        "notes_model_calls": 0,
    }
    monkeypatch.setattr("anamnema.capture_worker.run_worker", lambda *_: json.dumps(result))
    with pytest.raises(CapturePending, match="capture_failed"):
        execute_job(job, queue.deployment, queue.policy, time.monotonic() + 1)


def test_watch_accepts_new_work_and_attempts_a_failed_request_only_once(event_queue, monkeypatch):
    import argparse

    queue = event_queue
    queue.policy = queue.policy.model_copy(update={"max_worker_seconds": 0.12})
    first = request_from_event(payload(queue), "stop", queue.deployment, queue.policy)
    second = request_from_event(
        payload(queue, "fact: Added while worker runs."), "stop", queue.deployment, queue.policy
    )
    assert first is not None and second is not None
    queue.enqueue(first)

    def execute(job, *_):
        if job.id == first.id:
            raise CapturePending("capture_failed")
        return {"inserted": 1, "notes_model_calls": 0}

    def arrival():
        time.sleep(0.03)
        queue.enqueue(second)

    monkeypatch.setattr("anamnema.capture_worker.execute_job", execute)
    sender = threading.Thread(target=arrival)
    sender.start()
    try:
        result = _drain(argparse.Namespace(apply=True, watch=True), queue)
    finally:
        sender.join(timeout=2)
    assert result["completed"] == result["failed"] == result["pending"] == 1
    assert queue.inspect().pending[0].attempts == 1 and result["worker_budget_exhausted"]


def test_watch_retries_due_unflushed_job_in_same_lifetime(event_queue, monkeypatch):
    import argparse

    queue = event_queue
    queue.policy = queue.policy.model_copy(
        update={"max_worker_seconds": 0.15, "retry_seconds": 0.01}
    )
    request = request_from_event(payload(queue), "stop", queue.deployment, queue.policy)
    assert request is not None
    queue.enqueue(request)
    attempts = []

    def execute(job, *_):
        attempts.append(job.id)
        if len(attempts) == 1:
            raise CapturePending("unflushed")
        return {"inserted": 1, "notes_model_calls": 0}

    monkeypatch.setattr("anamnema.capture_worker.execute_job", execute)
    result = _drain(argparse.Namespace(apply=True, watch=True), queue)
    assert result["completed"] == result["failed"] == 1 and result["pending"] == 0
    assert attempts == [request.id, request.id] and not queue.inspect().failed


def test_watch_waits_out_overlapping_manual_worker_instead_of_exiting(event_queue, monkeypatch):
    import argparse

    queue = event_queue
    queue.policy = queue.policy.model_copy(update={"max_worker_seconds": 0.15})
    request = request_from_event(payload(queue), "stop", queue.deployment, queue.policy)
    assert request is not None
    queue.enqueue(request)
    monkeypatch.setattr("anamnema.capture_worker.execute_job", lambda *_: {"inserted": 1})
    results = []
    with queue.worker():
        watcher = threading.Thread(
            target=lambda: results.append(_drain(argparse.Namespace(apply=True, watch=True), queue))
        )
        watcher.start()
        time.sleep(0.03)
        assert watcher.is_alive()
    watcher.join(timeout=2)
    assert not watcher.is_alive() and results[0]["completed"] == 1
    assert results[0]["busy_encounters"] >= 1


def test_hook_cli_dry_run_and_failure_are_nonblocking_without_required_read_state(
    event_queue, tmp_path, monkeypatch, capsys
):
    queue = event_queue
    deployment = tmp_path / "deployment.json"
    deployment.write_text(queue.deployment.model_dump_json(), encoding="utf-8")
    args = ["capture-events", "hook", "--deployment", str(deployment), "--event", "stop"]
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload(queue))))
    assert main(args) == 0
    assert json.loads(capsys.readouterr().out)["queued"] is False
    assert not queue.deployment.state_root.exists()
    invalid = {**payload(queue), "cwd": "/not-the-deployment"}
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(invalid)))
    assert main([*args, "--apply"]) == 0
    output = json.loads(capsys.readouterr().out)
    assert "systemMessage" in output and "decision" not in output
    assert not queue.deployment.state_root.exists()
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload(queue))))
    assert main([*args, "--apply"]) == 0
    assert json.loads(capsys.readouterr().out) == {}
    assert len(queue.inspect().pending) == 1
    bad_session = {**payload(queue), "session_id": "session\nPRIVATE_SYNTHETIC_VALUE"}
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(bad_session)))
    assert main([*args, "--apply"]) == 0
    failure = capsys.readouterr()
    assert "PRIVATE_SYNTHETIC_VALUE" not in failure.out + failure.err
    assert "decision" not in json.loads(failure.out)


@pytest.mark.parametrize("event", ["stop", "precompact"])
def test_actual_queued_capture_child_cli_replays_after_completion_record_interruption(
    memory_workflow, tmp_path, monkeypatch, event
):
    instance, _, engine, server, _, _, _, archive = memory_workflow
    with SearchHTTP() as notes:
        options = _cli_options(
            instance, engine, server, archive, tmp_path, notes_port=notes.server.server_port
        )
        configured = dict(zip(options[::2], options[1::2], strict=True))
        deployment = CaptureDeployment(
            instance=instance,
            cwd=tmp_path,
            state_root=tmp_path / "queue-state",
            transcript_roots=(tmp_path,),
            config=Path(configured["--config"]),
            archive_config=Path(configured["--archive-config"]),
            memory_config=Path(configured["--memory-config"]),
            search_config=Path(configured["--search-config"]),
        )
        queue = CaptureQueue(deployment, capture_queue_policy())
        write_record(tmp_path / "session.jsonl", "fact: A queued note.", instance=instance)
        data = {**payload(queue), "hook_event_name": queue.policy.native_events[event]}
        job = request_from_event(data, event, deployment, queue.policy)
        assert job is not None
        write_record(job.transcript, "fact: A queued note.", instance=instance)
        queue.enqueue(job)
        original = record("fact: A queued note.", instance=instance)
        boundary = record(
            "Next turn.",
            instance=instance,
            type="user",
            uuid="later-prompt",
            **({"isCompactSummary": True} if event == "precompact" else {}),
        )
        later = record("fact: A queued note.", instance=instance, uuid="later-source")
        job.transcript.write_text(
            "\n".join(json.dumps(row.source) for row in (original, boundary, later)) + "\n",
            encoding="utf-8",
        )
        complete = queue.complete

        def interrupted(*_):
            raise OSError("fixture lost completion record after graph commit")

        monkeypatch.setattr(queue, "complete", interrupted)
        with pytest.raises(OSError, match="after graph commit"):
            drain_capture(queue)
        assert len(queue.inspect().pending) == 1
        calls = len(server.calls)
        assert calls > 0
        assert not engine.store.known(instance, digest(["marked", instance, later.identity, 0]))
        monkeypatch.setattr(queue, "complete", complete)
        assert drain_capture(queue)["completed"] == 1
        assert not queue.inspect().pending and len(server.calls) == calls
        assert queue.inspect().history[-1].summary["already_present"] == 1
        assert not notes.calls
