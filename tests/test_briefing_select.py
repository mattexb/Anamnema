"""Real Neo4j enumeration proves owned selectors, deduplication and read priority."""

import uuid

import pytest
from pydantic import JsonValue, TypeAdapter
from test_graph import note
from test_lesson_graph import document, tokens

from anamnema.briefing_select import brief_selector, select_briefing
from anamnema.graph_store import query
from anamnema.graph_topics import GraphTopics
from anamnema.lesson_graph import LessonGraph
from anamnema.setup_config import SetupError


@pytest.fixture
def selection(graph):
    owner = "briefing-" + uuid.uuid4().hex
    graph.put(owner, (note(owner), note(owner, "second"), note(owner, "third")))
    topics = GraphTopics(graph)
    topics.place(owner, "note-one")
    topics.place(owner, "second")
    topics.place(owner, "third")
    lessons = LessonGraph(graph)
    lessons.write(owner, document(), 0, tokens(owner, lessons))
    return owner, topics.group_id(owner, "note-one")


def test_typed_selectors_are_owned_deduplicated_and_in_source_priority(graph, selection):
    owner, group = selection
    keys = (f"group:{group}", "note:third", "concept:source-verification", "note:absent")
    selected = select_briefing(graph, owner, tuple(map(brief_selector, keys)), 10)
    assert selected.resolved == keys[:3] and selected.unresolved == ("note:absent",)
    assert [item["id"] for item in selected.notes] == ["third", "note-one", "second"]
    assert selected.notes[1]["via"] == ["concept:source-verification", f"group:{group}"]
    assert all(item["instance"] == owner for item in selected.notes)
    assert all(
        item.get("query_vector") is None and item.get("passage_vector") is None
        for item in selected.notes
    )


def test_foreign_and_missing_selector_never_exposes_another_owner(graph, selection):
    owner, group = selection
    other = owner + "-other"
    keys = (f"group:{group}", "concept:source-verification", "note:note-one")
    selected = select_briefing(graph, other, tuple(map(brief_selector, keys)), 10)
    assert selected.resolved == () and selected.unresolved == keys and not selected.notes
    graph.put(other, (note(other),))
    selected = select_briefing(graph, other, tuple(map(brief_selector, keys)), 10)
    assert selected.resolved == ("note:note-one",)
    assert [item["instance"] for item in selected.notes] == [other]


def test_selection_bound_refuses_instead_of_truncating_without_writes(graph, selection):
    owner, group = selection
    before = [graph.note(owner, identity) for identity in ("note-one", "second", "third")]
    with pytest.raises(SetupError, match="note limit"):
        select_briefing(graph, owner, (brief_selector(f"group:{group}"),), 2)
    after = [graph.note(owner, identity) for identity in ("note-one", "second", "third")]
    assert after == before


def test_bad_and_duplicate_selectors_fail_before_generation(graph, selection):
    owner, _ = selection
    for value in ("router", "unknown:value", "group:", "note: "):
        with pytest.raises(SetupError):
            brief_selector(value)
    selector = brief_selector("note:note-one")
    with pytest.raises(SetupError, match="repeat"):
        select_briefing(graph, owner, (selector, selector), 10)


def test_new_note_temporal_properties_are_json_safe_in_both_readers(graph, selection):
    owner, _ = selection
    graph.write(
        lambda tx: query(
            tx,
            f"MATCH (n:{graph.settings.note_label} {{instance:$owner}}) "
            "SET n.entity_linked_at=datetime(),n.lesson_at=datetime(),n.future_date=date()",
            owner=owner,
        )
    )
    selected = select_briefing(graph, owner, (brief_selector("note:note-one"),), 10)
    validator = TypeAdapter(JsonValue)
    for item in (selected.notes[0], graph.note(owner, "note-one")):
        validator.validate_python(item)
        assert all(
            isinstance(item[key], str) for key in ("entity_linked_at", "lesson_at", "future_date")
        )
