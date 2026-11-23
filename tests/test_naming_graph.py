"""Actual Neo4j transactions prove naming provenance, stale-result refusal and fallback."""

import uuid

import pytest
from test_graph import check, note

from anamnema.archive_models import digest
from anamnema.graph_corrections import correct
from anamnema.graph_search import GraphSearch
from anamnema.memory_models import Outcome, Rewrite
from anamnema.naming_graph import NamingGraph, SubjectWrite
from anamnema.setup_config import SetupError


@pytest.fixture
def naming(graph):
    owner = "naming-" + uuid.uuid4().hex
    graph.put(owner, (note(owner),))
    return owner, NamingGraph(graph)


def subject(**changes):
    return SubjectWrite(
        note="note-one",
        text="Retention is fourteen days.",
        source_ids=("source-one",),
        state="named",
        subject="fixture retention",
        model="fixture-model",
        version="fixture-v1",
        vector=(0.0, 0.0, 1.0, 0.0),
    ).model_copy(update=changes)


def vector(graph, owner):
    return graph.read(
        f"MATCH (n:{graph.settings.note_label} {{id:$id}}) RETURN n.subject_vector AS vector",
        id=digest([owner, "note-one"]),
    )[0]["vector"]


def test_named_subject_keeps_original_content_and_vectors_private(graph, naming):
    owner, subjects = naming
    before = graph.note(owner, "note-one")
    assert subjects.pending(owner, 10)[0]["id"] == "note-one"
    assert subjects.write(owner, (subject(),)) == 1
    current = graph.note(owner, "note-one")
    assert (
        current["text"] == before["text"] and current["trust_positive"] == before["trust_positive"]
    )
    assert current["subject"] == "fixture retention" and current["subject_state"] == "named"
    assert current["subject_vector"] is None
    assert vector(graph, owner) == [0.0, 0.0, 1.0, 0.0]
    assert subjects.pending(owner, 10) == ()
    assert subjects.write(owner + "-foreign", (subject(),)) == 0


def test_correction_between_read_and_write_retains_pending_new_meaning(graph, naming):
    owner, subjects = naming
    stale = subject()
    rewrite = Rewrite(
        query_vector=(0.0, 1.0, 0.0, 0.0),
        passage_vector=(0.0, 1.0, 0.0, 0.0),
        embedded_with="fixture",
    )
    assert correct(
        graph, owner, check(outcome=Outcome.PARTLY, rewrite="Retention is seven days."), rewrite
    )
    before = graph.note(owner, "note-one")
    assert subjects.write(owner, (stale,)) == 0
    assert graph.note(owner, "note-one") == before
    assert before["subject_state"] == "pending" and before.get("subject") is None
    assert vector(graph, owner) == list(rewrite.passage_vector)


def test_changed_provenance_skips_even_when_text_matches(graph, naming):
    owner, subjects = naming
    assert correct(graph, owner, check(records=("source-two",)))
    before = graph.note(owner, "note-one")
    assert subjects.write(owner, (subject(),)) == 0
    assert graph.note(owner, "note-one") == before


def test_unknown_and_legacy_seed_use_original_passage_without_rewriting(graph, naming):
    owner, subjects = naming
    assert subjects.write(owner, (subject(state="unknown", subject="", vector=None),)) == 1
    assert vector(graph, owner) == [1.0, 0.0, 0.0, 0.0]
    graph.write(
        lambda tx: tx.run(
            f"MATCH (n:{graph.settings.note_label} {{id:$id}}) "
            "REMOVE n.subject_vector,n.subject_state",
            id=digest([owner, "note-one"]),
        ).consume()
    )
    assert subjects.seed(owner + "-foreign") == 0
    assert subjects.seed(owner) == 1 and subjects.seed(owner) == 0
    assert graph.note(owner, "note-one")["subject_state"] == "pending"


def test_invalid_batch_refuses_before_any_subject_lands(graph, naming):
    owner, subjects = naming
    with pytest.raises(SetupError, match="repeats"):
        subjects.write(owner, (subject(), subject()))
    with pytest.raises(SetupError, match="dimensions"):
        subjects.write(owner, (subject(vector=(1.0,)),))
    assert graph.note(owner, "note-one")["subject_state"] == "pending"


def test_subject_search_adds_an_owned_representation_without_replacing_text(graph, naming):
    owner, subjects = naming
    assert subjects.write(owner, (subject(subject="uniquesubjectfixture"),)) == 1
    search = GraphSearch(graph)
    assert search.words(owner, "uniquesubjectfixture", 10) == []
    assert search.words(owner, "uniquesubjectfixture", 10, "subject") == ["note-one"]
    assert search.words(owner + "-foreign", "uniquesubjectfixture", 10, "subject") == []
    query = (0.0, 0.0, 1.0, 0.0)
    assert search.meaning(owner, query, 100, "subject")[0] == ("note-one", pytest.approx(1.0))
    assert search.meaning(owner + "-foreign", query, 100, "subject") == []
    assert search.similarities(owner, query, ["note-one"], "subject")["note-one"] == pytest.approx(
        1.0
    )
    assert search.similarities(owner, query, ["note-one"])["note-one"] < 1.0


def test_provenance_only_correction_invalidates_a_settled_subject(graph, naming):
    owner, subjects = naming
    assert subjects.write(owner, (subject(),)) == 1
    assert correct(graph, owner, check(records=("source-two",)))
    current = graph.note(owner, "note-one")
    assert current["subject_state"] == "pending" and current.get("subject") is None
    assert current.get("subject_records") is None
    assert vector(graph, owner) == [1.0, 0.0, 0.0, 0.0]
    assert subjects.pending(owner, 10)[0]["id"] == "note-one"


def test_subject_mode_keeps_short_notes_original_vector_without_later_pieces(graph, naming):
    owner, subjects = naming
    graph.put(owner, (note(owner, "short", pieces=()),))
    assert subjects.write(owner, (subject(note="short"),)) == 1
    search = GraphSearch(graph)
    original = (1.0, 0.0, 0.0, 0.0)
    scores = dict(search.meaning(owner, original, 100, "subject"))
    assert scores["short"] == pytest.approx(1.0)
    assert search.similarities(owner, original, ["short"], "subject")["short"] == pytest.approx(1.0)


@pytest.mark.parametrize("outcome", [Outcome.WRONG, Outcome.OUTDATED])
def test_pending_excludes_inactive_and_wrong_notes(graph, naming, outcome):
    owner, subjects = naming
    assert correct(graph, owner, check(outcome=outcome))
    assert subjects.pending(owner, 10) == ()
    assert subjects.write(owner, (subject(),)) == 0


def test_oversized_proposal_is_unwritten_not_a_batch_failure(graph, naming):
    owner, subjects = naming
    graph.put(owner, (note(owner, "healthy"),))
    oversized = subject(text="x" * (graph.settings.max_note_characters + 1))
    assert subjects.write(owner, (oversized, subject(note="healthy"))) == 1
    assert graph.note(owner, "note-one")["subject_state"] == "pending"
