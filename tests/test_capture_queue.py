"""Atomic private job state and worker serialization never become read obligations."""

import json
import threading
import time

import pytest

from anamnema.archive_models import digest
from anamnema.capture_queue import CaptureQueue, new_capture_job
from anamnema.capture_queue_config import CaptureDeployment, capture_queue_policy
from anamnema.capture_selection import CaptureSelection
from anamnema.setup_config import SetupError


@pytest.fixture
def queue(tmp_path):
    deployment = CaptureDeployment(
        instance="fixture",
        state_root=tmp_path / "capture-state",
        cwd=tmp_path,
        transcript_roots=(tmp_path / "transcripts",),
        config=tmp_path / "setup.json",
    )
    return CaptureQueue(deployment, capture_queue_policy())


def job(queue):
    path = queue.deployment.transcript_roots[0] / "session.jsonl"
    selection = CaptureSelection(
        instance=queue.deployment.instance,
        session="session",
        transcript=path,
        mode="turn",
        start=None,
        end=None,
    )
    return new_capture_job(
        "session",
        path,
        "stop",
        (digest("synthetic marker"),),
        digest("synthetic config"),
        selection,
    )


def test_queue_status_is_read_only_and_failure_keeps_exact_retryable_input(queue):
    assert queue.inspect().pending == () and not queue.deployment.state_root.exists()
    request = job(queue)
    assert queue.enqueue(request) == request.id
    queue.failed(request, "unflushed")
    pending = queue.inspect().pending
    assert len(pending) == 1 and pending[0].attempts == 1 and pending[0].error == "unflushed"
    assert pending[0].model_dump(exclude={"attempts", "error", "not_before"}) == request.model_dump(
        exclude={"attempts", "error", "not_before"}
    )
    queue.complete(pending[0], {"inserted": 1, "notes_model_calls": 0})
    state = queue.inspect()
    assert not state.pending and state.history[0].job == request.id
    assert {p.name for p in queue.folder.iterdir()} == {
        queue.policy.lock_file,
        queue.policy.state_file,
    }


def test_full_queue_refuses_new_event_without_discarding_pending_work(queue):
    queue.policy = queue.policy.model_copy(update={"max_pending": 1})
    request = job(queue)
    queue.enqueue(request)
    before = queue.inspect()
    with pytest.raises(SetupError, match="full"):
        queue.enqueue(job(queue))
    assert queue.inspect() == before


def test_separate_worker_lock_allows_enqueue_and_refuses_second_worker(queue):
    with queue.worker():
        queue.enqueue(job(queue))
        with pytest.raises(SetupError, match="busy"):
            with queue.worker():
                pytest.fail("concurrent worker was admitted")
    assert len(queue.inspect().pending) == 1


def test_brief_journal_contention_retries_without_losing_a_native_request(queue):
    queue._prepare()
    request = job(queue)
    errors = []

    def enqueue():
        try:
            queue.enqueue(request)
        except SetupError as exc:
            errors.append(type(exc).__name__)

    with queue.files.locked(queue.folder / queue.policy.lock_file):
        sender = threading.Thread(target=enqueue)
        sender.start()
        time.sleep(0.03)
        assert sender.is_alive()
    sender.join(timeout=2)
    assert not sender.is_alive() and not errors
    assert queue.inspect().pending == (request,)


def test_completion_after_journal_write_failure_can_retry_without_losing_request(
    queue, monkeypatch
):
    request = job(queue)
    queue.enqueue(request)
    before = queue.inspect()
    save = queue.files.save

    def interrupted(*_, **__):
        raise OSError("fixture interrupted completion")

    monkeypatch.setattr(queue.files, "save", interrupted)
    with pytest.raises(OSError, match="interrupted"):
        queue.complete(request, {"inserted": 1})
    assert queue.inspect() == before
    monkeypatch.setattr(queue.files, "save", save)
    queue.complete(request, {"inserted": 0, "already_present": 1})
    assert not queue.inspect().pending


def test_corrupt_or_foreign_queue_is_not_reset_and_cannot_capture_another_path(queue):
    request = job(queue)
    queue.enqueue(request)
    target = queue.folder / queue.policy.state_file
    original = target.read_bytes()
    for replacement in (b"broken-json", json.dumps({"instance": "foreign"}).encode("utf-8")):
        queue.files.save(target, replacement)
        with pytest.raises(SetupError):
            queue.enqueue(job(queue))
        assert target.read_bytes() == replacement
    queue.files.save(target, original)
    with pytest.raises(SetupError, match="outside"):
        queue.enqueue(
            request.model_copy(
                update={
                    "transcript": queue.deployment.cwd / "outside.jsonl",
                    "selection": request.selection.model_copy(
                        update={"transcript": queue.deployment.cwd / "outside.jsonl"}
                    ),
                }
            )
        )
    assert queue.inspect().pending == (request,)


def test_byte_quota_and_history_are_bounded_without_blocking_old_success(queue):
    request = job(queue)
    queue.enqueue(request)
    before = queue.inspect()
    queue.policy = queue.policy.model_copy(update={"max_state_bytes": 1})
    with pytest.raises(SetupError):
        queue.complete(request, {"inserted": 1})
    queue.policy = queue.policy.model_copy(update={"max_state_bytes": 2097152, "max_history": 1})
    assert queue.inspect() == before
    queue.complete(request, {"inserted": 1})
    next_request = job(queue)
    queue.enqueue(next_request)
    queue.complete(next_request, {"already_present": 1})
    assert len(queue.inspect().history) == 1 and queue.inspect().history[0].job == next_request.id


def test_changed_pending_request_and_unsafe_leftover_temp_are_refused(queue):
    request = job(queue)
    queue.enqueue(request)
    with pytest.raises(SetupError, match="changed"):
        queue.complete(request.model_copy(update={"expected": ()}), {"inserted": 1})
    leftover = queue.folder / (queue.policy.temporary_prefix + "interrupted")
    queue.files.save(leftover, b"unpublished state")
    queue.failed(request, "capture_failed")
    assert not leftover.exists()
    leftover.symlink_to(queue.folder / queue.policy.state_file)
    before = queue.inspect()
    with pytest.raises((SetupError, OSError)):
        queue.failed(request, "capture_failed")
    assert queue.inspect() == before and leftover.is_symlink()


def test_permanent_and_exhausted_jobs_move_off_pending_without_losing_input(queue):
    queue.policy = queue.policy.model_copy(update={"max_pending": 1, "max_attempts": 2})
    first = job(queue)
    queue.enqueue(first)
    queue.failed(first, "configuration_changed")
    state = queue.inspect()
    assert not state.pending and state.failed[0].selection == first.selection
    second = job(queue)
    queue.enqueue(second)
    queue.failed(second, "unflushed")
    queue.failed(second, "unflushed")
    state = queue.inspect()
    assert not state.pending and len(state.failed) == 2 and state.failed[-1].attempts == 2
    assert state.failed[-1].expected == second.expected
    retry_id = queue.retry(first.id, "Original configuration restored.")
    assert retry_id == first.id and queue.inspect().pending[0].attempts == 0
    assert queue.inspect().pending[0].selection == first.selection


def test_explicit_configuration_rebind_retains_original_failure_and_pinned_event(queue):
    original = job(queue)
    queue.enqueue(original)
    queue.failed(original, "configuration_changed")
    replacement = queue.retry(
        original.id, "Use the repaired endpoint selectors.", digest("new config")
    )
    state = queue.inspect()
    assert state.failed[0].config_digest == original.config_digest
    assert state.pending[0].id == replacement != original.id
    assert state.pending[0].config_digest == digest("new config")
    assert state.pending[0].selection == state.failed[0].selection
    assert state.history[-1].summary["config_rebound"] is True


def test_terminal_failure_quota_is_visible_and_does_not_drop_exact_pending_input(queue):
    queue.policy = queue.policy.model_copy(update={"max_failed": 1})
    first, second = job(queue), job(queue)
    queue.enqueue(first)
    queue.failed(first, "configuration_changed")
    queue.enqueue(second)
    before = queue.inspect()
    with pytest.raises(SetupError, match="terminal-failure quota"):
        queue.failed(second, "configuration_changed")
    assert queue.inspect() == before
