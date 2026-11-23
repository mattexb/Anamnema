"""Exercise actual graph transactions/search with synthetic ownership and vectors."""

import threading
import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest
from pydantic import ValidationError

import anamnema.graph_store as graph_module
from anamnema.graph_corrections import correct, history
from anamnema.graph_search import GraphSearch, fuse, words_of
from anamnema.graph_store import MemoryConflict, MemoryDenied
from anamnema.memory_config import load_memory
from anamnema.memory_models import Correction, Facet, MemoryNote, Outcome, Piece, Rewrite
from anamnema.setup_config import SetupError


@pytest.fixture
def instance():
    return f"fixture-{uuid.uuid4().hex}"


def note(instance, identity="note-one", **changes):
    return MemoryNote(
        id=identity,
        instance=instance,
        text="Retention is fourteen days.",
        kind="decision",
        source_ids=("source-one",),
        session="session-one",
        substrate="claude",
        window="window-one",
        run="run-one",
        query_vector=(1.0, 0.0, 0.0, 0.0),
        passage_vector=(1.0, 0.0, 0.0, 0.0),
        embedded_with="synthetic-encoder",
        pieces=(Piece(offset=10, vector=(0.0, 1.0, 0.0, 0.0)),),
        facets=(Facet(kind="project", value="fixture-project"),),
    ).model_copy(update=changes)


def check(identity="check-one", **changes):
    return Correction(
        id=identity,
        note="note-one",
        outcome=Outcome.RIGHT,
        reason="Compared with original conversation.",
        against=("source-one",),
    ).model_copy(update=changes)


def test_insert_replay_conflict_and_scope(graph, instance):
    proposed = note(instance)
    assert graph.put(instance, (proposed,)) == 1
    assert graph.put(instance, (proposed,)) == 0
    assert graph.note(instance, proposed.id)["text"] == proposed.text
    with pytest.raises(MemoryDenied):
        graph.note("other-instance", proposed.id)
    with pytest.raises(MemoryDenied):
        graph.put("other-instance", (proposed,))
    with pytest.raises(MemoryConflict):
        graph.put(
            instance,
            (
                note(instance, "new-before-conflict"),
                proposed.model_copy(update={"text": "Different."}),
            ),
        )
    with pytest.raises(MemoryDenied):
        graph.note(instance, "new-before-conflict")
    with pytest.raises(SetupError):
        graph.put(instance, (proposed, proposed))


def test_search_meaning_words_identifiers_and_pieces(graph, instance):
    proposed = note(instance)
    graph.put(instance, (proposed,))
    search = GraphSearch(graph)
    assert search.meaning(instance, (1.0, 0.0, 0.0, 0.0), 100)[0][0] == proposed.id
    assert search.meaning(instance, (0.0, 1.0, 0.0, 0.0), 100)[0][1] == pytest.approx(1.0)
    assert search.words(instance, words_of("fourteen", 1000), 100) == [proposed.id]
    assert search.identifiers(instance, proposed.facets) == [proposed.id]
    assert search.similarities(instance, (0.0, 1.0, 0.0, 0.0), [proposed.id]) == {
        proposed.id: pytest.approx(1.0)
    }
    assert search.words("other", "fourteen", 100) == []
    assert search.meaning("other", (1.0, 0.0, 0.0, 0.0), 100) == []
    assert search.identifiers("other", proposed.facets) == []
    assert search.similarities("other", (1.0, 0.0, 0.0, 0.0), [proposed.id]) == {}


def test_correction_evidence_once_and_clear(graph, instance):
    graph.put(instance, (note(instance),))
    wrong = check(outcome=Outcome.WRONG, useful=False)
    assert correct(graph, instance, wrong)
    assert not correct(graph, instance, wrong)
    current = graph.note(instance, "note-one")
    assert current["trust_positive"] == 3.0
    assert current["trust_negative"] == 5.0
    assert current["not_useful"] == 1
    assert current["wrong_open"] is True
    with pytest.raises(MemoryConflict):
        correct(graph, instance, wrong.model_copy(update={"reason": "different"}))
    assert len(history(graph, instance, "note-one")) == 1
    assert correct(graph, instance, check("clear", outcome=Outcome.CLEARED))
    assert graph.note(instance, "note-one")["wrong_open"] is False


def test_rewrite_vectors_search_history_and_replay(graph, instance):
    original = note(instance)
    graph.put(instance, (original,))
    change = check(outcome=Outcome.PARTLY, rewrite="Retention is seven days.")
    vectors = Rewrite(
        query_vector=(0.0, 0.0, 1.0, 0.0),
        passage_vector=(0.0, 0.0, 1.0, 0.0),
        embedded_with="synthetic-encoder",
    )
    with pytest.raises(SetupError):
        correct(graph, instance, change)
    assert correct(graph, instance, change, vectors)
    assert not correct(graph, instance, change, vectors)
    current = graph.note(instance, "note-one")
    assert current["text"] == change.rewrite
    assert current["subject_state"] == "pending"
    assert graph.put(instance, (original,)) == 0
    assert graph.note(instance, "note-one")["text"] == change.rewrite
    assert history(graph, instance, "note-one")[0]["before_source_ids"] == ["source-one"]
    assert GraphSearch(graph).similarities(instance, (0.0, 0.0, 1.0, 0.0), [original.id])[
        original.id
    ] == pytest.approx(1.0)
    assert (
        graph.read(
            f"MATCH (p:{graph.settings.piece_label} {{instance:$instance}}) RETURN p",
            instance=instance,
        )
        == []
    )
    groups = graph.read(
        f"MATCH (g:{graph.settings.group_label} {{instance:$instance}}) "
        "RETURN g.card_valid AS valid",
        instance=instance,
    )
    assert groups == [{"valid": False}]


def test_delete_retains_check_and_rejects_other_owner(graph, instance):
    graph.put(instance, (note(instance),))
    deletion = check(outcome=Outcome.WRONG, delete=True)
    with pytest.raises(MemoryDenied):
        correct(graph, "other-instance", deletion)
    assert history(graph, "other-instance", "note-one") == []
    assert correct(graph, instance, deletion)
    assert not correct(graph, instance, deletion)
    with pytest.raises(MemoryDenied):
        graph.note(instance, "note-one")
    assert history(graph, instance, "note-one")[0]["deleted_note"] is True
    assert graph.put(instance, (note(instance),)) == 0
    with pytest.raises(MemoryDenied):
        graph.note(instance, "note-one")


@pytest.mark.parametrize("field", ["delete", "useful"])
@pytest.mark.parametrize("value", ['"false"', '"true"', '""', "0", "1", "[]", "{}"])
def test_malformed_flags_never_reach_graph(field, value):
    with pytest.raises(ValidationError):
        Correction.model_validate_json(
            '{"id":"c","note":"n","outcome":"wrong","reason":"r",'
            '"against":["source"],"' + field + '":' + value + "}"
        )


def test_query_preparation():
    assert words_of("retain + fourteen fourteen:(days)", 2) == "retain fourteen"
    assert fuse([["a", "b"], ["b"]], 60)["b"] > fuse([["a", "b"], ["b"]], 60)["a"]
    with pytest.raises(SetupError):
        fuse([], 0)


def test_schema_names_are_not_cypher_input():
    values = load_memory().model_dump(mode="json")
    values["note_label"] = "Unsafe) DETACH DELETE n //"
    with pytest.raises(ValidationError):
        type(load_memory()).model_validate(values)


def test_concurrent_duplicate_checks_count_once(graph, instance):
    graph.put(instance, (note(instance),))
    verdict = check(useful=True)
    with ThreadPoolExecutor(max_workers=4) as workers:
        results = list(workers.map(lambda _: correct(graph, instance, verdict), range(8)))
    assert sum(results) == 1
    current = graph.note(instance, "note-one")
    assert current["trust_positive"] == 7.0
    assert current["useful"] == 1
    assert len(history(graph, instance, "note-one")) == 1


def test_delete_during_insertion_replay_does_not_resurrect(graph, instance, monkeypatch):
    original = note(instance)
    graph.put(instance, (original,))
    entered, released = threading.Event(), threading.Event()
    actual = graph_module.query
    paused = False

    def interleaved(tx, statement, **parameters):
        nonlocal paused
        result = actual(tx, statement, **parameters)
        if "deleted_note:true" in statement and not paused:
            paused = True
            entered.set()
            assert released.wait(10)
        return result

    monkeypatch.setattr(graph_module, "query", interleaved)
    with ThreadPoolExecutor(max_workers=1) as workers:
        replay = workers.submit(graph.put, instance, (original,))
        try:
            assert entered.wait(10)
            assert correct(graph, instance, check(outcome=Outcome.WRONG, delete=True))
        finally:
            released.set()
        assert replay.result(timeout=10) == 0
    with pytest.raises(MemoryDenied):
        graph.note(instance, original.id)
