"""Atomic real-Neo4j lesson history, source guards and correction invalidation."""

import json
import uuid

import pytest
from pydantic import ValidationError
from test_graph import check, note
from test_memory_workflow import memory_workflow as memory_workflow

from anamnema.graph_corrections import correct
from anamnema.graph_store import MemoryConflict, MemoryDenied
from anamnema.lesson_graph import LessonGraph, LessonPending
from anamnema.lesson_models import LessonDocument, LessonEpisode, LessonRoute
from anamnema.memory_models import Outcome


def document(**changes):
    values = {
        "id": "lesson-one",
        "statement": "Check retention against its source.",
        "mechanism": "Guessing loses the original decision.",
        "routes": (
            LessonRoute(
                condition="Before using retention",
                correction="Read the source",
                notes=("note-one",),
                exception=False,
            ),
        ),
        "severity": "normal",
        "status": "reviewed",
        "conflict": "",
        "model": "synthetic-judge",
        "embedding": (1.0, 0.0, 0.0, 0.0),
        "concepts": ("source-verification",),
        "episodes": (
            LessonEpisode(
                note="note-one",
                evidence="observed",
                event="event-one",
                event_reason="One original decision",
                records=("source-one",),
            ),
        ),
    }
    return LessonDocument(**{**values, **changes})


@pytest.fixture
def lessons(graph):
    owner = "lesson-" + uuid.uuid4().hex
    graph.put(owner, (note(owner),))
    return owner, LessonGraph(graph)


def tokens(owner, graph, ids=("note-one",)):
    return {item["note"]: item["token"] for item in graph.sources(owner, ids)}


@pytest.mark.parametrize(
    "field,value",
    [
        ("statement", "A different statement"),
        ("mechanism", "A different mechanism"),
        (
            "routes",
            (
                LessonRoute(
                    condition="Unless explicitly changed",
                    correction="Keep the record",
                    notes=("note-one",),
                    exception=True,
                ),
            ),
        ),
        ("severity", "high"),
        ("status", "conflict"),
        ("conflict", "An unresolved conflict"),
    ],
)
def test_each_content_field_versions_with_full_previous_snapshot(lessons, field, value):
    owner, graph = lessons
    original = document()
    assert graph.write(owner, original, 0, tokens(owner, graph)) == 1
    before = graph.get(owner, original.id)
    assert (
        graph.write(owner, document(**{field: value}), before["revision"], tokens(owner, graph))
        == 2
    )
    after = graph.get(owner, original.id)
    assert after["version"] == 2 and len(after["history"]) == 1
    previous = after["history"][0]
    assert previous["model"] == original.model
    assert previous["judged_at"] == before["judged_at"]
    assert set(previous) == {
        "version",
        "statement",
        "mechanism",
        "routes",
        "severity",
        "status",
        "conflict",
        "model",
        "judged_at",
    }


def test_metadata_only_rejudgement_and_replay_make_no_content_version(lessons):
    owner, graph = lessons
    assert graph.write(owner, document(), 0, tokens(owner, graph)) == 1
    for model in ("judge-b", "judge-b"):
        before = graph.get(owner, "lesson-one")
        assert (
            graph.write(owner, document(model=model), before["revision"], tokens(owner, graph)) == 1
        )
        after = graph.get(owner, "lesson-one")
        assert after["history"] == [] and after["model"] == model
        assert after["judged_at"] != before["judged_at"]


def test_foreign_and_fabricated_source_evidence_roll_back_everything(lessons):
    owner, graph = lessons
    own_tokens = tokens(owner, graph)
    with pytest.raises(MemoryDenied):
        graph.write(owner + "other", document(), 0, own_tokens)
    forged = document(
        episodes=(
            LessonEpisode(
                note="note-one",
                evidence="observed",
                event="event-one",
                event_reason="Forged reference",
                records=("invented",),
            ),
        )
    )
    with pytest.raises(MemoryDenied, match="actual sources"):
        graph.write(owner, forged, 0, own_tokens)
    with pytest.raises(MemoryDenied):
        graph.get(owner, "lesson-one", current=False)
    assert graph.write(owner, document(), 0, own_tokens) == 1
    with pytest.raises(MemoryDenied):
        graph.get(owner + "other", "lesson-one")


def test_corrected_sources_deny_stale_generation_and_retain_deleted_evidence(lessons):
    owner, graph = lessons
    original = document()
    stale_tokens = tokens(owner, graph)
    graph.write(owner, original, 0, stale_tokens)
    stale = graph.get(owner, original.id)
    assert correct(graph.store, owner, check("outdated-lesson", outcome=Outcome.OUTDATED))
    assert graph.affected(owner, "outdated-lesson") == [original.id]
    with pytest.raises(LessonPending):
        graph.get(owner, original.id)
    with pytest.raises(MemoryConflict, match="evidence changed"):
        graph.write(owner, original, stale["revision"], stale_tokens)
    assert graph.get(owner, original.id, current=False)["version"] == 1
    assert correct(graph.store, owner, check("delete-lesson", outcome=Outcome.WRONG, delete=True))
    assert graph.affected(owner, "delete-lesson") == [original.id]
    saved = graph.get(owner, original.id, current=False)
    assert saved["episodes"][0]["records"] == ["source-one"]
    assert not correct(
        graph.store, owner, check("delete-lesson", outcome=Outcome.WRONG, delete=True)
    )
    assert graph.get(owner, original.id, current=False)["revision"] == saved["revision"]


def test_stale_lesson_revision_rejects_another_writer_and_keeps_history(lessons):
    owner, graph = lessons
    own_tokens = tokens(owner, graph)
    graph.write(owner, document(), 0, own_tokens)
    revision = graph.get(owner, "lesson-one")["revision"]
    graph.write(owner, document(mechanism="A newer mechanism"), revision, own_tokens)
    before = graph.get(owner, "lesson-one")
    with pytest.raises(MemoryConflict, match="inputs changed"):
        graph.write(owner, document(mechanism="Stale mechanism"), revision, own_tokens)
    assert graph.get(owner, "lesson-one") == before


def test_bad_episode_after_good_episode_rolls_back_the_entire_lesson(lessons):
    owner, graph = lessons
    graph.store.put(owner, (note(owner, "second", source_ids=("source-two",)),))
    first = document().episodes[0]
    second = LessonEpisode(
        note="second",
        evidence="observed",
        event="event-two",
        event_reason="Second event",
        records=("invented",),
    )
    proposal = document(episodes=(first, second))
    with pytest.raises(MemoryDenied):
        graph.write(owner, proposal, 0, tokens(owner, graph, ("note-one", "second")))
    with pytest.raises(MemoryDenied):
        graph.get(owner, "lesson-one", current=False)
    assert "lesson_lock" not in graph.store.note(owner, "note-one")


def test_strict_routes_refuse_truthy_exception_flag_and_unknown_support():
    value = document().model_dump(mode="json")
    value["routes"][0]["exception"] = "false"
    with pytest.raises(ValidationError):
        LessonDocument.model_validate_json(json.dumps(value))
    with pytest.raises(ValidationError):
        document(
            routes=(
                LessonRoute(
                    condition="test", correction="test", notes=("unknown",), exception=False
                ),
            )
        )


def test_shared_engine_reports_pending_lesson_and_replayed_check_never_votes_twice(memory_workflow):
    owner, original, engine, _, _, _, _, _ = memory_workflow
    identity = engine.search(owner, "retention")["notes"][0]["id"]
    graph = LessonGraph(engine.store)
    proposal = document(
        routes=(),
        episodes=(
            LessonEpisode(
                note=identity,
                evidence="observed",
                event="original-event",
                event_reason="Original captured decision",
                records=tuple(engine.store.note(owner, identity)["source_ids"]),
            ),
        ),
    )
    graph.write(owner, proposal, 0, tokens(owner, graph, (identity,)))
    correction = check(
        "engine-lesson-check", note=identity, outcome=Outcome.OUTDATED, against=(original.identity,)
    )
    result = engine.check(owner, correction)
    assert result["applied"] and result["lessons"] == [
        {
            "lesson": proposal.id,
            "status": "pending",
            "rebuild_pending": True,
            "reason": "lesson model rebuilding is not configured",
        }
    ]
    revision = graph.get(owner, proposal.id, current=False)["revision"]
    with pytest.raises(LessonPending):
        graph.get(owner, proposal.id)
    repeated = engine.check(owner, correction)
    assert repeated["applied"] is False and repeated["lessons"] == result["lessons"]
    assert graph.get(owner, proposal.id, current=False)["revision"] == revision
