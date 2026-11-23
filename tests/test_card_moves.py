"""Manual intent, visible placement and revision changes share the actual graph transaction."""

import json

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from test_card_graph import cards as cards
from test_card_graph import lines
from test_card_workflow import card_answer
from test_card_workflow import linked_card as linked_card
from test_extraction import completion
from test_memory_workflow import _cli_options
from test_memory_workflow import memory_workflow as memory_workflow

from anamnema.card_graph import CardPending
from anamnema.card_moves import CardMoves
from anamnema.card_operations import CardRequest
from anamnema.card_rating import LOWERED, RAISED, CardRanker
from anamnema.cli import main
from anamnema.graph_store import MemoryConflict, MemoryDenied
from anamnema.memory_models import MemoryInputError
from anamnema.memory_service import create_memory_app
from anamnema.service_config import load_service


def ready(owner, graph):
    revision = graph.entity(owner, "example")["revision"]
    rows = [
        {**lines()[0], "text": f"Fact {i}", "front": i < graph.policy.front}
        for i in range(graph.policy.front + 1)
    ]
    graph.commit(owner, "example", revision, rows, "fixture")
    return graph.card(owner, "example", 2)["statements"][0]["id"]


def test_promotion_cap_replacement_and_reranking_are_durable(cards):
    owner, graph = cards
    target = ready(owner, graph)
    revision = graph.entity(owner, "example")["revision"]
    source = graph.store.note(owner, "note-one")
    moves = CardMoves(graph)
    result = moves.move(owner, "example", target[:16], "Keep this visible", True)
    assert result["moved"] == target and result["bumped"] is not None
    front = graph.card(owner, "example")["statements"]
    assert len(front) == graph.policy.front and any(item["id"] == target for item in front)
    inputs = graph.inputs(owner, "example")
    assert inputs.entity["revision"] == revision + 1
    assert len(inputs.moves) == 1 and inputs.moves[0]["reason"] == "Keep this visible"
    rewritten = [{"text": "Fact 4", "notes": ["note-one"], "front": False}]
    assert CardRanker._tiers(rewritten, inputs.moves) == ([RAISED], [])
    with pytest.raises(MemoryConflict):
        graph.commit(owner, "example", revision, lines(), "stale")
    moves.move(owner, "example", target, "Move to facts", False)
    inputs = graph.inputs(owner, "example")
    assert len(inputs.moves) == 1 and inputs.moves[0]["front"] is False
    assert CardRanker._tiers(rewritten, inputs.moves) == ([LOWERED], [])
    assert all(item["id"] != target for item in graph.card(owner, "example")["statements"])
    assert graph.store.note(owner, "note-one") == source


@pytest.mark.parametrize("stage", ["before_record", "after_record", "after_cap", "after_revision"])
def test_interruption_rolls_back_every_part_and_retry_is_durable(cards, monkeypatch, stage):
    owner, graph = cards
    target = ready(owner, graph)
    moves = CardMoves(graph)
    before = (
        graph.card(owner, "example"),
        graph.card(owner, "example", 2),
        graph.inputs(owner, "example"),
    )
    method = {
        "before_record": "_record",
        "after_record": "_record",
        "after_cap": "_cap",
        "after_revision": "_advance",
    }[stage]
    original = getattr(moves, method)

    def stopped(*args, **kwargs):
        if stage != "before_record":
            original(*args, **kwargs)
        raise RuntimeError("fixture interrupted")

    monkeypatch.setattr(moves, method, stopped)
    with pytest.raises(RuntimeError, match="fixture interrupted"):
        moves.move(owner, "example", target, "Keep this visible", True)
    assert (
        graph.card(owner, "example"),
        graph.card(owner, "example", 2),
        graph.inputs(owner, "example"),
    ) == before
    monkeypatch.setattr(moves, method, original)
    moves.move(owner, "example", target, "Keep this visible", True)
    assert len(graph.inputs(owner, "example").moves) == 1


@pytest.mark.parametrize("bad", ["foreign", "missing", "ambiguous", "dirty", "dropped"])
def test_refused_selection_or_stale_card_leaves_state_unchanged(cards, bad):
    owner, graph = cards
    target = ready(owner, graph)
    caller = owner
    if bad == "foreign":
        caller += "other"
    elif bad == "missing":
        target = "missing"
    elif bad == "ambiguous":
        # Give two statements a shared prefix through the disposable store only.
        graph.store.write(
            lambda tx: tx.run(
                f"MATCH (s:{graph.store.settings.statement_label} {{instance:$owner}}) "
                "SET s.id='shared-'+s.id",
                owner=owner,
            ).consume()
        )
        target = "shared-"
    elif bad == "dirty":
        graph.pin(owner, "example", "note-one", "Changed evidence", False)
    else:
        graph.decide(owner, "example", "drop", "Retired")
    before = graph.entity(owner, "example"), graph._lines(owner, "example")
    with pytest.raises((MemoryDenied, CardPending)):
        CardMoves(graph).move(caller, "example", target, "Refuse", True)
    assert (graph.entity(owner, "example"), graph._lines(owner, "example")) == before


@pytest.mark.parametrize("front", ["false", "true", 0, 1, None, [], {}])
def test_direct_move_does_not_use_truthiness(cards, front):
    owner, graph = cards
    before = graph.entity(owner, "example")
    with pytest.raises(MemoryInputError):
        CardMoves(graph).move(owner, "example", "line", "No coercion", front)
    assert graph.entity(owner, "example") == before


def test_forget_is_owned_reasoned_and_requires_rebuild(cards):
    owner, graph = cards
    target = ready(owner, graph)
    moves = CardMoves(graph)
    identity = moves.move(owner, "example", target, "Keep", True)["move"]
    before = graph.inputs(owner, "example")
    with pytest.raises(MemoryDenied):
        moves.forget(owner + "other", "example", identity, "No permission")
    with pytest.raises(MemoryDenied):
        moves.forget(owner, "example", "missing", "Not present")
    assert graph.inputs(owner, "example") == before
    assert moves.forget(owner, "example", identity, "No longer relevant")["rebuild_pending"]
    assert graph.inputs(owner, "example").moves == []
    assert graph.history(owner, "example")[-1]["reason"] == "No longer relevant"
    with pytest.raises(CardPending):
        graph.card(owner, "example")


def test_forget_failure_preserves_intent_card_and_history(cards, monkeypatch):
    owner, graph = cards
    target = ready(owner, graph)
    moves = CardMoves(graph)
    identity = moves.move(owner, "example", target, "Keep", True)["move"]
    before = graph.card(owner, "example"), graph.history(owner, "example")
    original = moves._advance

    def stopped(*args, **kwargs):
        original(*args, **kwargs)
        raise RuntimeError("fixture interrupted after forget")

    monkeypatch.setattr(moves, "_advance", stopped)
    with pytest.raises(RuntimeError, match="after forget"):
        moves.forget(owner, "example", identity, "Forget")
    assert (graph.card(owner, "example"), graph.history(owner, "example")) == before


def test_actual_rebuild_preserves_demotion_and_exposes_ambiguous_intent(linked_card):
    owner, _, engine, _, _, model = linked_card
    graph = engine.card_graph
    target = graph.card(owner, "example")["statements"][0]["id"]
    decision = CardMoves(graph).move(owner, "example", target, "Later page", False)
    card_answer(model, "Renamed retention rule.")
    assert engine.cards.rebuild(owner, "example")["problems"] == []
    assert graph.card(owner, "example")["statements"] == []
    assert graph.card(owner, "example", 2)["statements"][0]["text"] == "Renamed retention rule."
    model.chat_replies.append(
        completion(
            json.dumps(
                {
                    "statements": [
                        {"text": "One aspect", "notes": ["n1"], "front": True},
                        {"text": "Another aspect", "notes": ["n1"], "front": False},
                    ]
                }
            )
        )
    )
    result = engine.cards.rebuild(owner, "example")
    assert result["problems"] == ["manual move needs settlement: " + decision["move"]]
    intent = graph.card(owner, "example")["moves"]
    assert len(intent) == 1 and intent[0]["unsettled"] is True


@pytest.mark.parametrize("operation", ["promote", "demote", "forget"])
def test_request_refuses_missing_or_unrelated_move_fields(operation):
    good = {"name": "example", "operation": operation, "statement": "line", "reason": "Reason"}
    assert CardRequest(**good).operation == operation
    for changed in ({"reason": None}, {"statement": None}, {"page": 2}, {"rebuild": True}):
        with pytest.raises(ValidationError):
            CardRequest(**{**good, **changed})


def test_http_move_bearer_owner_and_stale_generation_guard(linked_card):
    owner, _, engine, _, _, model = linked_card
    graph, settings = engine.card_graph, load_service()
    client = TestClient(create_memory_app(engine, settings, {owner: "own", "other": "other"}))
    target = graph.card(owner, "example")["statements"][0]["id"]
    body = {"name": "example", "operation": "demote", "statement": target, "reason": "Later page"}
    assert client.post(settings.card_path, json=body).status_code == 401
    assert (
        client.post(
            settings.card_path,
            json=body,
            headers={"Authorization": "Bearer other", "X-Assistant": owner},
        ).status_code
        == 403
    )
    response = client.post(settings.card_path, json=body, headers={"Authorization": "Bearer own"})
    assert response.status_code == 200 and response.json()["front"] is False
    assert len(model.calls) == 1
    card_answer(model, "Retention is seven days.")

    def changed():
        model.after_chat = None
        CardMoves(graph).move(owner, "example", target, "Keep visible", True)

    model.after_chat = changed
    with pytest.raises(MemoryConflict):
        engine.cards.rebuild(owner, "example")
    assert graph.card(owner, "example")["statements"][0]["front"] is True


def test_cli_move_and_forget_need_no_model(memory_workflow, model_service, tmp_path, capsys):
    owner, _, engine, encoder, _, _, _, archive = memory_workflow
    root = engine.search(owner, "retention")["notes"][0]["id"]
    graph = engine.card_graph
    graph.create(owner, "example", "project", "Fixture")
    graph.link(owner, "example", root, "Fixture support")
    graph.commit(
        owner,
        "example",
        graph.entity(owner, "example")["revision"],
        [{**lines()[0], "notes": [root]}],
        "fixture",
    )
    target = graph.card(owner, "example")["statements"][0]["id"]
    options = _cli_options(owner, engine, encoder, archive, tmp_path, model_service.port)
    base = ["card", "--entity", "example", *options]
    assert main([*base, "--demote", target, "--reason", "Later"]) == 0
    move = json.loads(capsys.readouterr().out)["move"]
    assert main([*base, "--promote", target]) == 2
    capsys.readouterr()
    assert main([*base, "--forget-move", move, "--reason", "Forget"]) == 0
    assert json.loads(capsys.readouterr().out)["rebuild_pending"]
    assert model_service.calls == []
