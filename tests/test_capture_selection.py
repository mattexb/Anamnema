"""Pinned event ranges survive append-only progress and refuse changed anchors."""

from dataclasses import replace

import pytest
from test_marked_capture import record

from anamnema.archive_models import Snapshot
from anamnema.capture_selection import pin_capture, select_capture
from anamnema.marked_capture import capture_policy, captured_marks
from anamnema.setup_config import SetupError
from anamnema.speech_policy import speech_policy


def data(tmp_path, *rows):
    return Snapshot(
        tmp_path / "session.jsonl",
        "synthetic",
        tuple(replace(row, source_line=index) for index, row in enumerate(rows, start=1)),
        0,
        0,
        False,
    )


def test_duplicate_boundary_identity_is_disambiguated_by_physical_position(tmp_path):
    prompt = record("Repeated native object.", type="user")
    old = record("fact: Old turn.", uuid="old")
    current = record("fact: Correct turn.", uuid="current")
    snapshot = data(tmp_path, prompt, old, prompt, current)
    policy, speech = capture_policy(), speech_policy()
    selected = pin_capture(snapshot, "fixture", "session-one", policy, speech, turn=True)
    assert selected.start is not None and selected.start.line == 3
    assert selected.start.id == snapshot.records[0].identity == snapshot.records[2].identity
    newer = data(
        tmp_path,
        prompt,
        old,
        prompt,
        current,
        record("Next turn.", type="user"),
        record("fact: Later identical-looking turn.", uuid="later"),
    )
    marks = captured_marks(
        select_capture(newer, selected, policy, speech), policy, speech, whole=True
    )
    assert [mark.source_id for mark in marks] == [current.identity]


@pytest.mark.parametrize("change", ["trim", "edit", "path", "owner"])
def test_changed_or_foreign_anchor_never_falls_back_to_a_newer_turn(tmp_path, change):
    prompt = record("Owner prompt.", type="user")
    marked = record("fact: Original note.")
    snapshot = data(tmp_path, prompt, marked)
    policy, speech = capture_policy(), speech_policy()
    selected = pin_capture(snapshot, "fixture", "session-one", policy, speech, turn=False)
    if change == "trim":
        changed = data(tmp_path, marked)
    elif change == "edit":
        changed = data(tmp_path, prompt, record("fact: Replaced source."))
    elif change == "path":
        changed = replace(snapshot, path=tmp_path / "other.jsonl")
    else:
        changed = snapshot
        selected = selected.model_copy(update={"instance": "another-assistant"})
    with pytest.raises(SetupError):
        select_capture(changed, selected, policy, speech)


def test_an_observed_empty_stretch_stays_empty_after_later_messages(tmp_path):
    policy, speech = capture_policy(), speech_policy()
    selected = pin_capture(data(tmp_path), "fixture", "session-one", policy, speech, turn=False)
    assert selected.empty
    later = data(tmp_path, record("fact: Later, not part of that event."))
    assert select_capture(later, selected, policy, speech).records == ()
