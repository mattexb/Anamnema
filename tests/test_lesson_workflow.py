"""Actual source/archive/model/encoder/graph lesson generation and correction rebuild."""

import json

import pytest
from extraction_fixtures import completion
from fastapi.testclient import TestClient
from test_memory_workflow import _cli_options
from test_memory_workflow import memory_workflow as memory_workflow

from anamnema.cli import main
from anamnema.extraction_config import extraction_config
from anamnema.graph_corrections import correct, history
from anamnema.graph_store import MemoryConflict, MemoryDenied
from anamnema.lesson_builder import LessonBuilder
from anamnema.lesson_graph import LessonGraph, LessonPending
from anamnema.lesson_models import LessonDocument, LessonEpisode
from anamnema.lesson_policy import lesson_policy
from anamnema.lesson_writer import LessonWriter
from anamnema.llama_provider import LlamaProvider
from anamnema.memory_models import Correction, Outcome, Rewrite
from anamnema.memory_service import create_memory_app
from anamnema.service_config import load_service
from anamnema.setup_config import Endpoint, SetupError


def answer(model, label="L1", statement="Verify retention against the source.", **changes):
    content = {
        "statement": statement,
        "mechanism": "Guessing loses the original decision.",
        "routes": [
            {
                "condition": "Before using retention",
                "correction": "Read n1",
                "notes": ["n1"],
                "exception": False,
            }
        ],
        "concepts": [],
        "severity": "normal",
        "status": "reviewed",
        "conflict": "",
    }
    body = {
        "lessons": {label: {**content, **changes}},
        "notes": {
            "n1": [{"lesson": label, "evidence": "ruling", "event": "ev1", "event_reason": ""}]
        },
    }
    model.chat_replies.append(completion(json.dumps(body)))


@pytest.fixture
def lesson_workflow(memory_workflow, model_service):
    instance, original, engine, _, _, _, _, _ = memory_workflow
    root = engine.search(instance, "retention")["notes"][0]["id"]
    policy = lesson_policy()
    model_service.context = policy.answer_tokens + model_service.token_count
    provider = LlamaProvider(
        Endpoint(host="127.0.0.1", port=model_service.port),
        extraction_config(),
        model_service.model_file,
    )
    builder = LessonBuilder(
        LessonGraph(engine.store),
        LessonWriter(provider, policy),
        engine.encoder,
        engine.sources,
    )
    engine.lessons = builder
    return instance, original, engine, root, model_service, builder


def rewrite(original, root):
    return Correction(
        id="lesson-correction",
        note=root,
        outcome=Outcome.PARTLY,
        reason="Compared the captured source",
        against=(original.identity,),
        rewrite="Retention is fourteen days.",
    )


def test_actual_lesson_generation_and_correction_rebuild_with_idempotent_replay(lesson_workflow):
    owner, original, engine, root, model, builder = lesson_workflow
    answer(model)
    result = builder.build(owner, (root,))
    identity = result["lessons"][0]["lesson"]
    before = builder.graph.get(owner, identity)
    assert before["version"] == 1 and before["episodes"][0]["records"] == [original.identity]
    assert before["routes"][0]["notes"] == [root]
    answer(model, "E1", "Verify fourteen-day retention against the source.")
    checked = engine.check(owner, rewrite(original, root))
    assert checked["applied"] and checked["lessons"][0]["status"] == "success"
    after = builder.graph.get(owner, identity)
    assert after["version"] == 2 and after["history"][0]["statement"] == before["statement"]
    calls = len(model.calls)
    repeated = engine.check(owner, rewrite(original, root))
    assert not repeated["applied"] and repeated["lessons"][0]["rebuilt"] is False
    assert len(model.calls) == calls and len(history(engine.store, owner, root)) == 1


def test_one_judgement_cannot_commit_only_its_first_lesson(lesson_workflow, monkeypatch):
    from test_lesson_writer import output

    owner, _, engine, root, model, builder = lesson_workflow
    body = output()
    body["lessons"]["L2"] = {**body["lessons"]["L1"], "mechanism": "Another mechanism"}
    body["notes"]["n1"].append({**body["notes"]["n1"][0], "lesson": "L2"})
    model.chat_replies.append(completion(json.dumps(body)))
    actual = builder.graph._save
    calls = []

    def stop(*args, **kwargs):
        calls.append(True)
        if len(calls) == 2:
            raise RuntimeError("fixture stop before second lesson")
        return actual(*args, **kwargs)

    monkeypatch.setattr(builder.graph, "_save", stop)
    with pytest.raises(RuntimeError, match="before second lesson"):
        builder.build(owner, (root,))
    assert len(calls) == 2
    rows = engine.store.read(
        f"MATCH (l:{engine.store.settings.lesson_label} {{instance:$instance}}) "
        "RETURN count(l) AS n",
        instance=owner,
    )
    assert rows == [{"n": 0}]
    assert engine.store.note(owner, root).get("lesson_state") is None


def test_lesson_outage_is_pending_and_replay_retries_derivation_not_correction(lesson_workflow):
    owner, original, engine, root, model, builder = lesson_workflow
    answer(model)
    identity = builder.build(owner, (root,))["lessons"][0]["lesson"]
    model.overrides["/v1/chat/completions"] = (503, {"error": "synthetic model outage"})
    failed = engine.check(owner, rewrite(original, root))
    assert failed["applied"] and failed["lessons"][0]["rebuild_pending"]
    with pytest.raises(LessonPending):
        builder.graph.get(owner, identity)
    del model.overrides["/v1/chat/completions"]
    answer(model, "E1", "Verify fourteen days, not seven.")
    retried = engine.check(owner, rewrite(original, root))
    assert not retried["applied"] and retried["lessons"][0]["status"] == "success"
    assert builder.graph.get(owner, identity)["version"] == 2
    assert len(history(engine.store, owner, root)) == 1


def test_source_change_during_lesson_generation_refuses_stale_content(lesson_workflow):
    owner, original, engine, root, model, builder = lesson_workflow
    answer(model)
    identity = builder.build(owner, (root,))["lessons"][0]["lesson"]
    answer(model, "E1", "Stale old content")

    def changed():
        model.after_chat = None
        correct(
            engine.store,
            owner,
            rewrite(original, root),
            Rewrite(
                query_vector=(0.0, 0.0, 1.0, 0.0),
                passage_vector=(0.0, 0.0, 1.0, 0.0),
                embedded_with="synthetic-encoder",
            ),
        )

    model.after_chat = changed
    with pytest.raises(MemoryConflict, match="evidence changed"):
        builder.rebuild(owner, identity)
    assert builder.graph.get(owner, identity, current=False)["version"] == 1
    with pytest.raises(LessonPending):
        builder.graph.get(owner, identity)


def test_all_deleted_evidence_stays_pending_and_never_regenerates_old_lesson(lesson_workflow):
    owner, original, engine, root, model, builder = lesson_workflow
    answer(model)
    identity = builder.build(owner, (root,))["lessons"][0]["lesson"]
    calls = len(model.calls)
    result = engine.check(
        owner,
        Correction(
            id="delete-lesson-source",
            note=root,
            outcome=Outcome.WRONG,
            reason="Source does not support it",
            delete=True,
            against=(original.identity,),
        ),
    )
    assert result["lessons"][0]["status"] == "failed"
    assert "no eligible evidence" in result["lessons"][0]["reason"]
    assert len(model.calls) == calls
    assert builder.graph.get(owner, identity, current=False)["episodes"]
    with pytest.raises(LessonPending):
        builder.graph.get(owner, identity)


def test_foreign_notes_and_missing_source_context_never_reach_lesson_model(lesson_workflow):
    owner, _, engine, root, model, builder = lesson_workflow
    calls = len(model.calls)
    with pytest.raises(MemoryDenied):
        builder.build(owner + "other", (root,))
    engine.sources.display = None
    with pytest.raises(SetupError, match="verified attributed source"):
        builder.build(owner, (root,))
    assert len(model.calls) == calls


def test_single_event_reason_optional_but_shared_judgement_requires_reasons():
    from test_lesson_graph import document

    content = document().model_dump()
    episode = content["episodes"][0]
    content["episodes"] = (LessonEpisode(**{**episode, "event_reason": ""}),)
    assert LessonDocument(**content)
    content["episodes"] += (LessonEpisode(**{**episode, "note": "second", "event_reason": ""}),)
    with pytest.raises(ValueError, match="shared judged event"):
        LessonDocument(**content)


def test_http_lesson_generation_read_and_bearer_ownership(lesson_workflow):
    owner, _, engine, root, model, builder = lesson_workflow
    settings = load_service()
    client = TestClient(
        create_memory_app(engine, settings, {owner: "own-token", "other": "other-token"})
    )
    own = {"Authorization": "Bearer own-token"}
    other = {"Authorization": "Bearer other-token", "X-Assistant": owner}
    request = {"operation": "build", "notes": [root]}
    assert client.post(settings.lesson_path, json=request).status_code == 401
    assert client.post(settings.lesson_path, json=request, headers=other).status_code == 403
    assert not model.calls
    answer(model)
    built = client.post(settings.lesson_path, json=request, headers=own)
    assert built.status_code == 200, built.text
    identity = built.json()["lessons"][0]["lesson"]
    read = {"operation": "read", "lesson": identity}
    assert client.post(settings.lesson_path, json=read, headers=own).status_code == 200
    assert client.post(settings.lesson_path, json=read, headers=other).status_code == 403
    against = {
        "operation": "against",
        "lesson": identity,
        "note": root,
        "condition": "same action and conditions",
        "reason": "No narrower route fits",
    }
    assert client.post(settings.lesson_path, json=against, headers=other).status_code == 403
    nominated = client.post(settings.lesson_path, json=against, headers=own)
    assert nominated.status_code == 200, nominated.text
    assert nominated.json()["vote"] == {"support": 0, "against": 1, "stopped": True}
    assert nominated.json()["against"][0]["judged_by"] == owner
    assert (
        client.post(
            settings.lesson_path, json={**against, "judged_by": "other"}, headers=own
        ).status_code
        == 422
    )
    assert (
        client.post(
            settings.lesson_path, json={**read, "historical": "false"}, headers=own
        ).status_code
        == 422
    )
    engine.lessons = None
    assert (
        client.post(
            settings.lesson_path, json={"operation": "rebuild", "lesson": identity}, headers=own
        ).status_code
        == 503
    )
    assert builder.graph.get(owner, identity)["version"] == 1


def test_actual_cli_lesson_build_read_and_correction_refresh(
    memory_workflow, model_service, tmp_path, capsys
):
    owner, original, engine, encoder, _, _, _, archive = memory_workflow
    root = engine.search(owner, "retention")["notes"][0]["id"]
    policy = lesson_policy()
    model_service.context = policy.answer_tokens + model_service.token_count
    options = _cli_options(owner, engine, encoder, archive, tmp_path, model_service.port)
    answer(model_service)
    assert (
        main(
            [
                "build-lessons",
                "--note",
                root,
                "--lesson-model-file",
                str(model_service.model_file),
                *options,
            ]
        )
        == 0
    )
    identity = json.loads(capsys.readouterr().out)["lessons"][0]["lesson"]
    assert main(["lesson", "--lesson", identity, *options]) == 0
    assert json.loads(capsys.readouterr().out)["version"] == 1
    assert (
        main(
            [
                "lesson-against",
                "--lesson",
                identity,
                "--note",
                root,
                "--condition",
                "same conditions",
                "--reason",
                "No narrower route fits",
                *options,
            ]
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)["vote"]["stopped"] is True
    answer(model_service, "E1", "Verify fourteen days against the source.")
    path = tmp_path / "lesson-correction.json"
    path.write_text(rewrite(original, root).model_dump_json(), encoding="utf-8")
    assert (
        main(
            [
                "check",
                "--file",
                str(path),
                "--lesson-model-file",
                str(model_service.model_file),
                *options,
            ]
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)["lessons"][0]["status"] == "success"
    assert main(["lesson", "--lesson", identity, *options]) == 0
    updated = json.loads(capsys.readouterr().out)
    assert updated["version"] == 2 and updated["against"][0]["held_back"]
    assert updated["vote"] == {"support": 1, "against": 0, "stopped": False}
