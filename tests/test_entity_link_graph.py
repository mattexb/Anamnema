"""Actual Neo4j: links land only on the judged text and never override a person's settled answer."""

import uuid
from typing import Any, cast

import pytest
from test_graph import note

from anamnema.archive_models import digest
from anamnema.card_graph import CardGraph
from anamnema.card_policy import card_policy
from anamnema.entity_link_graph import EntityLinkGraph, link_pending
from anamnema.entity_link_policy import entity_link_policy
from anamnema.entity_link_writer import (
    EntityAmbiguity,
    EntityLink,
    EntityLinking,
    EntityLinkWriter,
    EntityProposal,
)

TEXT = "Retention is fourteen days."


@pytest.fixture
def linked(graph: Any) -> tuple[str, EntityLinkGraph, CardGraph]:
    owner = "links-" + uuid.uuid4().hex
    graph.put(owner, (note(owner, kind="fact"),))
    cards = CardGraph(graph, card_policy())
    cards.create(owner, "Andrea", "person", "fixture person")
    return owner, EntityLinkGraph(graph, entity_link_policy()), cards


def _link(text: str = TEXT, entities: tuple[str, ...] = ("Andrea",), **more: Any) -> EntityLink:
    return EntityLink(
        note="note-one",
        text=text,
        entities=entities,
        ambiguous=more.get("ambiguous", ()),
        proposed=more.get("proposed", ()),
    )


def _links(graph: Any, owner: str) -> list[dict[str, Any]]:
    return graph.read(
        f"MATCH (n:{graph.settings.note_label} {{id:$key}})-[r:ABOUT_ENTITY]->(e) "
        "RETURN e.name AS name, r.settled AS settled ORDER BY e.name",
        key=digest([owner, "note-one"]),
    )


def _set_text(graph: Any, owner: str, text: str) -> None:
    graph.write(
        lambda tx: tx.run(
            f"MATCH (n:{graph.settings.note_label} {{id:$key}}) SET n.text=$text",
            key=digest([owner, "note-one"]),
            text=text,
        ).consume()
    )


def test_a_link_on_the_judged_text_lands_and_settles_the_note(graph: Any, linked: Any) -> None:
    owner, links, _cards = linked
    assert [n["id"] for n in links.pending(owner, 10)] == ["note-one"]
    ambiguity = EntityAmbiguity(name="Ann", candidates=("Andrea", "Anna"))
    assert links.write(owner, (_link(ambiguous=(ambiguity,)),), "fixture-model") == 1
    assert _links(graph, owner) == [{"name": "Andrea", "settled": False}]
    assert links.pending(owner, 10) == []
    current = graph.note(owner, "note-one")
    assert current["entity_link_version"] == entity_link_policy().version
    assert "Ann" in current["entity_ambiguous"][0]
    assert links.write(owner + "-foreign", (_link(),), "fixture-model") == 0


def test_a_link_judged_on_text_since_rewritten_does_not_land(graph: Any, linked: Any) -> None:
    owner, links, _cards = linked
    _set_text(graph, owner, "Retention is thirty days.")
    assert links.write(owner, (_link(),), "fixture-model") == 0
    assert _links(graph, owner) == []
    assert [n["id"] for n in links.pending(owner, 10)] == ["note-one"]


def test_a_rewrite_makes_a_linked_note_due_and_relink_keeps_settled(
    graph: Any, linked: Any
) -> None:
    owner, links, cards = linked
    cards.create(owner, "Bruno", "person", "fixture person")
    links.write(owner, (_link(entities=("Andrea",)),), "fixture-model")
    cards.link(owner, "Bruno", "note-one", "set by a person")
    _set_text(graph, owner, "Retention is thirty days.")
    assert [n["id"] for n in links.pending(owner, 10)] == ["note-one"]
    relink = _link(text="Retention is thirty days.", entities=())
    assert links.write(owner, (relink,), "fixture-model") == 1
    assert _links(graph, owner) == [{"name": "Bruno", "settled": True}]


def test_proposals_create_free_names_only_and_dropped_ones_never_link(
    graph: Any, linked: Any
) -> None:
    owner, links, cards = linked
    cards.create(owner, "Gone", "place", "fixture place")
    cards.decide(owner, "Gone", "drop", "fixture drop")
    created = links.propose(
        owner,
        (
            EntityProposal(name="Carla", type="person"),
            EntityProposal(name="Andrea", type="person"),
            EntityProposal(name="Gone", type="place"),
        ),
    )
    assert created == ["Carla"]
    assert cards.entity(owner, "Carla")["status"] == "unconfirmed"
    link = _link(entities=("Andrea",), proposed=("Carla", "Gone"))
    assert links.write(owner, (link,), "fixture-model") == 1
    assert [row["name"] for row in _links(graph, owner)] == ["Andrea", "Carla"]
    assert [entity.name for entity in links.roster(owner)] == ["Andrea", "Carla"]


class _Writer:
    def __init__(self, answer: EntityLinking) -> None:
        self.answer, self.calls = answer, 0

    def judge(self, notes: list[dict[str, Any]], roster: Any) -> EntityLinking:
        self.calls += 1
        assert [n["id"] for n in notes] == ["note-one"] and roster
        return self.answer


def test_link_pending_persists_and_rotates_unanswered_notes(graph: Any, linked: Any) -> None:
    owner, links, _cards = linked
    silent = _Writer(EntityLinking((), (), ("note-one: missing entity answer",), "m"))
    report = link_pending(links, cast(EntityLinkWriter, silent), owner, 10)
    assert report["written"] == 0 and report["problems"]
    assert graph.note(owner, "note-one")["entity_link_attempts"] == 1
    answered = _Writer(EntityLinking((_link(),), (), (), "m"))
    report = link_pending(links, cast(EntityLinkWriter, answered), owner, 10)
    assert report == {"notes": 1, "written": 1, "created": [], "problems": []}
    assert link_pending(links, cast(EntityLinkWriter, answered), owner, 10)["notes"] == 0
