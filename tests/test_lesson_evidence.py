"""Distinct source events, explicit judged equivalence, counterexamples and bounded routes."""

import pytest

from anamnema.lesson_evidence import Episode, event_classes, event_count, vote
from anamnema.lesson_gate import admitted, routes_for, system_here
from anamnema.setup_config import SetupError


def test_events_join_by_records_then_auditable_judgment_not_note_count():
    episodes = [
        Episode("a", "lesson", "failure", "one", ("r1", "r2")),
        Episode("b", "lesson", "ruling", "other-label", ("r2", "r3")),
        Episode("c", "lesson", "failure", "one", ("r4",), "Same real event"),
    ]
    found = event_classes(episodes, "lesson")
    assert len(found) == 1 and found[0].basis == "judged"
    assert found[0].notes == ("a", "b", "c") and found[0].reasons == ("Same real event",)
    assert event_count(episodes, "lesson", ("ruling", "failure")) == {
        "events": 1,
        "judged": 1,
        "ruling": 1,
        "failure": 0,
    }
    assert event_classes(episodes, "another") == []
    renamed = [
        Episode(
            item.note,
            item.lesson,
            item.evidence,
            "renamed-" + item.event,
            item.records,
            item.event_reason,
        )
        for item in episodes
    ]
    assert event_classes(renamed, "lesson") == found


def test_source_equivalence_and_distinct_events_remain_separate():
    episodes = [
        Episode("a", "l", "failure", "a", ("shared",)),
        Episode("b", "l", "failure", "b", ("shared",)),
        Episode("c", "l", "failure", "c", ("other",)),
    ]
    found = event_classes(episodes, "l")
    assert len(found) == 2 and all(event.basis == "source" for event in found)


def test_against_event_cannot_also_support_and_held_back_is_no_vote():
    lesson = {
        "id": "l",
        "episodes": [
            {"note": "a", "evidence": "failure", "event": "a", "records": ["r1"]},
            {"note": "b", "evidence": "failure", "event": "b", "records": ["r2"]},
            {"note": "private-id-only", "evidence": "failure", "event": "c", "records": ["note:c"]},
        ],
        "against": [{"note": "x", "records": ["r1"]}],
    }
    assert vote(lesson, "note:") == {"support": 1, "against": 1, "stopped": False}
    lesson["against"].append({"note": "y", "records": ["r3"]})
    assert vote(lesson, "note:") == {"support": 1, "against": 2, "stopped": True}
    lesson["against"][0]["held_back"] = True
    assert vote(lesson, "note:") == {"support": 2, "against": 1, "stopped": False}
    with pytest.raises(SetupError):
        vote(lesson, "")


def test_admission_and_external_scope_do_not_confuse_general_work_with_one_system():
    systems = {"outside": ("external-project",)}
    assert system_here("/workspace/external-project/src", systems) == "outside"
    assert not admitted({"events": 1}, [], None, 2, ("ruling",), systems)
    assert admitted({"ruling": 1}, [], None, 2, ("ruling",), systems)
    assert not admitted({"events": 2}, ["external-project"], None, 2, (), systems)
    assert admitted({"events": 2}, ["external-project"], "outside", 2, (), systems)
    assert admitted({"events": 2}, ["external-project", "general-work"], None, 2, (), systems)


def test_exception_routes_never_consume_the_ordinary_budget():
    routes = [
        {"condition": "first", "correction": "a", "exception": False},
        {"condition": "exception", "correction": "b", "exception": True},
        {"condition": "nearest", "correction": "c", "exception": False},
    ]

    def embed(texts):
        assert len(texts) == 2
        return [(0.0, 1.0), (1.0, 0.0)]

    assert routes_for(routes, (1.0, 0.0), 1, embed) == routes[1:]
    assert routes_for(routes, (1.0, 0.0), 0, embed) == [routes[1]]
    with pytest.raises(SetupError):
        routes_for(routes, (1.0, 0.0), -1, embed)
