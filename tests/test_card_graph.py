"""Real Neo4j card lifecycle, source ownership, revision guards and correction invalidation."""

import uuid

import pytest
from test_graph import check, note

from anamnema.card_graph import CardGraph, CardPending
from anamnema.card_policy import card_policy
from anamnema.graph_corrections import correct
from anamnema.graph_store import MemoryConflict, MemoryDenied
from anamnema.memory_models import Outcome


@pytest.fixture
def cards(graph):
    owner = "card-fixture-" + uuid.uuid4().hex
    cards = CardGraph(graph, card_policy())
    cards.create(owner, "example", "project", "Explicit example entity")
    graph.put(owner, (note(owner),))
    cards.link(owner, "example", "note-one", "Explicit source link", pinned=True)
    return owner, cards


def lines():
    return [
        {
            "text": "Retention is fourteen days.",
            "notes": ["note-one"],
            "front": True,
            "score": 0.75,
            "who": True,
            "moved": False,
        }
    ]


def test_owned_card_commit_stale_revision_and_unknown_support_roll_back(cards):
    owner, cards = cards
    with pytest.raises(CardPending):
        cards.card(owner, "example")
    source = cards.inputs(owner, "example")
    assert source.notes[0]["pinned"] is True
    cards.link(owner, "example", "note-one", "Relink preserves an explicit pin")
    source = cards.inputs(owner, "example")
    assert source.notes[0]["pinned"] is True
    revision = source.entity["revision"]
    cards.commit(owner, "example", revision, lines(), "synthetic-writer")
    before = cards.card(owner, "example")
    assert before["statements"][0]["historical"] is False
    with pytest.raises(MemoryDenied):
        cards.commit(owner, "example", revision, [{**lines()[0], "notes": ["foreign-note"]}], "bad")
    assert cards.card(owner, "example") == before
    cards.link(owner, "example", "note-one", "Change source pin", pinned=False)
    with pytest.raises(MemoryConflict, match="inputs changed"):
        cards.commit(owner, "example", revision, lines(), "stale")
    with pytest.raises(CardPending):
        cards.card(owner, "example")
    with pytest.raises(MemoryDenied):
        cards.entity(owner + "other", "example")


def test_drop_and_undrop_preserve_reasons_and_do_not_restore_old_links(cards):
    owner, cards = cards
    with pytest.raises(MemoryConflict):
        cards.create(owner, "example", "project", "Taken name")
    cards.decide(owner, "example", "drop", "Retired project")
    assert cards.entity(owner, "example")["dropped_why"] == "Retired project"
    with pytest.raises(MemoryDenied):
        cards.card(owner, "example")
    with pytest.raises(MemoryDenied):
        cards.link(owner, "example", "note-one", "No retired link")
    cards.decide(owner, "example", "undrop", "Project resumed")
    assert cards.inputs(owner, "example").notes == []
    assert [event["reason"] for event in cards.history(owner, "example")] == [
        "Explicit example entity",
        "Retired project",
        "Project resumed",
    ]
    with pytest.raises(MemoryConflict):
        cards.decide(owner, "example", "undrop", "Not dropped")


def test_correction_invalidates_card_and_retains_affected_entity_after_deletion(cards):
    owner, cards = cards
    revision = cards.entity(owner, "example")["revision"]
    cards.commit(owner, "example", revision, lines(), "synthetic")
    assert correct(cards.store, owner, check("outdated", outcome=Outcome.OUTDATED))
    assert cards.affected(owner, "outdated") == ["example"]
    with pytest.raises(CardPending):
        cards.card(owner, "example")
    revision = cards.entity(owner, "example")["revision"]
    cards.commit(owner, "example", revision, lines(), "history")
    assert cards.card(owner, "example")["statements"][0]["historical"] is True
    assert correct(cards.store, owner, check("delete", outcome=Outcome.WRONG, delete=True))
    assert cards.affected(owner, "delete") == ["example"]
    with pytest.raises(CardPending):
        cards.card(owner, "example")
