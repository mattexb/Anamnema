"""Actual archive/model/encoder/Neo4j adapters in ordered interrupted/resumed core jobs."""

import pytest
from test_memory_workflow import memory_workflow as memory_workflow

from anamnema.batch_archive import ArchiveWindow
from anamnema.batch_journal import BatchJournal
from anamnema.batch_models import batch_policy
from anamnema.batch_prepare import PreparedInputs, preparation_policy
from anamnema.batch_runner import BatchRunner
from anamnema.batch_window import WindowStages
from anamnema.extraction_config import extraction_config
from anamnema.graph_corrections import correct, history
from anamnema.llama_provider import LlamaProvider
from anamnema.memory_models import Correction, Outcome, Rewrite
from anamnema.setup_config import Endpoint, SetupError, load_config


@pytest.fixture
def pipeline(memory_workflow, model_service, tmp_path):
    owner, original, engine, _, loader, window, _, archive = memory_workflow
    inputs = PreparedInputs(tmp_path / "inputs", preparation_policy())
    path = inputs.save(window)
    archived = ArchiveWindow(inputs, owner, path.parent.name, archive, loader.sources.windows)
    provider = LlamaProvider(
        Endpoint(host="127.0.0.1", port=model_service.port),
        extraction_config(),
        model_service.model_file,
    )
    core = WindowStages(archived, loader.extraction, provider, loader, load_config())
    return (
        owner,
        original,
        engine,
        core,
        model_service,
        BatchJournal(tmp_path / "jobs", core.plan(), batch_policy()),
    )


@pytest.mark.parametrize("memory_workflow", ["unloaded-three"], indirect=True)
def test_three_real_stages_complete_then_resume_without_model_or_graph_rewrites(pipeline):
    owner, _, engine, core, model, journal = pipeline
    result = BatchRunner(journal, core.operations()).run()
    assert result.complete and result.stages[1].attempts[-1].metrics["model_calls"] > 0
    assert result.stages[2].attempts[-1].metrics["inserted"] > 0
    notes = engine.search(owner, "retention")["notes"]
    assert notes and engine.sources.open_note(engine.store, owner, notes[0]["id"])
    before = engine.store.note(owner, notes[0]["id"])
    calls = len(model.calls)
    assert core.archive.retire_payload(journal)
    assert BatchRunner(journal, core.operations()).run().complete
    assert len(model.calls) == calls
    assert engine.store.note(owner, notes[0]["id"]) == before
    assert core.archive.load()


@pytest.mark.parametrize("memory_workflow", ["unloaded-three"], indirect=True)
def test_lost_store_result_replays_known_notes_without_overwriting_intervening_correction(pipeline):
    owner, original, engine, core, model, journal = pipeline
    identities = []

    def committed_then_stop():
        core.store()
        found = engine.search(owner, "retention")["notes"][0]["id"]
        identities.append(found)
        correct(
            engine.store,
            owner,
            Correction(
                id="batch-correction",
                note=found,
                outcome=Outcome.PARTLY,
                reason="Original conversation checked",
                against=(original.identity,),
                rewrite="Retention is fourteen days.",
            ),
            Rewrite(
                query_vector=(1.0, 0.0, 0.0, 0.0),
                passage_vector=(1.0, 0.0, 0.0, 0.0),
                embedded_with="fixture",
            ),
        )
        raise RuntimeError("fixture crash after store commit")

    operations = core.operations()
    operations["store"] = committed_then_stop
    with pytest.raises(RuntimeError, match="after store commit"):
        BatchRunner(journal, operations).run()
    before = engine.store.note(owner, identities[0])
    calls = len(model.calls)
    result = BatchRunner(journal, core.operations()).run()
    assert result.complete and result.stages[2].attempts[0].category == "interrupted"
    assert result.stages[2].attempts[-1].metrics["inserted"] == 0
    assert engine.store.note(owner, identities[0]) == before and len(model.calls) == calls
    assert len(history(engine.store, owner, identities[0])) == 1


def test_changed_deployment_cannot_resume_same_job_identity(pipeline, tmp_path):
    _, _, _, core, _, journal = pipeline
    changed = WindowStages(
        core.archive,
        core.extraction,
        core.provider,
        core.loader,
        load_config().model_copy(update={"backend": "cpu"}),
    )
    assert changed.profile != core.profile
    with pytest.raises(SetupError, match="plan changed"):
        BatchJournal(journal.root, changed.plan(), batch_policy(), identity=journal.plan.identity)
