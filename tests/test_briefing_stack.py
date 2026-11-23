"""Actual private journals and read gates prove budget and interrupted delivery boundaries."""

import json
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from test_continuity import ack, state
from test_continuity import continuity as continuity

from anamnema.briefing_files import BriefFiles, brief_file_policy
from anamnema.briefing_policy import briefing_policy
from anamnema.briefing_render import RenderedBrief
from anamnema.briefing_stack import BriefStack, stack_policy
from anamnema.cli import main
from anamnema.private_files import PrivateStateBusy
from anamnema.setup_config import SetupError


@pytest.fixture
def stack(continuity, tmp_path):
    bundles = BriefFiles(tmp_path / "bundles", brief_file_policy())
    return BriefStack(tmp_path / "stack", bundles, continuity, briefing_policy(), stack_policy())


def bundle(stack, text="Synthetic ready recall.\n", status="ready"):
    rendered = RenderedBrief((text,), (1,), (), "Review.\n")
    return stack.bundles.save(stack.continuity.config.instance, rendered, {"status": status})


def test_status_creates_nothing_and_delivery_requires_real_ack(stack):
    assert stack.status("session-one")["held"] == {} and not stack.root.exists()
    identity = bundle(stack)
    original = stack.bundles.read("example", identity)[1][0].read_bytes()
    first = stack.deliver("session-one", identity)
    held = state(stack.continuity)[1]
    assert len(held.reads) == 1 and held.reads[0].token not in first["required_read"]
    repeated = stack.deliver("session-one", identity)
    assert repeated == first and state(stack.continuity)[1] == held
    assert stack.continuity.stop("session-one", "read it")
    assert stack.continuity.stop("session-one", ack(stack.continuity, held)) == ""
    assert stack.bundles.read("example", identity)[1][0].read_bytes() == original


def test_hard_ceiling_refuses_before_read_debt_and_drop_keeps_files_and_old_debt(stack):
    first = bundle(stack)
    second = bundle(stack, "Another synthetic ready recall.\n")
    stack.briefing = stack.briefing.model_copy(
        update={"context_tokens": 100, "stack_hard_share": 0.1, "stack_soft_share": 0.05}
    )
    stack.deliver("session-one", first)
    before = state(stack.continuity)[1]
    with pytest.raises(SetupError, match="ceiling"):
        stack.deliver("session-one", second)
    assert state(stack.continuity)[1] == before
    assert stack.drop("session-one", first)["dropped"]
    assert state(stack.continuity)[1] == before
    stack.bundles.read("example", first)
    assert stack.deliver("session-one", first)["briefing"] == first
    assert state(stack.continuity)[1] == before


def test_failure_after_reservation_replays_without_double_budget(stack, monkeypatch):
    identity = bundle(stack)
    actual = stack.continuity.require

    def stop(*args, **kwargs):
        raise SetupError("fixture interruption before debt")

    monkeypatch.setattr(stack.continuity, "require", stop)
    with pytest.raises(SetupError, match="interruption"):
        stack.deliver("session-one", identity)
    before = stack.status("session-one")
    monkeypatch.setattr(stack.continuity, "require", actual)
    assert stack.deliver("session-one", identity)["tokens_estimated"] == before["tokens_estimated"]


@pytest.mark.parametrize("status", ["failed", "partial"])
def test_nonready_build_never_creates_stack_or_read_debt(stack, status):
    identity = bundle(stack, status=status)
    with pytest.raises(SetupError, match="only a ready"):
        stack.deliver("session-one", identity)
    assert not stack.root.exists() and not stack.continuity.config.state_root.exists()


def test_bundle_change_before_managed_copy_is_refused_and_retry_retains_reservation(
    stack, monkeypatch
):
    identity = bundle(stack)
    actual = stack.continuity.require

    def corrupt(session, producer, key, path, **kwargs):
        stack.bundles.files.save(path, b"Different content.\n")
        return actual(session, producer, key, path, **kwargs)

    monkeypatch.setattr(stack.continuity, "require", corrupt)
    with pytest.raises(SetupError, match="checksum differs"):
        stack.deliver("session-one", identity)
    assert not stack.continuity.config.state_root.exists()
    assert len(stack.status("session-one")["held"]) == 1


def test_drop_cannot_pass_a_delivery_while_read_registration_is_in_flight(stack, monkeypatch):
    identity = bundle(stack)
    actual = stack.continuity.require
    entered, released = threading.Event(), threading.Event()

    def pause(*args, **kwargs):
        entered.set()
        assert released.wait(10)
        return actual(*args, **kwargs)

    monkeypatch.setattr(stack.continuity, "require", pause)
    with ThreadPoolExecutor(max_workers=1) as pool:
        result = pool.submit(stack.deliver, "session-one", identity)
        try:
            assert entered.wait(10)
            with pytest.raises(PrivateStateBusy):
                stack.drop("session-one", identity)
        finally:
            released.set()
        assert result.result(timeout=10)["briefing"] == identity
    assert stack.drop("session-one", identity)["dropped"]


def test_budget_recalculates_from_verified_size_when_density_changes(stack):
    identity = bundle(stack)
    first = stack.deliver("session-one", identity)
    stack.briefing = stack.briefing.model_copy(update={"bytes_per_token": 1.0})
    assert stack.status("session-one")["tokens_estimated"] > first["tokens_estimated"]


def test_existing_reservation_replays_even_after_the_hard_cap_shrinks(stack, monkeypatch):
    identity = bundle(stack)
    actual = stack.continuity.require

    def stop(*args, **kwargs):
        raise SetupError("fixture interruption")

    monkeypatch.setattr(stack.continuity, "require", stop)
    with pytest.raises(SetupError, match="interruption"):
        stack.deliver("session-one", identity)
    stack.briefing = stack.briefing.model_copy(update={"context_tokens": 1})
    monkeypatch.setattr(stack.continuity, "require", actual)
    result = stack.deliver("session-one", identity)
    assert result["above_hard_limit"] and result["required_read"]


def test_explicit_rerequest_after_drop_and_ack_gets_a_new_read_proof(stack):
    identity = bundle(stack)
    stack.deliver("session-one", identity)
    before = state(stack.continuity)[1]
    assert stack.continuity.stop("session-one", ack(stack.continuity, before)) == ""
    stack.drop("session-one", identity)
    assert stack.status("session-one")["tokens_estimated"] == 0
    stack.deliver("session-one", identity)
    after = state(stack.continuity)[1]
    assert after.reads[0].token != before.reads[0].token
    assert stack.continuity.stop("session-one", ack(stack.continuity, before))
    assert stack.continuity.stop("session-one", ack(stack.continuity, after)) == ""


@pytest.mark.parametrize("text", ["", " \n"])
def test_empty_page_is_a_controlled_refusal_before_any_state(stack, text):
    identity = bundle(stack, text=text)
    with pytest.raises(SetupError, match="page is empty"):
        stack.deliver("session-one", identity)
    assert not stack.root.exists() and not stack.continuity.config.state_root.exists()


def test_cli_read_preview_deliver_status_and_drop_share_actual_read_gate(stack, tmp_path, capsys):
    identity = bundle(stack)
    deployment = tmp_path / "continuity.json"
    deployment.write_text(stack.continuity.config.model_dump_json(), encoding="utf-8")
    common = ["--instance", "example", "--briefing-root", str(stack.bundles.root)]
    session = [
        *common,
        "--session",
        "session-one",
        "--stack-root",
        str(stack.root),
        "--continuity-config",
        str(deployment),
    ]
    assert main(["briefing", "read", *common, "--briefing", identity]) == 0
    assert json.loads(capsys.readouterr().out)["required_read_registered"] is False
    assert main(["briefing", "deliver", *session, "--briefing", identity]) == 0
    assert json.loads(capsys.readouterr().out)["files_written"] is False
    assert not stack.root.exists() and not stack.continuity.config.state_root.exists()
    assert main(["briefing", "deliver", *session, "--briefing", identity, "--apply"]) == 0
    assert "required_read" in json.loads(capsys.readouterr().out)
    held = state(stack.continuity)[1]
    assert stack.continuity.stop("session-one", "not a proof")
    assert main(["briefing", "status", *session]) == 0
    assert len(json.loads(capsys.readouterr().out)["held"]) == 1
    assert main(["briefing", "drop", *session, "--briefing", identity, "--apply"]) == 0
    assert json.loads(capsys.readouterr().out)["dropped"]
    assert state(stack.continuity)[1] == held
    assert stack.continuity.stop("session-one", ack(stack.continuity, held)) == ""
