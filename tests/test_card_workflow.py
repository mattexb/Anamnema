"""Actual archive/encoder/model/graph card refresh behind both CLI and authenticated HTTP."""

import json

import pytest
from extraction_fixtures import completion
from fastapi.testclient import TestClient
from test_memory_workflow import _cli_options
from test_memory_workflow import memory_workflow as memory_workflow

from anamnema.card_builder import CardBuilder
from anamnema.card_graph import CardGraph, CardPending
from anamnema.card_policy import card_policy
from anamnema.card_writer import CardWriter
from anamnema.cli import main
from anamnema.extraction_config import extraction_config
from anamnema.graph_corrections import correct, history
from anamnema.graph_store import MemoryConflict
from anamnema.llama_provider import LlamaProvider
from anamnema.memory_models import Correction, Outcome, Rewrite
from anamnema.memory_service import create_memory_app
from anamnema.service_config import load_service
from anamnema.setup_config import Endpoint


def card_answer(server, text):
    server.chat_replies.append(
        completion(json.dumps({"statements": [{"text": text, "notes": ["n1"], "front": True}]}))
    )


@pytest.fixture
def linked_card(memory_workflow, model_service):
    instance, original, engine, server, _, _, _, _ = memory_workflow
    root = engine.search(instance, "retention")["notes"][0]["id"]
    provider = LlamaProvider(
        Endpoint(host="127.0.0.1", port=model_service.port),
        extraction_config(),
        model_service.model_file,
    )
    policy = card_policy()
    graph = CardGraph(engine.store, policy)
    graph.create(instance, "example", "project", "Explicit fixture entity")
    graph.link(instance, "example", root, "Explicit fixture link")
    builder = CardBuilder(graph, CardWriter(provider, policy))
    engine.cards, engine.card_graph = builder, graph
    card_answer(model_service, "Retention is seven days.")
    builder.rebuild(instance, "example")
    return instance, original, engine, server, root, model_service


def rewrite(original, root):
    return Correction(
        id="card-correction",
        note=root,
        outcome=Outcome.PARTLY,
        reason="Compared original source",
        against=(original.identity,),
        rewrite="Retention is fourteen days.",
    )


def test_correction_rebuilds_card_and_replay_does_not_vote_or_generate_again(linked_card):
    instance, original, engine, encoder, root, model = linked_card
    card_answer(model, "Retention is fourteen days.")
    result = engine.check(instance, rewrite(original, root))
    assert result["applied"] and result["cards"][0]["status"] == "success"
    card = engine.card_graph.card(instance, "example")
    assert card["statements"][0]["text"] == "Retention is fourteen days."
    generation, embeddings = len(model.calls), len(encoder.calls)
    assert not engine.check(instance, rewrite(original, root))["applied"]
    assert len(model.calls) == generation and len(encoder.calls) == embeddings
    assert len(history(engine.store, instance, root)) == 1


def test_outage_retains_correction_but_denies_stale_card_and_retry_repairs_it(linked_card):
    instance, original, engine, encoder, root, model = linked_card
    model.overrides["/v1/chat/completions"] = (503, {"error": "synthetic outage"})
    result = engine.check(instance, rewrite(original, root))
    assert result["applied"] is True
    assert result["cards"][0]["status"] == "failed"
    assert result["cards"][0]["rebuild_pending"] is True
    assert engine.store.note(instance, root)["text"] == "Retention is fourteen days."
    with pytest.raises(CardPending):
        engine.card_graph.card(instance, "example")
    embeddings = len(encoder.calls)
    del model.overrides["/v1/chat/completions"]
    card_answer(model, "Retention is fourteen days.")
    replay = engine.check(instance, rewrite(original, root))
    assert replay["applied"] is False and replay["cards"][0]["status"] == "success"
    assert len(encoder.calls) == embeddings
    assert engine.card_graph.card(instance, "example")["statements"][0]["text"] == (
        "Retention is fourteen days."
    )


def test_source_change_during_generation_refuses_stale_card_commit(linked_card):
    instance, original, engine, _, root, model = linked_card
    card_answer(model, "Stale seven-day output")

    def changed():
        model.after_chat = None
        assert correct(
            engine.store,
            instance,
            rewrite(original, root),
            Rewrite(
                query_vector=(0.0, 0.0, 1.0, 0.0),
                passage_vector=(0.0, 0.0, 1.0, 0.0),
                embedded_with="synthetic-encoder",
            ),
        )

    model.after_chat = changed
    with pytest.raises(MemoryConflict, match="inputs changed"):
        engine.cards.rebuild(instance, "example")
    with pytest.raises(CardPending):
        engine.card_graph.card(instance, "example")
    assert len(history(engine.store, instance, root)) == 1


def test_wrong_evidence_is_held_back_instead_of_regenerating_its_old_card(linked_card):
    instance, original, engine, _, root, model = linked_card
    calls = len(model.calls)
    result = engine.check(
        instance,
        Correction(
            id="wrong-card-evidence",
            note=root,
            outcome=Outcome.WRONG,
            against=(original.identity,),
            reason="Source does not support the old claim",
        ),
    )
    assert result["cards"][0]["held_back"] == [root]
    assert engine.card_graph.card(instance, "example")["statements"] == []
    assert len(model.calls) == calls


def test_http_card_and_entity_operations_hold_their_own_bearer_namespace(linked_card):
    instance, _, engine, _, root, model = linked_card
    settings = load_service()
    client = TestClient(
        create_memory_app(engine, settings, {instance: "own-token", "other": "other-token"})
    )
    own = {"Authorization": "Bearer own-token"}
    other = {"Authorization": "Bearer other-token", "X-Assistant": instance}
    assert client.post(settings.card_path, json={"name": "example"}).status_code == 401
    assert client.post(settings.card_path, headers=own, json={"name": "example"}).status_code == 200
    assert (
        client.post(settings.card_path, headers=other, json={"name": "example"}).status_code == 403
    )
    before = engine.card_graph.entity(instance, "example")["revision"]
    command = {"operation": "link", "name": "example", "note": root, "reason": "wrong owner"}
    assert client.post(settings.entity_path, headers=other, json=command).status_code == 403
    assert (
        client.post(
            settings.entity_path, headers=own, json={**command, "pinned": "false"}
        ).status_code
        == 422
    )
    assert (
        client.post(
            settings.card_path, headers=own, json={"name": "example", "rebuild": "false"}
        ).status_code
        == 422
    )
    assert engine.card_graph.entity(instance, "example")["revision"] == before
    engine.cards = None
    assert (
        client.post(
            settings.card_path, headers=own, json={"name": "example", "rebuild": True}
        ).status_code
        == 503
    )
    assert len(model.calls) == 1


def test_actual_cli_entity_card_build_and_correction_refresh(
    memory_workflow, model_service, tmp_path, capsys
):
    instance, original, engine, encoder, _, _, _, archive = memory_workflow
    root = engine.search(instance, "retention")["notes"][0]["id"]
    options = _cli_options(instance, engine, encoder, archive, tmp_path, model_service.port)
    assert (
        main(
            [
                "entities",
                "--create",
                "example",
                "--type",
                "project",
                "--reason",
                "Fixture",
                *options,
            ]
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)["entity"]["status"] == "confirmed"
    assert (
        main(
            [
                "entities",
                "--link",
                root,
                "--entity",
                "example",
                "--reason",
                "Cited source",
                "--pin",
                *options,
            ]
        )
        == 0
    )
    capsys.readouterr()
    card_answer(model_service, "Retention is seven days.")
    assert (
        main(
            [
                "build-cards",
                "--entity",
                "example",
                "--card-model-file",
                str(model_service.model_file),
                *options,
            ]
        )
        == 0
    )
    assert (
        json.loads(capsys.readouterr().out)["statements"][0]["text"] == "Retention is seven days."
    )
    assert main(["card", "--entity", "example", *options]) == 0
    capsys.readouterr()
    assert len(model_service.calls) == 1
    card_answer(model_service, "Retention is fourteen days.")
    path = tmp_path / "check.json"
    path.write_text(rewrite(original, root).model_dump_json(), encoding="utf-8")
    assert (
        main(
            [
                "check",
                "--file",
                str(path),
                "--card-model-file",
                str(model_service.model_file),
                *options,
            ]
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)["cards"][0]["status"] == "success"
    assert main(["card", "--entity", "example", *options]) == 0
    assert (
        json.loads(capsys.readouterr().out)["statements"][0]["text"]
        == "Retention is fourteen days."
    )
