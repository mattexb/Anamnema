"""Actual stores and temporary print processes; not native model quality or login acceptance."""

import json
import sys
from pathlib import Path

import pytest
from test_graph import note
from test_memory_workflow import _cli_options
from test_memory_workflow import memory_workflow as memory_workflow
from test_native_judge import judge as judge
from test_native_judge import reply, success

from anamnema.briefing_builder import BriefBuilder
from anamnema.briefing_files import BriefFiles, brief_file_policy
from anamnema.briefing_inputs import note_block
from anamnema.briefing_policy import briefing_policy
from anamnema.briefing_select import brief_selector
from anamnema.cli import main
from anamnema.graph_corrections import correct
from anamnema.memory_models import Correction, Outcome, Rewrite
from anamnema.setup_config import SetupError
from anamnema.speech_policy import speech_policy


@pytest.fixture
def briefing(memory_workflow, judge, tmp_path):
    owner, _, engine, _, _, _, _, _ = memory_workflow
    identity = engine.search(owner, "retention")["notes"][0]["id"]
    files = BriefFiles(tmp_path / "briefings", brief_file_policy())
    builder = BriefBuilder(
        engine.store, engine.sources, judge, briefing_policy(), speech_policy(), files
    )
    return owner, identity, builder, judge


def answer():
    return {
        "claims": [
            {"type": "fact", "text": "Retention was seven days in the note.", "notes": ["n1"]}
        ],
        "suspect": [],
        "left_out": [],
    }


def test_briefing_builds_from_verified_owned_sources_and_commits_full_bundle(briefing):
    owner, identity, builder, worker = briefing
    before = builder.graph.note(owner, identity)
    reply(worker, success(structured_output=answer()))
    result = builder.build(owner, "fixture retention", (brief_selector("note:" + identity),))
    assert result["status"] == "ready" and result["metadata"]["read"] == [identity]
    manifest, pages = builder.files.read(owner, result["briefing"])
    assert manifest.metadata["models"] == ["fixture-model"]
    assert identity in pages[0].read_text(encoding="utf-8")
    assert builder.graph.note(owner, identity) == before
    assert result["metadata"]["unread"] == []


def test_failed_worker_preserves_failed_bundle_and_does_not_claim_reading(briefing):
    owner, identity, builder, worker = briefing
    reply(worker, success(is_error=True))
    result = builder.build(owner, "fixture", (brief_selector("note:" + identity),))
    assert result["status"] == "failed"
    assert result["metadata"]["read"] == [] and result["metadata"]["unread"] == [identity]
    assert "read batch failed" in result["metadata"]["problems"][0]
    builder.files.read(owner, result["briefing"])


def test_unknown_citation_is_not_successful_recall(briefing):
    owner, identity, builder, worker = briefing
    body = answer()
    body["claims"][0]["notes"] = ["n1", "forged"]
    reply(worker, success(structured_output=body))
    result = builder.build(owner, "fixture", (brief_selector("note:" + identity),))
    assert result["status"] == "partial" and result["metadata"]["problems"]
    _, pages = builder.files.read(owner, result["briefing"])
    assert "Retention was seven" not in pages[0].read_text(encoding="utf-8")


def test_foreign_selector_never_calls_worker_or_writes_files(briefing):
    owner, identity, builder, worker = briefing
    with pytest.raises(SetupError, match="no owned notes"):
        builder.build(owner + "-other", "fixture", (brief_selector("note:" + identity),))
    assert not (worker.config.cwd / "args.json").exists()
    assert not builder.files.root.exists()


def test_brief_cli_preview_and_missing_judge_never_open_infrastructure(tmp_path, capsys):
    request = tmp_path / "request.json"
    request.write_text(
        json.dumps({"subject": "fixture", "selectors": [{"kind": "note", "value": "n"}]}),
        encoding="utf-8",
    )
    root = tmp_path / "output"
    args = [
        "brief",
        "--instance",
        "fixture",
        "--request",
        str(request),
        "--briefing-root",
        str(root),
        "--config",
        str(tmp_path / "absent.json"),
    ]
    assert main(args) == 0
    assert json.loads(capsys.readouterr().out)["model_called"] is False
    assert main([*args, "--apply"]) == 2
    assert "judge-config" in json.loads(capsys.readouterr().err)["error"]
    assert not root.exists()


@pytest.mark.parametrize("status", ["ready", "failed"])
def test_actual_brief_cli_preserves_reports_and_failure_exit(
    memory_workflow, judge, tmp_path, capsys, status
):
    owner, _, engine, encoder, _, _, _, archive = memory_workflow
    identity = engine.search(owner, "retention")["notes"][0]["id"]
    options = _cli_options(owner, engine, encoder, archive, tmp_path)
    request = tmp_path / "request.json"
    request.write_text(
        json.dumps(
            {"subject": "fixture retention", "selectors": [{"kind": "note", "value": identity}]}
        ),
        encoding="utf-8",
    )
    deployment = tmp_path / "judge.json"
    deployment.write_text(judge.config.model_dump_json(), encoding="utf-8")
    reply(judge, success(structured_output=answer(), is_error=status == "failed"))
    result = main(
        [
            "brief",
            "--request",
            str(request),
            "--briefing-root",
            str(tmp_path / "output"),
            "--judge-config",
            str(deployment),
            "--apply",
            *options,
        ]
    )
    assert result == (0 if status == "ready" else 2)
    report = json.loads(capsys.readouterr().out)
    assert report["status"] == status
    assert all(Path(path).is_file() for path in report["pages"])


@pytest.mark.parametrize("mode", ["valid", "combine-fails", "combine-drops-citation"])
def test_real_multi_batch_transport_retains_failed_combine_or_reports_lost_citation(briefing, mode):
    owner, identity, builder, worker = briefing
    first = builder.graph.note(owner, identity)
    second = note(
        owner,
        "second",
        text="Another synthetic fact. " * 8,
        source_ids=tuple(first["source_ids"]),
        window=first["window"],
    )
    builder.graph.put(owner, (second,))
    notes = [first, builder.graph.note(owner, second.id)]
    size = max(
        len(
            note_block(
                item, "n1", builder._rows(owner, item), builder.policy, builder.speech
            ).encode()
        )
        for item in notes
    )
    builder.policy = builder.policy.model_copy(update={"batch_bytes": size})
    second_answer = {
        "claims": [{"type": "fact", "text": "Another synthetic fact.", "notes": ["n2"]}],
        "suspect": [],
        "left_out": [],
    }
    combined = {"claims": [*answer()["claims"], *second_answer["claims"]]}
    if mode == "combine-drops-citation":
        combined["claims"] = answer()["claims"]
    replies = [
        success(structured_output=answer()),
        success(structured_output=second_answer),
        success(structured_output=combined, is_error=mode == "combine-fails"),
    ]
    folder = worker.config.cwd
    (folder / "replies.json").write_text(json.dumps(replies), encoding="utf-8")
    worker.config.executable.write_text(
        f"#!{sys.executable}\n"
        + """import json,pathlib,sys
root=pathlib.Path.cwd()
sys.stdin.buffer.read()
counter=root/"counter"
index=int(counter.read_text(encoding="utf-8")) if counter.exists() else 0
counter.write_text(str(index+1),encoding="utf-8")
print(json.dumps(json.loads((root/"replies.json").read_text(encoding="utf-8"))[index]))
""",
        encoding="utf-8",
    )
    result = builder.build(
        owner, "fixture", (brief_selector("note:" + identity), brief_selector("note:second"))
    )
    assert (folder / "counter").read_text(encoding="utf-8") == "3"
    assert result["status"] == ("ready" if mode == "valid" else "partial")
    assert set(result["metadata"]["read"]) == {identity, "second"}
    _, pages = builder.files.read(owner, result["briefing"])
    content = "".join(path.read_text(encoding="utf-8") for path in pages)
    if mode != "combine-drops-citation":
        assert identity in content and "second" in content
    if mode == "combine-drops-citation":
        assert "citation lost in combining: second" in result["metadata"]["problems"]


@pytest.mark.parametrize("include_healthy", [True, False])
def test_unavailable_source_is_unread_but_does_not_destroy_inspectable_build(
    briefing, include_healthy
):
    owner, identity, builder, worker = briefing
    bad = note(owner, "missing-source", source_ids=("missing-archive-record",))
    builder.graph.put(owner, (bad,))
    selectors = (brief_selector("note:missing-source"),)
    if include_healthy:
        selectors += (brief_selector("note:" + identity),)
        reply(worker, success(structured_output=answer()))
    result = builder.build(owner, "fixture", selectors)
    assert result["status"] == ("partial" if include_healthy else "failed")
    assert result["metadata"]["unread"] == ["missing-source"]
    assert result["metadata"]["read"] == ([identity] if include_healthy else [])
    assert any(
        item.startswith("source unavailable: missing-source")
        for item in result["metadata"]["problems"]
    )
    builder.files.read(owner, result["briefing"])
    if not include_healthy:
        assert not (worker.config.cwd / "args.json").exists()


def test_unchanged_rebuild_uses_verified_snapshot_without_another_worker(briefing):
    owner, identity, builder, worker = briefing
    reply(worker, success(structured_output=answer()))
    first = builder.build(owner, "fixture", (brief_selector("note:" + identity),))
    (worker.config.cwd / "args.json").unlink()
    again = builder.rebuild(owner, first["briefing"])
    assert again["unchanged"] and again["briefing"] == first["briefing"]
    assert not (worker.config.cwd / "args.json").exists()


def test_correction_rebuild_keeps_old_bundle_and_uses_current_notes(briefing):
    owner, identity, builder, worker = briefing
    reply(worker, success(structured_output=answer()))
    first = builder.build(owner, "fixture", (brief_selector("note:" + identity),))
    note = builder.graph.note(owner, identity)
    correct(
        builder.graph,
        owner,
        Correction(
            id="briefing-correction",
            note=identity,
            outcome=Outcome.PARTLY,
            reason="Checked source",
            against=tuple(note["source_ids"]),
            rewrite="Retention is fourteen days.",
        ),
        Rewrite(
            query_vector=(1.0, 0.0, 0.0, 0.0),
            passage_vector=(1.0, 0.0, 0.0, 0.0),
            embedded_with="fixture",
        ),
    )
    updated = answer()
    updated["claims"][0]["text"] = "Retention is fourteen days according to its source."
    reply(worker, success(structured_output=updated))
    again = builder.rebuild(owner, first["briefing"])
    assert not again["unchanged"] and again["briefing"] != first["briefing"]
    assert again["rebuilt_from"] == first["briefing"]
    _, old = builder.files.read(owner, first["briefing"])
    _, new = builder.files.read(owner, again["briefing"])
    assert "seven" in old[0].read_text(encoding="utf-8")
    assert "fourteen" in new[0].read_text(encoding="utf-8")


def test_failed_build_is_retried_even_when_its_input_has_not_changed(briefing):
    owner, identity, builder, worker = briefing
    reply(worker, success(is_error=True))
    first = builder.build(owner, "fixture", (brief_selector("note:" + identity),))
    assert first["status"] == "failed"
    reply(worker, success(structured_output=answer()))
    result = builder.rebuild(owner, first["briefing"])
    assert result["status"] == "ready" and not result["unchanged"]


def test_an_over_size_first_page_is_condensed_and_uncarried_drafts_overflow(briefing, monkeypatch):
    owner, identity, builder, worker = briefing
    builder.policy = builder.policy.model_copy(
        update={"summary_tokens": 60, "summary_tolerance": 0}
    )
    long_claims = [
        {"type": "fact", "text": f"Retention detail number {i} " + "x" * 120, "notes": ["n1"]}
        for i in range(4)
    ]
    condensed = {
        "claims": [
            {"type": "fact", "text": "Retention: seven days.", "notes": ["n1"], "carries": ["d1"]}
        ]
    }
    actual = worker.judge
    replies = [
        success(structured_output={**answer(), "claims": long_claims}),
        success(structured_output=condensed),
    ]

    def staged(prompt, schema):
        reply(worker, replies.pop(0))
        return actual(prompt, schema)

    monkeypatch.setattr(worker, "judge", staged)
    result = builder.build(owner, "fixture retention", (brief_selector("note:" + identity),))
    assert result["condensed"] is True and result["metadata"]["condensed"] is True
    _, pages = builder.files.read(owner, result["briefing"])
    first = pages[0].read_text(encoding="utf-8")
    assert "Retention: seven days." in first and "detail number 0" not in first
    further = "".join(page.read_text(encoding="utf-8") for page in pages[1:])
    assert all(f"detail number {i}" in further for i in (1, 2, 3))
    assert replies == []
