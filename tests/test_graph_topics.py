"""Actual Neo4j placement recovery, complete writes, corrections and owner isolation."""

import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest
from pydantic import ValidationError
from test_graph import check, note

from anamnema.archive_models import digest
from anamnema.graph_corrections import correct
from anamnema.graph_store import MemoryDenied
from anamnema.graph_topics import GraphTopics
from anamnema.memory_config import load_memory
from anamnema.memory_models import Facet, Outcome, Rewrite
from anamnema.setup_config import SetupError


@pytest.fixture
def owner():
    return "topic-fixture-" + uuid.uuid4().hex


def memberships(graph, owner, identity):
    return graph.read(
        f"MATCH (n:{graph.settings.note_label} {{id:$key}})-[r:IN_GROUP]->"
        f"(g:{graph.settings.group_label} {{group_type:'topic'}}) "
        "RETURN g.id AS id, g.instance AS instance, g.exemplar_vector AS vector, "
        "r.similarity AS similarity ORDER BY id",
        key=digest([owner, identity]),
    )


def test_topics_create_join_and_idempotent_marker(graph, owner):
    topics = GraphTopics(graph)
    graph.put(
        owner,
        (
            note(owner, "first", facets=(Facet(kind="topic", value="first"),)),
            note(owner, "second"),
        ),
    )
    assert graph.note(owner, "first")["placement_pending"] is True
    assert topics.place(owner, "first").created == 1
    assert topics.place(owner, "second").joined == 1
    assert topics.place(owner, "second").joined == 0
    assert (
        memberships(graph, owner, "first")[0]["id"] == memberships(graph, owner, "second")[0]["id"]
    )
    assert "placement_pending" not in graph.note(owner, "first")
    assert (
        graph.put(owner, (note(owner, "first", facets=(Facet(kind="topic", value="first"),)),)) == 0
    )
    assert "placement_pending" not in graph.note(owner, "first")


def test_fresh_pass_recovers_corrected_note_without_rewriting(graph, owner):
    graph.put(owner, (note(owner),))
    vectors = Rewrite(
        query_vector=(0.0, 0.0, 1.0, 0.0),
        passage_vector=(0.0, 0.0, 1.0, 0.0),
        embedded_with="synthetic-encoder",
    )
    correct(graph, owner, check(outcome=Outcome.PARTLY, rewrite="Corrected retention."), vectors)
    correct(graph, owner, check("stale", outcome=Outcome.OUTDATED))
    before = graph.note(owner, "note-one")
    assert GraphTopics(graph).place(owner, "note-one").created == 1
    after = graph.note(owner, "note-one")
    assert {k: v for k, v in before.items() if k != "placement_pending"} == after
    assert memberships(graph, owner, "note-one")[0]["vector"] == list(vectors.query_vector)


def test_foreign_or_missing_exemplar_and_group_cannot_mutate(graph, owner):
    other = owner + "-other"
    graph.put(owner, (note(owner),))
    graph.put(other, (note(other),))
    topics = GraphTopics(graph)
    # Note identities are scoped; the same external id never selects the other owner.
    with pytest.raises(MemoryDenied):
        topics.create(owner, "uncreated-" + owner, "missing", (1.0, 0.0, 0.0, 0.0))
    assert topics.place(other, "note-one").created == 1
    foreign = topics.group_id(other, "note-one")
    before = graph.note(owner, "note-one")
    with pytest.raises(MemoryDenied):
        topics.create(owner, foreign, "note-one", (1.0, 0.0, 0.0, 0.0))
    with pytest.raises(MemoryDenied):
        topics.join(owner, "note-one", [(foreign, 0.9)])
    assert graph.note(owner, "note-one") == before
    assert memberships(graph, owner, "note-one") == []
    assert memberships(graph, other, "note-one")[0]["instance"] == other


def test_partial_join_rolls_back_all_edges_and_marker(graph, owner):
    graph.put(owner, (note(owner, "exemplar"), note(owner, "pending")))
    topics = GraphTopics(graph)
    topics.place(owner, "exemplar")
    group = topics.group_id(owner, "exemplar")
    before = graph.note(owner, "pending")
    for joins in ([("missing", 0.9)], [(group, 0.9), ("missing", 0.9)]):
        with pytest.raises(MemoryDenied):
            topics.join(owner, "pending", joins)
        assert graph.note(owner, "pending") == before
        assert memberships(graph, owner, "pending") == []
    topics.join(owner, "pending", [])
    assert graph.note(owner, "pending") == before
    with pytest.raises(SetupError):
        topics.join(owner, "pending", [(group, 0.9), (group, 0.9)])
    with pytest.raises(SetupError):
        topics.join(owner, "pending", [(group, float("nan"))])


def test_cached_exemplars_are_scoped_and_multiple_memberships_are_bounded(graph, owner):
    topics = GraphTopics(graph)
    graph.put(owner, (note(owner, "first"), note(owner, "second"), note(owner, "third")))
    for identity in ("first", "second"):
        topics.create(owner, topics.group_id(owner, identity), identity, (1.0, 0.0, 0.0, 0.0))
        topics.exemplars[owner, topics.group_id(owner, identity)] = (1.0, 0.0, 0.0, 0.0)
    assert topics.place(owner, "third").joined == 2
    other = owner + "-other"
    graph.put(other, (note(other),))
    assert topics.place(other, "note-one").created == 1
    assert len(memberships(graph, other, "note-one")) == 1


def test_concurrent_placement_of_same_note_is_one_effect(graph, owner):
    graph.put(owner, (note(owner),))
    with ThreadPoolExecutor(max_workers=4) as workers:
        results = list(workers.map(lambda _: GraphTopics(graph).place(owner, "note-one"), range(8)))
    assert sum(result.created + result.joined for result in results) == 1
    assert len(memberships(graph, owner, "note-one")) == 1


def test_stale_cached_target_fails_visibly_and_keeps_retry_state(graph, owner):
    graph.put(owner, (note(owner, "first"), note(owner, "second")))
    topics = GraphTopics(graph)
    topics.place(owner, "first")
    graph.write(
        lambda tx: list(
            tx.run(
                f"MATCH (g:{graph.settings.group_label} {{id:$id}}) DETACH DELETE g",
                id=topics.group_id(owner, "first"),
            )
        )
    )
    with pytest.raises(MemoryDenied):
        topics.place(owner, "second")
    assert graph.note(owner, "second")["placement_pending"] is True
    assert GraphTopics(graph).place(owner, "second").created == 1


def test_topic_bounds_validate_and_old_notes_are_not_backfilled(graph, owner):
    settings = load_memory().model_dump(mode="json")
    settings["topics"]["max_memberships"] = settings["topics"]["candidates"] + 1
    with pytest.raises(ValidationError):
        type(load_memory()).model_validate_json(__import__("json").dumps(settings))
    graph.put(owner, (note(owner),))
    graph.write(
        lambda tx: list(
            tx.run(
                f"MATCH (n:{graph.settings.note_label} {{id:$id}}) REMOVE n.placement_pending",
                id=digest([owner, "note-one"]),
            )
        )
    )
    assert GraphTopics(graph).place(owner, "note-one").created == 0
    assert memberships(graph, owner, "note-one") == []
