"""Real ordered effects, interruption and resume; callbacks are synthetic stage operations."""

import pytest
from test_batch_journal import journal as journal

from anamnema.batch_runner import BatchRunner
from anamnema.setup_config import SetupError


def test_missing_or_extra_stage_operation_refuses_before_journal_creation(journal):
    with pytest.raises(SetupError, match="every pinned stage"):
        BatchRunner(journal, {})
    assert not journal.root.exists()


def test_order_failure_and_resume_do_not_reexecute_recorded_success(journal, tmp_path):
    events = []
    stages = journal.plan.stages

    def record(stage):
        events.append(stage)
        path = tmp_path / (stage + ".effect")
        if not path.exists():
            path.write_text("synthetic durable effect", encoding="utf-8")
        return {"written": True}

    def fail():
        record(stages[1])
        raise SetupError("fixture dependency unavailable after a verified effect")

    operations = {stage: lambda stage=stage: record(stage) for stage in stages}
    operations[stages[1]] = fail
    with pytest.raises(SetupError, match="dependency unavailable"):
        BatchRunner(journal, operations).run()
    state = journal.inspect()
    assert state.stages[0].complete and state.stages[1].attempts[-1].status == "failed"
    assert events == list(stages[:2])
    operations[stages[1]] = lambda: record(stages[1])
    assert BatchRunner(journal, operations).run().complete
    assert events == [*stages[:2], *stages[1:]]
    before = list(events)
    assert BatchRunner(journal, operations).run().complete and events == before


def test_unexpected_stop_keeps_running_marker_for_interrupted_replay(journal):
    stages = journal.plan.stages

    def crash():
        raise RuntimeError("fixture crash after remote commit, before result")

    operations = {stage: lambda: {"ok": True} for stage in stages}
    operations[stages[0]] = crash
    with pytest.raises(RuntimeError, match="fixture crash"):
        BatchRunner(journal, operations).run()
    assert journal.inspect().stages[0].attempts[-1].status == "running"
    operations[stages[0]] = lambda: {"replayed": True}
    result = BatchRunner(journal, operations).run()
    assert result.complete
    assert result.stages[0].attempts[0].category == "interrupted"


def test_unverified_nonnumeric_result_cannot_advance_later_stage(journal):
    operations = {stage: lambda: {"ok": True} for stage in journal.plan.stages}
    operations[journal.plan.stages[0]] = lambda: {"unsafe": "synthetic raw model text"}
    with pytest.raises(SetupError, match="numeric counters"):
        BatchRunner(journal, operations).run()
    state = journal.inspect()
    assert state.stages[0].attempts[-1].status == "failed"
    assert not any(stage.attempts for stage in state.stages[1:])
