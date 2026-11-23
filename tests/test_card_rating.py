"""Card priority is not recall relevance, and manual decisions must survive regeneration."""

import json
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from anamnema.card_policy import card_policy
from anamnema.card_rating import CardRanker
from anamnema.memory_config import load_memory

NOW = datetime(2026, 1, 1, tzinfo=UTC)


def note(identity="one", **changes):
    return {
        "id": identity,
        "text": identity,
        "window": identity,
        "said_at": NOW.isoformat(),
        "trust_positive": 3.0,
        "trust_negative": 1.0,
        "status": "active",
        "wrong_open": False,
        **changes,
    }


def ranker(**changes):
    return CardRanker(card_policy().model_copy(update=changes), load_memory().rating, NOW)


def test_value_repeats_retelling_age_and_inactive():
    subject = ranker()
    assert subject.note_value(note()) == 0.75
    assert subject.note_value(note(said_again=3)) == 2.25
    assert subject.note_value(note(retold_count=100)) == 0.75
    assert subject.note_value(note(said_at="2025-01-01T00:00:00+00:00")) == 0.375
    assert subject.note_value(note(said_at=None)) == 0.375
    assert subject.note_value(note(said_at="2027-01-01T00:00:00+00:00")) == 0.75
    assert subject.note_value(note(status="outdated", opens=100)) == 0.0
    assert subject.note_value(note(wrong_open=True)) == pytest.approx(0.49)


def test_line_sources_copies_and_bounded_open_bonus():
    subject = ranker()
    notes = [
        note("a", text="same text", window="w"),
        note("b", text="different", window="w", said_again=3, opens=100),
        note("c", text=" SAME   TEXT ", window="other"),
    ]
    assert subject.line_score(notes) == 2.75
    assert subject.line_score([note(status="outdated", opens=100)]) == 0.0
    assert subject.line_score([]) == 0.0


def test_pick_keeps_pins_moves_front_support_and_extra_capacity():
    subject = ranker(notes=2)
    notes = [note("newest"), note("highest", said_again=7), note("middle")]
    assert [n["id"] for n in subject.pick("example", notes)] == ["newest", "highest"]
    forced = [note("a", pinned=True), note("b", moved=True), note("c", on_front=True)]
    assert subject.pick("example", forced) == forced
    expanded = ranker(notes=1, expanded_entities=("example",), expanded_factor=2)
    assert len(expanded.pick("example", notes)) == 2


def test_manual_down_overrides_pin_and_ambiguous_move_is_unsettled():
    subject = ranker(front=2)
    notes = [note("p", pinned=True), note("i"), note("f", said_again=7)]
    lines = [
        {"text": "pin", "notes": ["p"], "front": False},
        {"text": "identity", "notes": ["i"], "front": True},
        {"text": "fact", "notes": ["f"], "front": False},
    ]
    down = {"text": "pin", "notes": ["p"], "front": False}
    result = subject.order(lines, notes, [down])
    assert [line["text"] for line in result.statements] == ["identity", "fact", "pin"]
    assert result.statements[-1]["front"] is False
    assert result.statements[-1]["moved"] is True
    assert "score" not in lines[0]
    ambiguous = {"text": "old wording", "notes": ["p", "i"], "front": True}
    assert subject.order(lines, notes, [ambiguous]).unsettled == [ambiguous]


def test_policy_bounds_are_strict():
    raw = card_policy().model_dump(mode="json")
    raw["who"] = raw["front"] + 1
    with pytest.raises(ValidationError):
        type(card_policy()).model_validate_json(json.dumps(raw))
