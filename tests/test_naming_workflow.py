"""Actual archive/model/encoder/graph path for source-backed naming and stale judgement."""

import json

import pytest
from extraction_fixtures import completion
from test_graph import note
from test_memory_workflow import _cli_options
from test_memory_workflow import memory_workflow as memory_workflow

from anamnema.cli import main
from anamnema.extraction_config import extraction_config
from anamnema.graph_corrections import correct
from anamnema.graph_search import GraphSearch
from anamnema.llama_provider import LlamaProvider
from anamnema.memory_models import Correction, Outcome, Rewrite
from anamnema.naming_builder import NamingBuilder
from anamnema.naming_policy import naming_policy
from anamnema.naming_writer import NamingWriter
from anamnema.setup_config import Endpoint


@pytest.fixture
def naming_workflow(memory_workflow, model_service):
    owner, original, engine, _, _, _, _, _ = memory_workflow
    identity = engine.search(owner, "retention")["notes"][0]["id"]
    provider = LlamaProvider(
        Endpoint(host="127.0.0.1", port=model_service.port),
        extraction_config(),
        model_service.model_file,
    )
    builder = NamingBuilder(
        engine.store, NamingWriter(provider, naming_policy()), engine.encoder, engine.sources
    )
    return owner, original, engine, identity, model_service, builder


def answer(model, *, known=True):
    model.chat_replies.append(
        completion(
            json.dumps({"subjects": {"n1": {"known": known, "subject": "uniquesubjectfixture"}}})
        )
    )


def test_preview_then_apply_names_owned_verified_note(naming_workflow):
    owner, _, engine, identity, model, builder = naming_workflow
    before = engine.store.note(owner, identity)
    answer(model)
    preview = builder.build(owner, (identity,))
    assert preview["stored"] == 0 and preview["calls"][0]["proposed"][0]["known"]
    assert engine.store.note(owner, identity) == before
    answer(model)
    result = builder.build(owner, (identity,), apply=True)
    assert result["stored"] == 1 and result["calls"][0]["stale_or_absent"] == 0
    current = engine.store.note(owner, identity)
    assert current["text"] == before["text"] and current["subject_state"] == "named"
    assert GraphSearch(engine.store).words(owner, "uniquesubjectfixture", 10, "subject") == [
        identity
    ]


def test_rewrite_during_judgement_skips_stale_result(naming_workflow):
    owner, original, engine, identity, model, builder = naming_workflow
    answer(model)

    def rewrite():
        model.after_chat = None
        correct(
            engine.store,
            owner,
            Correction(
                id="naming-rewrite",
                note=identity,
                outcome=Outcome.PARTLY,
                reason="Compared the original",
                against=(original.identity,),
                rewrite="Retention is fourteen days.",
            ),
            Rewrite(
                query_vector=(0.0, 0.0, 1.0, 0.0),
                passage_vector=(0.0, 0.0, 1.0, 0.0),
                embedded_with="fixture",
            ),
        )

    model.after_chat = rewrite
    result = builder.build(owner, (identity,), apply=True)
    assert result["stored"] == 0 and result["calls"][0]["stale_or_absent"] == 1
    assert engine.store.note(owner, identity)["subject_state"] == "pending"


def test_missing_source_context_never_calls_model(naming_workflow):
    owner, _, engine, identity, model, builder = naming_workflow
    calls = len(model.calls)
    engine.sources.display = None
    result = builder.build(owner, (identity,), apply=True)
    assert result["stored"] == 0
    assert "verified attributed source" in result["calls"][0]["problems"][0]
    assert len(model.calls) == calls


def test_naming_cli_preview_needs_no_config_or_services(tmp_path, capsys):
    assert (
        main(
            [
                "name-notes",
                "--instance",
                "fixture",
                "--pending",
                "--config",
                str(tmp_path / "absent.json"),
            ]
        )
        == 0
    )
    result = json.loads(capsys.readouterr().out)
    assert result["subjects_judged"] is False and result["graph_written"] is False


def test_actual_naming_cli_names_and_then_resumes_without_generation(
    memory_workflow, model_service, tmp_path, capsys
):
    owner, _, engine, encoder, _, _, _, archive = memory_workflow
    options = _cli_options(owner, engine, encoder, archive, tmp_path, model_service.port)
    answer(model_service)
    arguments = [
        "name-notes",
        "--pending",
        "--apply",
        "--model-file",
        str(model_service.model_file),
        *options,
    ]
    assert main(arguments) == 0
    assert json.loads(capsys.readouterr().out)["stored"] == 1
    calls = len(model_service.calls)
    assert main(arguments) == 0
    assert json.loads(capsys.readouterr().out)["calls"] == []
    assert len(model_service.calls) == calls


def test_missing_source_note_does_not_block_healthy_batch_or_starve_fresh_notes(naming_workflow):
    owner, _, engine, identity, model, builder = naming_workflow
    bad = note(owner, "000-missing-source", source_ids=("gone",))
    engine.store.put(owner, (bad,))
    builder.writer.policy = builder.writer.policy.model_copy(update={"batch": 2})
    chosen = tuple(item["id"] for item in builder.graph.pending(owner, 2))
    assert chosen[0] == bad.id and identity in chosen
    answer(model)
    result = builder.build(owner, chosen, apply=True)
    assert result["stored"] == 1 and result["calls"][0]["left_pending"] == 1
    assert bad.id in result["calls"][0]["problems"][0]
    engine.store.put(owner, (note(owner, "fresh-after-failure"),))
    assert builder.graph.pending(owner, 1)[0]["id"] == "fresh-after-failure"


def test_missing_model_answer_rotates_behind_fresh_pending_notes(naming_workflow):
    owner, _, engine, identity, model, builder = naming_workflow
    model.chat_replies.append(completion('{"subjects":{}}'))
    result = builder.build(owner, (identity,), apply=True)
    assert result["calls"][0]["left_pending"] == 1
    engine.store.put(owner, (note(owner, "fresh-after-no-answer"),))
    assert builder.graph.pending(owner, 1)[0]["id"] == "fresh-after-no-answer"
