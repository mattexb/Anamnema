"""Real model transport and disposable graph, with deliberately synthetic judgments."""

import json
import uuid

import pytest
from extraction_fixtures import completion
from test_graph import check, note

from anamnema.extraction_config import extraction_config
from anamnema.graph_corrections import correct
from anamnema.graph_store import MemoryConflict, MemoryDenied
from anamnema.graph_topics import GraphTopics
from anamnema.llama_provider import LlamaProvider
from anamnema.memory_models import Outcome
from anamnema.merge_judge import MergeJudge
from anamnema.merge_policy import merge_policy
from anamnema.repeat_merge import RepeatMerger
from anamnema.setup_config import Endpoint, SetupError


@pytest.fixture
def repeats(graph, model_service):
    policy = merge_policy().model_copy(update={"enabled": True})
    provider = LlamaProvider(
        Endpoint(host="127.0.0.1", port=model_service.port),
        extraction_config(),
        model_service.model_file,
    )
    owner = "repeat-fixture-" + uuid.uuid4().hex
    return owner, RepeatMerger(graph, policy, MergeJudge(provider, policy)), model_service


def answer(service, *verdicts):
    service.chat_replies.append(
        completion(
            json.dumps(
                {
                    "verdicts": [
                        {"pair": f"p{index}", "verdict": verdict}
                        for index, verdict in enumerate(verdicts, 1)
                    ]
                }
            )
        )
    )


@pytest.mark.parametrize("verdict,count", [("same_claim", 1), ("same_event", 0)])
def test_repeat_own_source_no_root_mutation_and_exactly_once(graph, repeats, verdict, count):
    owner, merger, service = repeats
    root = note(owner, "root", window="old-window", source_ids=("original-source",))
    graph.put(owner, (root,))
    before = graph.note(owner, "root")
    repeated = note(owner, "alias", window="new-window", source_ids=("repeated-source",))
    answer(service, verdict)
    result = merger.plan([repeated])
    assert result.kept == [] and result.judged == 1
    assert merger.graph.append(owner, result.occurrences) == 1
    assert merger.graph.append(owner, result.occurrences) == 0
    after = graph.note(owner, "root")
    assert after["said_again"] == count
    assert after["retold_count"] == int(verdict == "same_event")
    assert {
        key: value for key, value in after.items() if key not in ("said_again", "retold_count")
    } == {key: value for key, value in before.items() if key not in ("said_again", "retold_count")}
    occurrence = merger.graph.occurrences(owner, "root")[0]
    assert occurrence["source_ids"] == ["repeated-source"]
    assert occurrence["window"] == "new-window" and occurrence["verdict"] == verdict
    assert graph.known(owner, "alias")
    calls = len(service.calls)
    assert merger.plan([repeated]).replayed == 1
    assert len(service.calls) == calls
    assert merger.graph.occurrences(owner + "other", "root") == []


@pytest.mark.parametrize("verdict", ["changed", "different"])
def test_changed_and_different_remain_new_notes(graph, repeats, verdict):
    owner, merger, service = repeats
    graph.put(owner, (note(owner, "root"),))
    answer(service, verdict)
    result = merger.plan([note(owner, "new")])
    assert len(result.kept) == 1 and result.occurrences == []
    assert result.kept[0].changes_note == ("root" if verdict == "changed" else None)
    assert result.changed == int(verdict == "changed")


@pytest.mark.parametrize(
    "raw",
    [
        "not json",
        '{"verdicts":[]}',
        '{"verdicts":[{"pair":"p1","verdict":"same_claim"},{"pair":"p1","verdict":"different"}]}',
        '{"verdicts":[{"pair":"p999","verdict":"same_claim"}]}',
        '{"verdicts":[{"pair":"p1","verdict":"maybe"}]}',
    ],
)
def test_invalid_or_missing_judgment_is_visible_deferred_note(graph, repeats, raw):
    owner, merger, service = repeats
    graph.put(owner, (note(owner, "root"),))
    service.chat_replies.append(completion(raw))
    result = merger.plan([note(owner, "new")])
    assert result.deferred == 1 and result.occurrences == []
    assert result.kept[0].merge_deferred
    audit = graph.read(
        f"MATCH (d:{graph.settings.decision_label} {{instance:$owner}}) "
        "RETURN d.verdict AS verdict",
        owner=owner,
    )
    assert audit == [{"verdict": "deferred"}]


@pytest.mark.parametrize("outcome", [Outcome.WRONG, Outcome.OUTDATED])
def test_inactive_or_wrong_target_is_not_judged(graph, repeats, outcome):
    owner, merger, service = repeats
    graph.put(owner, (note(owner, "root"),))
    correct(graph, owner, check(note="root", outcome=outcome))
    result = merger.plan([note(owner, "new")])
    assert result.judged == 0 and len(result.kept) == 1
    assert service.calls == []


def test_in_run_chain_resolves_to_kept_root(graph, repeats):
    owner, merger, service = repeats
    merger.policy = merger.policy.model_copy(update={"candidates": 1})
    proposals = [
        note(owner, identity, passage_vector=vector, query_vector=vector)
        for identity, vector in (
            ("a", (1.0, 0.0, 0.0, 0.0)),
            ("b", (0.98, 0.2, 0.0, 0.0)),
            ("c", (0.9, 0.4, 0.0, 0.0)),
        )
    ]
    # Distinct telling keys even though the synthetic text is the same.
    proposals = [
        proposed.model_copy(update={"source_ids": (proposed.id,)}) for proposed in proposals
    ]
    answer(service, "same_claim", "same_event")
    result = merger.plan(proposals)
    assert [kept.id for kept in result.kept] == ["a"]
    assert [row["under"] for row in result.occurrences] == ["a", "a"]
    graph.put(owner, tuple(result.kept))
    assert merger.graph.append(owner, result.occurrences) == 2
    assert graph.note(owner, "a")["said_again"] == 1


def test_unmerge_retry_preserves_root_and_restores_own_note(graph, repeats, monkeypatch):
    owner, merger, service = repeats
    graph.put(owner, (note(owner, "root"),))
    answer(service, "same_claim")
    result = merger.plan([note(owner, "alias", source_ids=("repeat-source",))])
    merger.graph.append(owner, result.occurrences)
    place = GraphTopics.place

    def stopped(*args):
        raise OSError("synthetic stop before restored placement")

    monkeypatch.setattr(GraphTopics, "place", stopped)
    with pytest.raises(OSError):
        merger.graph.unmerge(owner, "alias", "Wrong merge")
    assert graph.note(owner, "alias")["placement_pending"]
    assert len(merger.graph.occurrences(owner, "root")) == 1
    monkeypatch.setattr(GraphTopics, "place", place)
    assert merger.graph.unmerge(owner, "alias", "Wrong merge")["undone"]
    assert merger.graph.occurrences(owner, "root") == []
    assert graph.note(owner, "root")["said_again"] == 0
    assert graph.note(owner, "alias")["source_ids"] == ["repeat-source"]
    assert not merger.graph.unmerge(owner, "alias", "Wrong merge")["undone"]
    with pytest.raises(MemoryDenied):
        merger.graph.unmerge(owner + "other", "alias", "Wrong merge")


def test_occurrence_conflict_and_different_run_are_refused(graph, repeats):
    owner, merger, service = repeats
    graph.put(owner, (note(owner, "root"), note(owner, "other")))
    answer(service, "same_claim", "same_claim")
    result = merger.plan([note(owner, "alias")])
    merger.graph.append(owner, result.occurrences)
    other_root = "root" if result.occurrences[0]["under"] == "other" else "other"
    changed = [{**result.occurrences[0], "under": other_root}]
    with pytest.raises(MemoryConflict):
        merger.graph.append(owner, changed)
    merger.graph.refuse_other_run(owner, "window-one", "run-one")
    with pytest.raises(MemoryConflict):
        merger.graph.refuse_other_run(owner, "window-one", "another-run")
    with pytest.raises(SetupError):
        RepeatMerger(graph, merge_policy(), merger.judge)
