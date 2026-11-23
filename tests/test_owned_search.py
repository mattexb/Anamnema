"""Regression for global index budgets starving an assistant's own candidates."""

import uuid

from test_graph import note

from anamnema.graph_nearest import owned_nearest
from anamnema.graph_search import GraphSearch
from anamnema.graph_topics import GraphTopics
from anamnema.memory_models import Piece


def test_note_and_word_candidates_widen_past_other_owners(graph):
    owner, other = "owned-" + uuid.uuid4().hex, "other-" + uuid.uuid4().hex
    graph.put(other, tuple(note(other, f"foreign-{i}", pieces=()) for i in range(30)))
    graph.put(owner, (note(owner, "mine", passage_vector=(0.99, 0.01, 0.0, 0.0), pieces=()),))
    search = GraphSearch(graph)
    assert [identity for identity, _ in search.meaning(owner, (1.0, 0.0, 0.0, 0.0), 6)] == ["mine"]
    assert search.words(owner, "fourteen", 6) == ["mine"]
    assert search.meaning("absent-" + owner, (1.0, 0.0, 0.0, 0.0), 6) == []


def test_piece_candidates_widen_past_other_owners(graph):
    owner, other = "piece-" + uuid.uuid4().hex, "other-" + uuid.uuid4().hex
    passage = (0.0, 0.0, 1.0, 0.0)
    graph.put(
        other,
        tuple(
            note(
                other,
                f"foreign-{i}",
                passage_vector=passage,
                pieces=(Piece(offset=10, vector=(1.0, 0.0, 0.0, 0.0)),),
            )
            for i in range(30)
        ),
    )
    graph.put(
        owner,
        (
            note(
                owner,
                "mine",
                passage_vector=passage,
                pieces=(Piece(offset=10, vector=(0.99, 0.01, 0.0, 0.0)),),
            ),
        ),
    )
    hits = GraphSearch(graph).meaning(owner, (1.0, 0.0, 0.0, 0.0), 6)
    assert [identity for identity, _ in hits] == ["mine"]
    assert hits[0][1] > 0.99


def test_topic_placement_reuses_owned_topic_past_foreign_exemplars(graph):
    owner, other = "topic-" + uuid.uuid4().hex, "other-" + uuid.uuid4().hex
    graph.put(other, tuple(note(other, f"foreign-{i}", pieces=()) for i in range(30)))
    foreign = GraphTopics(graph)
    for index in range(30):
        foreign.create(
            other,
            foreign.group_id(other, f"foreign-{index}"),
            f"foreign-{index}",
            (1.0, 0.0, 0.0, 0.0),
        )
    graph.put(owner, (note(owner, "exemplar", pieces=()), note(owner, "new", pieces=())))
    topics = GraphTopics(graph)
    own_group = topics.group_id(owner, "exemplar")
    topics.create(owner, own_group, "exemplar", (0.99, 0.01, 0.0, 0.0))
    # Fresh pass has no exemplar cache to hide an index-scoping error.
    result = GraphTopics(graph).place(owner, "new")
    assert result.joined == 1 and result.created == 0
    rows = graph.read(
        f"MATCH (n:{graph.settings.note_label} {{instance:$owner,note_id:'new'}})"
        f"-[:IN_GROUP]->(g:{graph.settings.group_label} {{group_type:'topic'}}) "
        "RETURN g.id AS id",
        owner=owner,
    )
    assert rows == [{"id": own_group}]


def test_owned_budget_growth_exhaustion_and_no_zero_budget_io():
    rows = [{"id": str(i), "mine": i >= 20} for i in range(22)]
    calls = []

    def fetch(limit):
        calls.append(limit)
        return rows[:limit]

    assert [row["id"] for row in owned_nearest(fetch, 2)] == ["20", "21"]
    assert calls == [2, 8, 32]
    calls.clear()
    assert owned_nearest(fetch, 0) == [] and calls == []
    assert [row["id"] for row in owned_nearest(fetch, 5)] == ["20", "21"]
