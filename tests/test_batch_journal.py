"""Durable ordered operations foundation, not a claim of a complete nightly pipeline."""

import hashlib
import json
import subprocess
import sys
import uuid

import pytest
from test_graph import check, note

from anamnema.archive_models import digest
from anamnema.batch_input import input_digest
from anamnema.batch_journal import BatchJournal
from anamnema.batch_models import BatchPlan, batch_policy
from anamnema.graph_corrections import correct
from anamnema.memory_models import Outcome, Rewrite
from anamnema.private_files import PrivateStateBusy
from anamnema.setup_config import SetupError


@pytest.fixture
def journal(tmp_path):
    source = tmp_path / "prepared.jsonl"
    source.write_bytes(b"synthetic immutable prepared input\n")
    plan = BatchPlan(
        instance="fixture",
        session="session",
        input_path=source,
        input_digest=hashlib.sha256(source.read_bytes()).hexdigest(),
        profile_digest=digest("synthetic deployment"),
        stages=("import", "process", "store"),
    )
    return BatchJournal(tmp_path / "batches", plan, batch_policy())


def test_status_creates_nothing_and_all_stages_must_succeed_in_order(journal):
    assert not journal.inspect().complete and not journal.root.exists()
    with journal.worker() as work:
        assert work.begin() == "import"
        with pytest.raises(SetupError, match="has not finished"):
            work.begin()
        with pytest.raises(SetupError, match="running stage"):
            work.succeed("store", {"inserted": 1})
        work.succeed("import", {"verified": 2})
        assert not journal.inspect().complete
        assert work.begin() == "process"
        work.succeed("process", {"chunks": 1})
        assert work.begin() == "store"
        work.succeed("store", {"inserted": 1})
        assert work.begin() is None
    assert journal.inspect().complete
    with pytest.raises(SetupError, match="lease has ended"):
        work.begin()


def test_interrupted_and_failed_attempts_remain_visible_and_later_stages_do_not_run(journal):
    with journal.worker() as work:
        assert work.begin() == "import"
        # Deliberately leave without a result, as after committed effects and a lost reply.
    assert journal.inspect().stages[0].attempts[-1].status == "running"
    with journal.worker() as work:
        assert work.begin() == "import"
        work.fail("import", "operation_failed", {"verified_before_failure": 1})
    state = journal.inspect()
    assert not state.complete and not state.stages[1].attempts
    assert [a.category for a in state.stages[0].attempts] == ["interrupted", "operation_failed"]
    with journal.worker() as work:
        assert work.begin() == "import"
        work.succeed("import", {"already_verified": 1})
        assert work.begin() == "process"
        work.fail("process", "timeout")
    attempts = journal.inspect().stages[0].attempts
    assert len(attempts) == 3 and attempts[1].metrics == {"verified_before_failure": 1}


def test_input_change_and_plan_change_refuse_without_advancing(journal):
    with journal.worker() as work:
        assert work.begin() == "import"
        before = journal.inspect()
        journal.plan.input_path.write_bytes(b"different input")
        with pytest.raises(SetupError, match="input changed"):
            work.succeed("import", {})
        assert journal.inspect() == before
    changed = journal.plan.model_copy(update={"profile_digest": digest("changed profile")})
    with pytest.raises(SetupError, match="plan changed"):
        BatchJournal(journal.root, changed, journal.policy, identity=journal.plan.identity)


def test_two_workers_cannot_run_same_job_and_status_does_not_need_worker_lock(journal):
    other = BatchJournal(journal.root, journal.plan, journal.policy)
    with journal.worker() as work:
        assert work.begin() == "import"
        assert other.inspect().stages[0].attempts[-1].status == "running"
        with pytest.raises(PrivateStateBusy):
            with other.worker():
                pytest.fail("second worker admitted")


def test_invalid_counters_and_state_quota_preserve_prior_running_state(journal):
    with journal.worker() as work:
        assert work.begin() == "import"
        before = journal.inspect()
        with pytest.raises(SetupError, match="numeric"):
            work.succeed("import", {"invalid": "synthetic-private-value"})
        with pytest.raises(SetupError, match="invalid batch transition"):
            work.succeed("import", {"invalid": float("nan")})
        assert journal.inspect() == before
        original = journal.policy
        size = (journal.folder / journal.policy.state_file).stat().st_size
        journal.policy = original.model_copy(update={"max_state_bytes": size})
        with pytest.raises(SetupError, match="byte limit"):
            work.succeed("import", {"inserted": 1})
        journal.policy = original
        assert journal.inspect() == before


def test_lost_stage_result_replays_real_graph_write_without_replacing_a_correction(journal, graph):
    owner = "batch-" + uuid.uuid4().hex
    plan = journal.plan.model_copy(update={"instance": owner, "stages": ("store",)})
    journal = BatchJournal(journal.root, plan, journal.policy)
    proposed = note(owner)
    with journal.worker() as work:
        assert work.begin() == "store"
        assert graph.put(owner, (proposed,)) == 1
    corrected_vectors = Rewrite(
        query_vector=(0.0, 1.0, 0.0, 0.0),
        passage_vector=(0.0, 1.0, 0.0, 0.0),
        embedded_with="synthetic-encoder",
    )
    correct(
        graph,
        owner,
        check(outcome=Outcome.PARTLY, rewrite="Corrected source decision."),
        corrected_vectors,
    )
    before = graph.note(owner, proposed.id)
    with journal.worker() as work:
        assert work.begin() == "store"
        assert graph.put(owner, (proposed,)) == 0
        work.succeed("store", {"inserted": 0, "already_present": 1})
    assert graph.note(owner, proposed.id) == before and journal.inspect().complete
    assert journal.inspect().stages[0].attempts[0].category == "interrupted"


def test_killed_real_python_worker_releases_lease_but_leaves_running_stage(journal, tmp_path):
    config = tmp_path / "job.json"
    config.write_text(
        json.dumps(
            {
                "root": str(journal.root),
                "plan": journal.plan.model_dump(mode="json"),
                "policy": journal.policy.model_dump(mode="json"),
            }
        ),
        encoding="utf-8",
    )
    code = """import json, pathlib, sys, time
from anamnema.batch_models import BatchPlan, BatchPolicy
from anamnema.batch_journal import BatchJournal
data=json.loads(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"))
plan=BatchPlan.model_validate_json(json.dumps(data["plan"]))
policy=BatchPolicy.model_validate_json(json.dumps(data["policy"]))
with BatchJournal(pathlib.Path(data["root"]),plan,policy).worker() as work:
    assert work.begin() == "import"
    print("ready",flush=True)
    time.sleep(60)
"""
    process = subprocess.Popen(  # noqa: S603 - fixture-only Python process, no model or service
        [sys.executable, "-c", code, str(config)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
    )
    try:
        assert process.stdout.readline().strip() == "ready"
        process.kill()
        _, errors = process.communicate(timeout=5)
        assert process.returncode != 0 and not errors
    finally:
        if process.poll() is None:
            process.kill()
        process.communicate(timeout=5)
    assert journal.inspect().stages[0].attempts[-1].status == "running"
    with journal.worker() as work:
        assert work.begin() == "import"
        work.fail("import", "operation_failed")
    assert journal.inspect().stages[0].attempts[0].category == "interrupted"


def test_corrupt_journal_never_resets_or_repairs_itself(journal):
    with journal.worker() as work:
        assert work.begin() == "import"
        work.fail("import", "operation_failed")
    path = journal.folder / journal.policy.state_file
    path.write_bytes(b"corrupt fixture journal")
    with pytest.raises(SetupError, match="invalid"):
        journal.inspect()
    assert path.read_bytes() == b"corrupt fixture journal"


def test_input_bounds_and_symlink_refusal_do_not_mutate_prepared_source(journal, tmp_path):
    before = journal.plan.input_path.read_bytes()
    small = journal.policy.model_copy(update={"max_input_bytes": len(before) - 1})
    with pytest.raises(SetupError, match="byte limit"):
        input_digest(journal.plan.input_path, small)
    linked = tmp_path / "linked-input"
    linked.symlink_to(journal.plan.input_path)
    with pytest.raises(SetupError, match="symlink"):
        input_digest(linked, journal.policy)
    assert journal.plan.input_path.read_bytes() == before and not journal.root.exists()


def test_attempt_quota_refuses_retry_without_losing_failed_history(journal):
    journal.policy = journal.policy.model_copy(update={"max_attempts": 1})
    with journal.worker() as work:
        assert work.begin() == "import"
        work.fail("import", "operation_failed", {"verified": 1})
    before = journal.inspect()
    with journal.worker() as work:
        with pytest.raises(SetupError, match="attempt bound"):
            work.begin()
    assert journal.inspect() == before
