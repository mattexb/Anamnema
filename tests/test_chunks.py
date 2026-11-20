"""Decision binding and bounded preparation reuse the reviewed extraction method."""

import json
from pathlib import Path

import pytest

from anamnema.archive_models import Record
from anamnema.binding import bind_answer, bindings, describe
from anamnema.chunks import chunks, estimated_tokens
from anamnema.notes_policy import notes_policy
from anamnema.setup_config import SetupError
from anamnema.speech import Row


def row(number, speaker, text):
    source = Record("a", "claude", "s", "conversation", "2026-01-01", number + 1, {"message": text})
    return Row(number, speaker, text, source)


def test_only_explicit_items_are_bound_not_silence():
    policy = notes_policy()
    proposal = row(0, "agent", "1. first\n2. second\n3. third")
    answer = row(1, "owner", "I disagree on 2")
    found = bindings((proposal, answer), policy)
    assert found[1].answers == 0
    assert found[1].rejected == (2,)
    assert found[1].accepted == ()
    assert found[1].unresolved == (1, 3)
    text = describe(found[1], policy.names)
    assert "binding by code, not Owner's words" in text
    assert "not addressed by name: 1, 3" in text


def test_rejection_wins_when_same_item_named_both_ways():
    proposal = row(0, "agent", "1. first\n2. second")
    found = bind_answer("agree on 1 and 2; disagree on 2", proposal, notes_policy())
    assert found.accepted == (1,)
    assert found.rejected == (2,)
    assert found.unresolved == ()


def test_unrelated_row_breaks_a_proposal_and_only_first_owner_reply_binds():
    policy = notes_policy()
    proposal = row(0, "agent", "1. first\n2. second")
    answer = row(2, "owner", "agree on 2")
    assert bindings((proposal, row(1, "agent", "unrelated"), answer), policy) == {}
    found = bindings((proposal, row(1, "owner", "no item named"), answer), policy)
    assert list(found) == [1]
    assert found[1].unresolved == (1, 2)


def test_chunks_keep_binding_labels_and_exact_sources():
    policy = notes_policy()
    rows = (row(0, "agent", "1. first\n2. second"), row(1, "owner", "agree on 1"))
    parts = chunks(rows, bindings(rows, policy), policy)
    assert len(parts) == 1
    assert parts[0].rows == rows
    assert "[#0 Assistant]" in parts[0].text
    assert "[#1 Owner]" in parts[0].text
    assert "binding by code" in parts[0].text
    assert "accepted items: 1" in parts[0].text


@pytest.mark.parametrize("limit", ["chunk_characters", "chunk_tokens"])
def test_multiple_chunks_obey_each_bound_without_dropping_rows(limit):
    policy = notes_policy().model_copy(update={limit: 45})
    rows = tuple(row(n, "owner", "abcdefgh " * 2) for n in range(6))
    parts = chunks(rows, {}, policy)
    assert len(parts) > 1
    assert tuple(r for part in parts for r in part.rows) == rows
    assert [part.index for part in parts] == list(range(len(parts)))
    assert all(len(part.text) <= policy.chunk_characters for part in parts)
    assert all(estimated_tokens(part.text, policy) <= policy.chunk_tokens for part in parts)


@pytest.mark.parametrize("text", ["x" * 100, "<0123456789>" * 10])
def test_oversized_single_row_fails_instead_of_truncating_or_exceeding_limits(text):
    policy = notes_policy().model_copy(update={"chunk_characters": 80, "chunk_tokens": 30})
    with pytest.raises(SetupError, match="exceeds configured chunk bounds"):
        chunks((row(0, "owner", text),), {}, policy)


def test_empty_rows_are_an_empty_successful_preparation():
    assert chunks((), {}, notes_policy()) == ()


@pytest.mark.parametrize(
    "change",
    [
        {"numbered_pattern": "["},
        {"numbered_pattern": "no group"},
        {"minimum_note_characters": 1000},
        {"model_options": {"messages": []}},
        {"model_options": {"temperature": float("nan")}},
        {"nearest_rows": 0},
    ],
)
def test_invalid_policy_fails_without_echoing_user_values(tmp_path: Path, change):
    value = notes_policy().model_dump(mode="json")
    value.update(change)
    path = tmp_path / "notes.json"
    path.write_text(json.dumps(value), encoding="utf-8")
    with pytest.raises(SetupError, match="cannot be read or validated"):
        notes_policy(path)


def test_noninteger_capture_fails_visibly_instead_of_crashing():
    policy = notes_policy().model_copy(update={"numbered_pattern": "(not-a-number)"})
    with pytest.raises(SetupError, match="integer item numbers"):
        bind_answer("agree on 1", row(0, "agent", "not-a-number"), policy)
