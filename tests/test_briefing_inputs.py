"""Real note-block packing proves bounded evidence and explicit unread source accounting."""

import copy
import json

import pytest
from pydantic import ValidationError

from anamnema.briefing_inputs import note_block, prepare_briefing
from anamnema.briefing_policy import BriefingPolicy, briefing_policy
from anamnema.setup_config import SetupError
from anamnema.speech_policy import speech_policy


def note(identity, **changes):
    return {
        "id": identity,
        "text": "A dated fixture observation.",
        "kind": "fact",
        "said_at": "2026-01-01",
        **changes,
    }


def test_attributed_clipped_sanitized_blocks_do_not_mutate_note_or_sources():
    policy = briefing_policy().model_copy(update={"source_rows": 1, "row_characters": 35})
    notes = [note("first", text="Synthetic password Demo1!xyz for this test.", wrong_open=True)]
    sources = {"first": ["owner: synthetic Demo1!xyz " + "context " * 20, "agent: extra context"]}
    before = copy.deepcopy((notes, sources))
    inputs = prepare_briefing(notes, sources, policy, speech_policy())
    rendered = inputs.batches[0].text
    assert inputs.batches[0].labels == {"n1": "first"}
    assert '"checked":"WRONG"' in rendered and '"said_at":"2026-01-01"' in rendered
    assert "Demo1!xyz" not in rendered and "[secret]" in rendered
    assert "[clipped]" in rendered and "agent: extra context" not in rendered
    assert (notes, sources) == before and not inputs.source_unavailable


def test_byte_bound_includes_separator_and_leaves_an_explicit_unread_tail():
    policy, speech = briefing_policy(), speech_policy()
    notes = [note("first", text="è" * 50), note("second", text="è" * 50), note("third")]
    weights = [
        len(note_block(n, f"n{i}", [], policy, speech).encode("utf-8"))
        for i, n in enumerate(notes, 1)
    ]
    policy = policy.model_copy(
        update={"batch_bytes": max(weights), "read_bytes": sum(weights[:2]) + 2}
    )
    inputs = prepare_briefing(notes, {}, policy, speech)
    assert len(inputs.batches) == 2 and inputs.unread == ("third",)
    assert inputs.source_unavailable == ("first", "second", "third") and inputs.broad
    assert all(len(batch.text.encode("utf-8")) <= policy.batch_bytes for batch in inputs.batches)
    smaller = policy.model_copy(update={"read_bytes": sum(weights[:2]) + 1})
    assert prepare_briefing(notes, {}, smaller, speech).unread == ("second", "third")


def test_empty_selection_and_single_oversize_note_are_not_fake_success():
    policy, speech = briefing_policy(), speech_policy()
    assert not prepare_briefing([], {}, policy, speech).batches
    with pytest.raises(SetupError, match="batch byte limit"):
        prepare_briefing([note("first", text="x" * policy.batch_bytes)], {}, policy, speech)
    with pytest.raises(SetupError, match="repeated"):
        prepare_briefing([note("first"), note("first")], {}, policy, speech)


def test_policy_validates_budget_order_and_estimate_includes_multibyte_margin():
    policy = briefing_policy()
    assert policy.estimate("è" * 30) == 22
    assert policy.ceiling == 3449 or policy.ceiling == 3450
    data = policy.model_dump(mode="json")
    for changes in (
        {"batch_bytes": policy.read_bytes + 1},
        {"stack_soft_share": 0.5, "stack_hard_share": 0.2},
        {"bytes_per_token": 0},
    ):
        with pytest.raises(ValidationError):
            BriefingPolicy.model_validate_json(json.dumps({**data, **changes}))
