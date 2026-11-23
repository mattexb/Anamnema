"""Actual graph-backed retrieval ordering, floors, paging and visible reranker degradation."""

import uuid
from datetime import UTC, datetime

import pytest
from search_fixtures import SearchHTTP
from test_graph import check, note

from anamnema.graph_corrections import correct
from anamnema.memory_models import Outcome
from anamnema.retrieval import Retriever
from anamnema.retrieval_config import load_retrieval
from anamnema.search_services import CPUReranker, LocalJSON, load_search_services
from anamnema.setup_config import Endpoint, SetupError


def test_budget_pages_do_not_lose_notes(graph):
    instance = "pages-" + uuid.uuid4().hex
    proposals = tuple(
        note(instance, f"note-{i}", text=f"Retention fourteen days variant {i}.") for i in range(5)
    )
    graph.put(instance, proposals)
    settings = load_retrieval().model_copy(
        update={"rerank_enabled": False, "page": 2, "header_tokens": 40}
    )
    retrieval = Retriever(graph, settings)
    found = []
    for page in range(1, 7):
        result = retrieval.recall(instance, "fourteen", (1.0, 0.0, 0.0, 0.0), page=page)
        found.extend(row["id"] for row in result["notes"])
        if not result["more"]:
            break
    assert len(found) == len(set(found)) == 5
    assert set(found) == {proposal.id for proposal in proposals}


def test_wrong_floor_and_historical_cutoff(graph):
    instance = "floors-" + uuid.uuid4().hex
    graph.put(instance, (note(instance, said_at="2026-01-02T00:00:00Z"),))
    retrieval = Retriever(graph, load_retrieval().model_copy(update={"rerank_enabled": False}))
    assert retrieval.recall(instance, "fourteen", (1.0, 0.0, 0.0, 0.0))["notes"]
    past = retrieval.recall(
        instance, "fourteen", (1.0, 0.0, 0.0, 0.0), at=datetime(2026, 1, 1, tzinfo=UTC)
    )
    assert past["notes"] == []
    assert "archive itself was not searched" in past["answer"]
    correct(graph, instance, check(outcome=Outcome.WRONG))
    assert retrieval.recall(instance, "fourteen", (1.0, 0.0, 0.0, 0.0))["notes"] == []
    with pytest.raises(SetupError, match="timezone"):
        retrieval.recall(instance, "fourteen", (1.0, 0.0, 0.0, 0.0), at=datetime(2026, 1, 1))


def test_rerank_moves_low_scores_to_next_page_and_failure_keeps_order(graph):
    instance = "rerank-" + uuid.uuid4().hex
    graph.put(
        instance,
        tuple(note(instance, f"n-{i}", text=f"Retention fourteen variant {i}.") for i in range(4)),
    )
    with SearchHTTP() as server:
        policy = load_search_services()
        transport = LocalJSON(
            Endpoint(host="127.0.0.1", port=server.server.server_port),
            policy,
            "fixture-search-token",
            1.0,
        )
        settings = load_retrieval().model_copy(update={"rerank_keep_within": 0.1})
        retrieval = Retriever(graph, settings, CPUReranker(transport))
        first = retrieval.recall(instance, "fourteen", (1.0, 0.0, 0.0, 0.0))
        second = retrieval.recall(instance, "fourteen", (1.0, 0.0, 0.0, 0.0), page=2)
        assert first["rerank"] == {"status": "reranked", "kept": 1}
        assert len(first["notes"]) == 1
        assert len(second["notes"]) == 3
        assert len({n["id"] for n in first["notes"] + second["notes"]}) == 4
        server.override["/v1/rerank"] = {"results": []}
        failed = retrieval.recall(instance, "fourteen", (1.0, 0.0, 0.0, 0.0))
        baseline = Retriever(graph, settings.model_copy(update={"rerank_enabled": False})).recall(
            instance, "fourteen", (1.0, 0.0, 0.0, 0.0)
        )
        assert failed["rerank"]["status"] == "failed"
        assert failed["rerank"]["reason"]
        assert failed["notes"] == baseline["notes"]
