"""Structural correctness and source attribution are not semantic truth judgments."""

import json

import pytest

from anamnema import notes_request
from anamnema.archive_models import Record
from anamnema.chunks import Chunk
from anamnema.notes_policy import notes_policy
from anamnema.notes_request import render, request_body, run_identity, schema
from anamnema.notes_response import source_notes, validate_response
from anamnema.setup_config import SetupError
from anamnema.speech import Row
from anamnema.speech_policy import speech_policy


def sample(kind="fact", rows=(0,), ruled_by=0, **extra):
    return json.dumps(
        {
            "concepts": [
                {
                    "text": "The project retains original conversation records.",
                    "rows": rows,
                    "kind": kind,
                    "ruled_by": ruled_by,
                    **extra,
                }
            ]
        }
    )


def chunk():
    owner = Record("a", "claude", "s", "conversation", "2026-01-01", 2, {"message": "keep sources"})
    agent = Record("a", "claude", "s", "conversation", "2026-01-02", 4, {"message": "done"})
    return Chunk(
        0,
        (
            Row(0, "owner", "Keep source records", owner),
            Row(2, "agent", "Source records kept", agent),
        ),
        "sample",
    )


def test_valid_decision_keeps_owner_and_exact_source_reference():
    policy = notes_policy()
    parsed = validate_response(sample("decision"), policy, speech_policy())
    note = source_notes(parsed, chunk(), policy)[0]
    assert note["problems"] == []
    assert note["owner_source"] == chunk().rows[0].source.identity
    assert note["source_ids"] == [chunk().rows[0].source.identity]
    assert note["candidate_evidence"] is False


def test_bad_citations_and_wrong_ruling_are_flagged_not_forged_as_support():
    policy = notes_policy()
    parsed = validate_response(sample("rule", (99,), 2), policy, speech_policy())
    note = source_notes(parsed, chunk(), policy)[0]
    assert note["candidate_evidence"] is True
    assert note["owner_source"] is None
    assert "row 99 not in chunk" in note["problems"]
    assert any("owner row" in problem for problem in note["problems"])
    assert any("candidate evidence" in problem for problem in note["problems"])
    assert note["source_ids"]


def test_missing_citations_are_explicit_and_empty_chunk_has_no_candidate():
    policy = notes_policy()
    response = validate_response(sample(rows=()), policy, speech_policy())
    note = source_notes(response, Chunk(0, (), ""), policy)[0]
    assert "no cited rows" in note["problems"]
    assert note["source_ids"] == [] and note["said_at"] is None


@pytest.mark.parametrize(
    "content",
    [
        "invalid",
        '{"concepts":[]}',
        sample("unknown"),
        sample(rows=(True,)),
        sample(ruled_by="0"),
        sample(unexpected=True),
        '{"concepts":[{"text":"short"}]}',
    ],
)
def test_local_schema_refuses_bad_answers_without_echoing_them(content):
    with pytest.raises(SetupError, match="violates the configured notes schema"):
        validate_response(content, notes_policy(), speech_policy())


def test_dynamic_schema_limits_match_local_validation():
    policy = notes_policy().model_copy(update={"max_notes": 1, "max_rows_per_note": 1})
    assert schema(policy)["properties"]["concepts"]["maxItems"] == 1
    too_many = json.loads(sample())
    too_many["concepts"] *= 2
    for content in (json.dumps(too_many), sample(rows=(0, 2))):
        with pytest.raises(SetupError):
            validate_response(content, policy, speech_policy())
    with pytest.raises(SetupError):
        validate_response(
            sample(), policy.model_copy(update={"note_characters": 40}), speech_policy()
        )


def test_prompt_request_and_identity_include_all_result_shaping_policy():
    policy, speech = notes_policy(), speech_policy()
    body = request_body(chunk(), policy, "model")
    assert body["messages"][0]["content"] == render(policy, "sample")
    assert body["response_format"]["json_schema"]["schema"] == schema(policy)
    assert not body["stream"] and body["max_tokens"] > 0
    model = {"alias": "model", "sha256": "a" * 64, "build": "fixture"}
    initial = run_identity(model, policy, speech)
    changed = policy.model_copy(update={"nearest_rows": policy.nearest_rows + 1})
    assert initial != run_identity(model, changed, speech)
    reordered = policy.model_copy(
        update={"model_options": dict(reversed(list(policy.model_options.items())))}
    )
    assert initial == run_identity(model, reordered, speech)
    assert initial != run_identity({**model, "build": "changed"}, policy, speech)


@pytest.mark.parametrize(
    "options", [{"api_key": "example"}, {"chat_template_kwargs": {"token": "example"}}]
)
def test_credentials_cannot_be_saved_in_request_policy(tmp_path, options):
    value = notes_policy().model_dump(mode="json")
    value["model_options"] = options
    path = tmp_path / "notes.json"
    path.write_text(json.dumps(value), encoding="utf-8")
    with pytest.raises(SetupError, match="cannot be read or validated"):
        notes_policy(path)


def test_prompt_snapshot_cannot_change_between_fingerprint_and_later_chunks(tmp_path, monkeypatch):
    template = notes_request.load_template()
    folder = tmp_path / "config"
    folder.mkdir()
    path = folder / "notes_prompt.txt"
    path.write_text(template, encoding="utf-8")
    notes_request.load_template.cache_clear()
    monkeypatch.setattr(notes_request, "files", lambda package: tmp_path)
    try:
        first = render(notes_policy(), "same slice")
        path.write_text(template + "\nUpdated prompt revision", encoding="utf-8")
        assert render(notes_policy(), "same slice") == first
        notes_request.load_template.cache_clear()
        assert render(notes_policy(), "same slice") != first
    finally:
        notes_request.load_template.cache_clear()
