"""Explicit pin/unpin across real owned graph, model rebuild, HTTP and CLI paths."""

import json

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from test_card_graph import cards as cards
from test_card_graph import lines
from test_card_workflow import card_answer
from test_card_workflow import linked_card as linked_card
from test_graph import note
from test_memory_workflow import _cli_options
from test_memory_workflow import memory_workflow as memory_workflow

from anamnema.card_graph import CardPending
from anamnema.card_operations import EntityRequest
from anamnema.cli import main
from anamnema.graph_store import MemoryConflict, MemoryDenied
from anamnema.memory_models import MemoryInputError
from anamnema.memory_service import create_memory_app
from anamnema.service_config import load_service


def test_unpin_keeps_the_settled_link_and_invalidates_the_card(cards):
    owner, graph = cards
    revision = graph.entity(owner, "example")["revision"]
    graph.commit(owner, "example", revision, lines(), "fixture")
    before = graph.store.note(owner, "note-one")
    graph.pin(owner, "example", "note-one", "No longer needs priority", False)
    assert graph.entity(owner, "example")["revision"] == revision + 1
    inputs = graph.inputs(owner, "example")
    assert len(inputs.notes) == 1 and inputs.notes[0]["pinned"] is False
    assert graph.store.note(owner, "note-one") == before
    with pytest.raises(CardPending):
        graph.card(owner, "example")
    with pytest.raises(MemoryConflict):
        graph.commit(owner, "example", revision, lines(), "stale")
    graph.commit(owner, "example", revision + 1, lines(), "fresh")
    assert graph.card(owner, "example")["statements"]


def test_pin_establishes_a_settled_link_without_a_model_call(cards):
    owner, graph = cards
    graph.store.put(owner, (note(owner, "second"),))
    before = graph.store.note(owner, "second")
    graph.pin(owner, "example", "second", "Keep this evidence selected", True)
    selected = {item["id"]: item for item in graph.inputs(owner, "example").notes}
    assert selected["second"]["pinned"] is True
    assert graph.store.note(owner, "second") == before
    rows = graph.store.read(
        f"MATCH (n:{graph.store.settings.note_label} {{instance:$owner,note_id:'second'}})"
        f"-[r:ABOUT_ENTITY]->(e:{graph.store.settings.entity_label} {{name:'example'}}) "
        "RETURN r.settled AS settled,r.reason AS reason",
        owner=owner,
    )
    assert rows == [{"settled": True, "reason": "Keep this evidence selected"}]


def test_unpin_does_not_invent_a_link_for_an_unlinked_note(cards):
    owner, graph = cards
    graph.store.put(owner, (note(owner, "second"),))
    before = graph.entity(owner, "example")
    source = graph.store.note(owner, "second")
    with pytest.raises(MemoryDenied, match="unpin needs"):
        graph.pin(owner, "example", "second", "Not linked", False)
    assert graph.entity(owner, "example") == before
    assert graph.store.note(owner, "second") == source
    assert [item["id"] for item in graph.inputs(owner, "example").notes] == ["note-one"]


@pytest.mark.parametrize("pinned", [True, False])
@pytest.mark.parametrize("bad", ["foreign-note", "missing-note", "foreign-entity", "dropped"])
def test_pin_refusals_leave_owned_state_unchanged(cards, pinned, bad):
    owner, graph = cards
    identity, name = "note-one", "example"
    if bad == "foreign-note":
        graph.store.put(owner + "other", (note(owner + "other", "foreign-note"),))
        identity = "foreign-note"
    elif bad == "missing-note":
        identity = "absent"
    elif bad == "foreign-entity":
        graph.create(owner + "other", "foreign-entity", "project", "Other owner")
        name = "foreign-entity"
    else:
        graph.decide(owner, "example", "drop", "Retired")
    before = graph.entity(owner, "example")
    source = graph.store.note(owner, "note-one")
    with pytest.raises(MemoryDenied):
        graph.pin(owner, name, identity, "Must be refused", pinned)
    assert graph.entity(owner, "example") == before
    assert graph.store.note(owner, "note-one") == source


@pytest.mark.parametrize("value", ["false", "true", 0, 1, None, [], {}])
def test_direct_pin_flag_is_not_interpreted_by_truthiness(cards, value):
    owner, graph = cards
    before = graph.entity(owner, "example")
    with pytest.raises(MemoryInputError):
        graph.pin(owner, "example", "note-one", "Explicit boolean only", value)
    assert graph.entity(owner, "example") == before


@pytest.mark.parametrize("operation", ["pin", "unpin"])
def test_pin_requests_require_only_their_own_fields(operation):
    good = {"operation": operation, "name": "example", "note": "note", "reason": "Reason"}
    assert EntityRequest(**good).operation == operation
    for changes in ({"note": None}, {"reason": None}, {"kind": "project"}, {"pinned": False}):
        with pytest.raises(ValidationError):
            EntityRequest(**{**good, **changes})


def test_http_pin_unpin_and_rebuild_keep_the_authenticated_owner(linked_card):
    owner, _, engine, _, root, model = linked_card
    settings = load_service()
    client = TestClient(create_memory_app(engine, settings, {owner: "own", "other": "other"}))
    headers = {"Authorization": "Bearer own"}
    command = {"operation": "pin", "name": "example", "note": root, "reason": "Keep evidence"}
    before = engine.card_graph.entity(owner, "example")
    assert client.post(settings.entity_path, json=command).status_code == 401
    assert (
        client.post(
            settings.entity_path,
            json=command,
            headers={
                "Authorization": "Bearer other",
                "X-Assistant": owner,
            },
        ).status_code
        == 403
    )
    assert engine.card_graph.entity(owner, "example") == before
    pinned = client.post(settings.entity_path, json=command, headers=headers)
    assert pinned.status_code == 200 and pinned.json()["pinned"] is True
    assert len(model.calls) == 1
    assert (
        client.post(settings.card_path, json={"name": "example"}, headers=headers).status_code
        == 503
    )
    card_answer(model, "Retention is seven days.")
    rebuilt = client.post(
        settings.card_path, json={"name": "example", "rebuild": True}, headers=headers
    )
    assert rebuilt.status_code == 200
    assert engine.card_graph.inputs(owner, "example").notes[0]["pinned"] is True
    unpinned = client.post(
        settings.entity_path, json={**command, "operation": "unpin"}, headers=headers
    )
    assert unpinned.status_code == 200 and unpinned.json()["pinned"] is False
    assert len(engine.card_graph.inputs(owner, "example").notes) == 1
    assert engine.card_graph.inputs(owner, "example").notes[0]["pinned"] is False


def test_pin_during_generation_refuses_the_old_revision(linked_card):
    owner, _, engine, _, root, model = linked_card
    card_answer(model, "Old generated output")

    def changed():
        model.after_chat = None
        engine.card_graph.pin(owner, "example", root, "Keep current evidence", True)

    model.after_chat = changed
    with pytest.raises(MemoryConflict, match="inputs changed"):
        engine.cards.rebuild(owner, "example")
    with pytest.raises(CardPending):
        engine.card_graph.card(owner, "example")
    assert engine.card_graph.inputs(owner, "example").notes[0]["pinned"] is True


def test_cli_pin_unpin_do_not_need_a_card_model(memory_workflow, model_service, tmp_path, capsys):
    owner, _, engine, encoder, _, _, _, archive = memory_workflow
    root = engine.search(owner, "retention")["notes"][0]["id"]
    graph = engine.card_graph
    graph.create(owner, "example", "project", "Explicit test entity")
    options = _cli_options(owner, engine, encoder, archive, tmp_path, model_service.port)
    for flag, pinned in (("--pin-note", True), ("--unpin-note", False)):
        assert (
            main(["entities", flag, root, "--entity", "example", "--reason", "Test", *options]) == 0
        )
        result = json.loads(capsys.readouterr().out)
        assert result["pinned"] is pinned and result["note"] == root
        assert graph.inputs(owner, "example").notes[0]["pinned"] is pinned
    assert (
        main(
            [
                "entities",
                "--link",
                root,
                "--entity",
                "example",
                "--pin",
                "--reason",
                "Keep",
                *options,
            ]
        )
        == 0
    )
    capsys.readouterr()
    assert graph.inputs(owner, "example").notes[0]["pinned"] is True
    before = graph.entity(owner, "example")
    assert main(["entities", "--pin-note", root, "--reason", "No target", *options]) == 2
    capsys.readouterr()
    assert graph.entity(owner, "example") == before
    assert model_service.calls == []
