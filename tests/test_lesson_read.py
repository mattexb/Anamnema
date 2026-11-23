"""Real source-backed lesson votes, changed-evidence holdback and owner-scoped pairing."""

import uuid

import pytest
from test_graph import check
from test_lesson_graph import document, tokens
from test_lesson_graph import lessons as lessons

from anamnema.graph_corrections import correct
from anamnema.graph_store import MemoryConflict, MemoryDenied, query
from anamnema.lesson_graph import LessonPending
from anamnema.lesson_models import LessonAgainst
from anamnema.lesson_policy import lesson_policy
from anamnema.lesson_read import LessonReader
from anamnema.lesson_search import LessonSearch
from anamnema.memory_models import Outcome, Rewrite


def saved(owner, graph):
    proposed = document()
    proposed = proposed.model_copy(
        update={"episodes": (proposed.episodes[0].model_copy(update={"evidence": "ruling"}),)}
    )
    graph.write(owner, proposed, 0, tokens(owner, graph))
    return LessonReader(graph, lesson_policy())


def nominate(owner, graph, **changes):
    proposal = LessonAgainst(
        note="note-one",
        records=("source-one",),
        condition="same condition",
        reason="No narrower route fits this counterexample",
        judged_by="learner",
    )
    graph.add_against(
        owner,
        "lesson-one",
        proposal.model_copy(update=changes),
        graph.get(owner, "lesson-one", current=False)["revision"],
        tokens(owner, graph)["note-one"],
    )


def test_event_cannot_vote_on_both_sides_and_repeated_nomination_counts_once(lessons):
    owner, graph = lessons
    reader = saved(owner, graph)
    assert reader.read(owner, "lesson-one")["admitted"]
    for _ in range(2):
        nominate(owner, graph)
        state = reader.read(owner, "lesson-one")
        assert state["vote"] == {"support": 0, "against": 1, "stopped": True}
        assert len(state["against"]) == 1 and not state["admitted"]


def test_reputation_only_check_keeps_counter_but_rewrite_holds_both_sides(lessons):
    owner, graph = lessons
    reader = saved(owner, graph)
    nominate(owner, graph)
    assert correct(graph.store, owner, check("right-again"))
    checked = reader.read(owner, "lesson-one", historical=True)
    assert checked["vote"]["against"] == 1
    assert not checked["against"][0]["held_back"]
    assert correct(
        graph.store,
        owner,
        check("rewritten", outcome=Outcome.PARTLY, rewrite="Retention is seven days."),
        Rewrite(
            query_vector=(0.0, 1.0, 0.0, 0.0),
            passage_vector=(0.0, 1.0, 0.0, 0.0),
            embedded_with="synthetic-encoder",
        ),
    )
    changed = reader.read(owner, "lesson-one", historical=True)
    assert changed["vote"] == {"support": 0, "against": 0, "stopped": False}
    assert changed["episodes"][0]["held_back"] and changed["against"][0]["held_back"]
    with pytest.raises(LessonPending):
        reader.read(owner, "lesson-one")


def test_counterexample_refuses_foreign_forged_and_stale_inputs_atomically(lessons):
    owner, graph = lessons
    saved(owner, graph)
    with pytest.raises(MemoryDenied):
        graph.add_against(
            owner + "other",
            "lesson-one",
            LessonAgainst(
                note="note-one",
                records=("source-one",),
                condition="test",
                reason="test",
                judged_by="learner",
            ),
            1,
            tokens(owner, graph)["note-one"],
        )
    with pytest.raises(MemoryDenied):
        nominate(owner, graph, records=("invented",))
    assert graph.get(owner, "lesson-one")["against"] == []
    before = tokens(owner, graph)["note-one"]
    correct(graph.store, owner, check("source-changed"))
    with pytest.raises(MemoryConflict, match="source changed"):
        graph.add_against(
            owner,
            "lesson-one",
            LessonAgainst(
                note="note-one",
                records=("source-one",),
                condition="test",
                reason="test",
                judged_by="learner",
            ),
            graph.get(owner, "lesson-one", current=False)["revision"],
            before,
        )
    assert graph.get(owner, "lesson-one", current=False)["against"] == []


def test_deleted_counterexample_audit_survives_and_its_vote_is_held_back(lessons):
    owner, graph = lessons
    reader = saved(owner, graph)
    nominate(owner, graph)
    correct(graph.store, owner, check("delete-counter", outcome=Outcome.WRONG, delete=True))
    state = reader.read(owner, "lesson-one", historical=True)
    assert state["against"][0]["records"] == ["source-one"]
    assert state["vote"]["against"] == 0 and state["against"][0]["held_back"]
    assert graph.affected(owner, "delete-counter") == ["lesson-one"]


def test_configured_external_system_scope_is_not_a_private_project_catalogue(lessons):
    owner, graph = lessons
    saved(owner, graph)
    policy = lesson_policy().model_copy(
        update={"system_projects": {"external": ("fixture-project",)}}
    )
    reader = LessonReader(graph, policy)
    assert not reader.read(owner, "lesson-one", cwd="/work/unrelated")["admitted"]
    assert reader.read(owner, "lesson-one", cwd="/work/fixture-project")["admitted"]


def test_correction_during_evidence_read_refuses_mixed_current_snapshot(lessons, monkeypatch):
    owner, graph = lessons
    reader = saved(owner, graph)
    original = graph.store.note

    def changed(instance, identity):
        monkeypatch.setattr(graph.store, "note", original)
        value = original(instance, identity)
        correct(graph.store, owner, check("read-race", outcome=Outcome.OUTDATED))
        return value

    monkeypatch.setattr(graph.store, "note", changed)
    with pytest.raises(LessonPending, match="while evidence was read"):
        reader.read(owner, "lesson-one")


def test_lesson_candidates_and_pairs_widen_past_foreign_crowds(graph):
    owner = "lesson-search-" + uuid.uuid4().hex
    rows = [
        {
            "id": owner + ":" + key,
            "lesson_id": key,
            "instance": owner,
            "valid": True,
            "passage_vector": vector,
        }
        for key, vector in (("a", [1.0, 0.0, 0.0, 0.0]), ("b", [0.99, 0.01, 0.0, 0.0]))
    ]
    rows += [
        {
            "id": owner + f":filler-{i}",
            "lesson_id": f"filler-{i}",
            "instance": owner,
            "valid": True,
            "passage_vector": [0.0, 0.0, 1.0, 0.0],
        }
        for i in range(100)
    ]
    rows += [
        {
            "id": owner + f":foreign-{side}-{i}",
            "lesson_id": f"foreign-{side}-{i}",
            "instance": owner + "other",
            "valid": True,
            "passage_vector": vector,
        }
        for side, vector in (("a", [1.0, 0.0, 0.0, 0.0]), ("b", [0.99, 0.01, 0.0, 0.0]))
        for i in range(30)
    ]
    graph.write(
        lambda tx: query(
            tx, f"UNWIND $rows AS row CREATE (l:{graph.settings.lesson_label}) SET l=row", rows=rows
        )
    )
    search = LessonSearch(graph)
    assert "a" in search.candidates(owner, (1.0, 0.0, 0.0, 0.0), 6)
    assert search.candidates(owner + "absent", (1.0, 0.0, 0.0, 0.0), 6) == []
    pairs = search.pairs(owner, 1, 0.99)
    assert ("a", "b") in {(a, b) for a, b, _ in pairs}
    assert all(not a.startswith("foreign") and not b.startswith("foreign") for a, b, _ in pairs)
