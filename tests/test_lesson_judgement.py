"""One real graph transaction per judgement, including history and unresolved notes."""

import argparse
import json

import pytest
from extraction_fixtures import completion
from fastapi.testclient import TestClient
from pydantic import ValidationError
from test_graph import note
from test_lesson_graph import document, tokens
from test_lesson_graph import lessons as lessons
from test_lesson_workflow import answer
from test_lesson_workflow import lesson_workflow as lesson_workflow
from test_lesson_writer import output
from test_memory_workflow import memory_workflow as memory_workflow

from anamnema.graph_store import MemoryConflict, MemoryDenied
from anamnema.lesson_cli import add_lesson_arguments
from anamnema.lesson_models import LessonEpisode, LessonJudgement, LessonWrite
from anamnema.lesson_operations import LessonRequest
from anamnema.memory_service import create_memory_app
from anamnema.service_config import load_service
from anamnema.setup_config import SetupError


class Stopped(RuntimeError):
    """An injected stop at a transaction boundary."""


def decision(owner, graph, *, pending_only=True):
    graph.store.put(owner, (note(owner, "unresolved", source_ids=("source-two",)),))
    return LessonJudgement(
        lessons=(
            LessonWrite(lesson=document(id="a", concepts=("first",)), revision=0),
            LessonWrite(
                lesson=document(id="b", mechanism="Second mechanism", concepts=("second",)),
                revision=0,
            ),
        ),
        unresolved=("unresolved",),
        source_tokens=tokens(owner, graph, ("note-one", "unresolved")),
        model="synthetic-judge",
        pending_only=pending_only,
    )


def assert_no_lessons(owner, graph):
    rows = graph.store.read(
        f"MATCH (l:{graph.store.settings.lesson_label} {{instance:$instance}}) "
        "RETURN count(l) AS n",
        instance=owner,
    )
    assert rows == [{"n": 0}]
    concepts = graph.store.read(
        f"MATCH (c:{graph.store.settings.concept_label} {{instance:$instance}}) "
        "RETURN count(c) AS n",
        instance=owner,
    )
    assert concepts == [{"n": 0}]


def test_stop_at_second_lesson_rolls_back_then_retry_commits_the_whole_judgement(
    lessons, monkeypatch
):
    owner, graph = lessons
    batch = decision(owner, graph)
    identities = ("note-one", "unresolved")
    before = [graph.store.note(owner, identity) for identity in identities]
    actual = graph._write_in
    calls = []

    def stop(tx, instance, item, sources):
        calls.append(item.lesson.id)
        if len(calls) == 2:
            raise Stopped("before second lesson")
        return actual(tx, instance, item, sources)

    monkeypatch.setattr(graph, "_write_in", stop)
    with pytest.raises(Stopped):
        graph.write_judgement(owner, batch)
    assert calls == ["a", "b"]
    assert [graph.store.note(owner, identity) for identity in identities] == before
    assert_no_lessons(owner, graph)
    assert graph.pending(owner, identities) == identities
    monkeypatch.setattr(graph, "_write_in", actual)
    assert graph.write_judgement(owner, batch) == [1, 1]
    assert graph.get(owner, "a")["episodes"][0]["records"] == ["source-one"]
    assert graph.get(owner, "b")["concepts"] == ["second"]
    assert graph.store.note(owner, "note-one")["lesson_state"] == "episode"
    assert graph.store.note(owner, "unresolved")["lesson_state"] == "unresolved"
    assert graph.pending(owner, identities) == ()


def test_stop_after_note_marks_rolls_back_everything(lessons, monkeypatch):
    owner, graph = lessons
    batch = decision(owner, graph)
    actual = graph._mark_judged

    def stop(tx, instance, judgement):
        actual(tx, instance, judgement)
        raise Stopped("after unresolved mark")

    monkeypatch.setattr(graph, "_mark_judged", stop)
    with pytest.raises(Stopped):
        graph.write_judgement(owner, batch)
    assert_no_lessons(owner, graph)
    assert graph.pending(owner, ("note-one", "unresolved")) == ("note-one", "unresolved")
    assert "lesson_lock" not in graph.store.note(owner, "note-one")


def test_late_revision_refusal_rolls_back_existing_content_history_and_concepts(lessons):
    owner, graph = lessons
    batch = decision(owner, graph, pending_only=False)
    graph.write(owner, document(id="a"), 0, tokens(owner, graph))
    previous = graph.get(owner, "a")
    batch = batch.model_copy(
        update={
            "lessons": (
                LessonWrite(
                    lesson=document(id="a", mechanism="Changed", concepts=("new",)),
                    revision=previous["revision"],
                ),
                LessonWrite(lesson=document(id="b"), revision=99),
            )
        }
    )
    with pytest.raises(MemoryConflict, match="inputs changed"):
        graph.write_judgement(owner, batch)
    assert graph.get(owner, "a") == previous
    with pytest.raises(MemoryDenied):
        graph.get(owner, "b", current=False)
    assert graph.pending(owner, ("unresolved",)) == ("unresolved",)


@pytest.mark.parametrize("bad", ["foreign", "missing", "stale"])
def test_all_inputs_are_checked_before_any_lesson_or_note_mark_commits(lessons, bad):
    owner, graph = lessons
    batch = decision(owner, graph)
    original = dict(batch.source_tokens)
    if bad == "stale":
        original["unresolved"] = "stale-token"
        batch = batch.model_copy(update={"source_tokens": original})
        error = MemoryConflict
    else:
        identity = "bad-source"
        if bad == "foreign":
            graph.store.put(owner + "other", (note(owner + "other", identity),))
        original.pop("unresolved")
        original[identity] = "unowned-token"
        batch = batch.model_copy(update={"unresolved": (identity,), "source_tokens": original})
        error = MemoryDenied
    with pytest.raises(error):
        graph.write_judgement(owner, batch)
    assert_no_lessons(owner, graph)
    assert graph.pending(owner, ("note-one", "unresolved")) == ("note-one", "unresolved")


def test_generation_that_loses_the_pending_race_cannot_commit_another_judgement(lessons):
    owner, graph = lessons
    batch = decision(owner, graph)
    graph.write_judgement(owner, batch)
    with pytest.raises(MemoryConflict, match="already judged"):
        graph.write_judgement(owner, batch)
    assert graph.get(owner, "a")["revision"] == 1
    assert graph.get(owner, "a")["history"] == []


@pytest.mark.parametrize("bound", ["max_items", "max_source_bytes"])
def test_whole_judgement_resource_bounds_fail_before_mutation(lessons, bound, monkeypatch):
    owner, graph = lessons
    batch = decision(owner, graph)
    monkeypatch.setattr(graph.store, "settings", graph.store.settings.model_copy(update={bound: 1}))
    with pytest.raises(SetupError, match="judgement exceeds"):
        graph.write_judgement(owner, batch)
    assert_no_lessons(owner, graph)


@pytest.mark.parametrize(
    "bad", ["duplicate", "missing-token", "extra-token", "mixed-model", "overlap", "boolean"]
)
def test_judgement_schema_refuses_incomplete_or_ambiguous_decisions(bad):
    item = LessonWrite(lesson=document(), revision=0)
    values = {
        "lessons": (item,),
        "source_tokens": {"note-one": "token"},
        "model": item.lesson.model,
    }
    if bad == "duplicate":
        values["lessons"] = (item, item)
    elif bad == "missing-token":
        values["source_tokens"] = {}
    elif bad == "extra-token":
        values["source_tokens"] = {"note-one": "token", "extra": "token"}
    elif bad == "mixed-model":
        values["model"] = "another-model"
    elif bad == "overlap":
        values["unresolved"] = ("note-one",)
    else:
        values["pending_only"] = "false"
    with pytest.raises(ValidationError):
        LessonJudgement(**values)


def test_later_fabricated_episode_rolls_back_earlier_valid_lesson(lessons):
    owner, graph = lessons
    batch = decision(owner, graph)
    bad = document(
        id="b",
        episodes=(
            LessonEpisode(
                note="note-one",
                evidence="ruling",
                event="e",
                event_reason="",
                records=("invented",),
            ),
        ),
    )
    batch = batch.model_copy(
        update={"lessons": (batch.lessons[0], LessonWrite(lesson=bad, revision=0))}
    )
    with pytest.raises(MemoryDenied, match="actual sources"):
        graph.write_judgement(owner, batch)
    assert_no_lessons(owner, graph)


def multi_answer(model):
    body = output()
    body["lessons"]["L2"] = {**body["lessons"]["L1"], "mechanism": "A second mechanism"}
    body["notes"]["n1"].append({**body["notes"]["n1"][0], "lesson": "L2"})
    model.chat_replies.append(completion(json.dumps(body)))


@pytest.mark.parametrize("boundary", ["second_lesson", "after_commit"])
def test_real_builder_interruption_and_replay_at_each_commit_boundary(
    lesson_workflow, monkeypatch, boundary
):
    owner, _, engine, root, model, builder = lesson_workflow
    graph = builder.graph
    multi_answer(model)
    method = "_write_in" if boundary == "second_lesson" else "write_judgement"
    actual = getattr(graph, method)
    calls = []

    def stop(*args, **kwargs):
        calls.append(True)
        if boundary == "second_lesson" and len(calls) == 2:
            raise Stopped("before second write")
        value = actual(*args, **kwargs)
        if boundary == "after_commit":
            raise Stopped("committed but reply not delivered")
        return value

    monkeypatch.setattr(graph, method, stop)
    with pytest.raises(Stopped):
        builder.build(owner, (root,))
    monkeypatch.setattr(graph, method, actual)
    if boundary == "second_lesson":
        assert graph.pending(owner, (root,)) == (root,)
        assert_no_lessons(owner, graph)
        multi_answer(model)
        assert len(builder.build(owner, (root,))["lessons"]) == 2
    assert graph.pending(owner, (root,)) == ()
    calls_before = len(model.calls)

    def no_encoding(*args, **kwargs):
        raise AssertionError("committed retry must not call the encoder")

    monkeypatch.setattr(engine.encoder, "encode", no_encoding)
    assert builder.build(owner, (root,))["already_judged"] == [root]
    assert len(model.calls) == calls_before
    count = engine.store.read(
        f"MATCH (l:{engine.store.settings.lesson_label} {{instance:$instance}}) "
        "RETURN count(l) AS n",
        instance=owner,
    )
    assert count == [{"n": 2}]


def test_http_unresolved_is_committed_and_explicit_rejudge_is_required(lesson_workflow):
    owner, _, engine, root, model, builder = lesson_workflow
    settings = load_service()
    client = TestClient(create_memory_app(engine, settings, {owner: "own-token"}))
    headers = {"Authorization": "Bearer own-token"}
    request = {"operation": "build", "notes": [root]}
    model.chat_replies.append(completion(json.dumps({"lessons": {}, "notes": {"n1": []}})))
    first = client.post(settings.lesson_path, json=request, headers=headers)
    assert first.status_code == 200 and first.json()["unresolved"] == [root]
    assert builder.graph.store.note(owner, root)["lesson_state"] == "unresolved"
    calls = len(model.calls)
    replay = client.post(settings.lesson_path, json=request, headers=headers)
    assert replay.status_code == 200 and replay.json()["already_judged"] == [root]
    assert len(model.calls) == calls
    answer(model)
    again = client.post(settings.lesson_path, json={**request, "rejudge": True}, headers=headers)
    assert again.status_code == 200 and len(again.json()["lessons"]) == 1
    assert builder.graph.store.note(owner, root)["lesson_state"] == "episode"


@pytest.mark.parametrize("value", ["false", "true", 0, 1, None, [], {}])
def test_rejudge_never_uses_json_truthiness(value):
    with pytest.raises(ValidationError):
        LessonRequest(operation="build", notes=("n",), rejudge=value)


def test_rejudge_is_build_only_and_cli_flag_is_explicit():
    with pytest.raises(ValidationError):
        LessonRequest(operation="read", lesson="l", rejudge=True)
    parser = argparse.ArgumentParser()
    add_lesson_arguments("build-lessons", parser)
    assert parser.parse_args(["--note", "n"]).rejudge is False
    assert parser.parse_args(["--note", "n", "--rejudge"]).rejudge is True
