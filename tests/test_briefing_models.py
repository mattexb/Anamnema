"""Citation accounting never turns omitted or unsupported evidence into complete recall."""

import pytest

from anamnema.briefing_models import (
    BriefAnswer,
    BriefClaim,
    BriefDisposition,
    BriefLedger,
    CombinedBrief,
)
from anamnema.setup_config import SetupError


def claim(*notes, text="A dated fixture observation.", kind="fact"):
    return BriefClaim(type=kind, text=text, notes=notes)


def answer(*claims, suspect=(), left_out=()):
    return BriefAnswer(claims=claims, suspect=suspect, left_out=left_out)


def test_unknown_citation_drops_the_whole_claim_not_just_the_bad_id():
    ledger = BriefLedger(("first", "second"))
    ledger.read(answer(claim("n1", "foreign"), claim("n2")), {"n1": "first", "n2": "second"})
    assert [c.notes for c in ledger.claims] == [("second",)]
    assert ledger.left_out[0].note == "first" and ledger.status == "partial"
    assert any("dropped whole" in problem for problem in ledger.problems)


def test_every_note_is_cited_omitted_or_suspect_and_none_mutates_memory():
    ledger = BriefLedger(("first", "second", "third"))
    ledger.read(
        answer(
            claim("n1", "n1", text="one\ncontinued"),
            suspect=(BriefDisposition(note="n2", reason="dated value may have changed"),),
            left_out=(BriefDisposition(note="n3", reason="different subject"),),
        ),
        {"n1": "first", "n2": "second", "n3": "third"},
    )
    assert ledger.status == "ready" and ledger.processed == set(ledger.selected)
    assert ledger.claims[0].notes == ("first",) and ledger.claims[0].text == "one continued"
    assert [item.note for item in ledger.suspect] == ["second"]
    assert [item.note for item in ledger.left_out] == ["third"]


def test_combine_cannot_launder_a_note_that_only_appeared_in_an_omission():
    ledger = BriefLedger(("first", "second"))
    labels = {"n1": "first", "n2": "second"}
    ledger.read(
        answer(
            claim("n1"),
            left_out=(BriefDisposition(note="n2", reason="different subject"),),
        ),
        labels,
    )
    ledger.combine(CombinedBrief(claims=(claim("n1", "n2"),)), labels)
    assert not ledger.claims and ledger.status == "partial"
    assert any("lost in combining" in item.reason for item in ledger.left_out)


def test_lost_combine_citation_is_visible_and_contradiction_is_preserved():
    ledger = BriefLedger(("first", "second", "third"))
    labels = {"n1": "first", "n2": "second", "n3": "third"}
    ledger.read(answer(claim("n1", "n2", kind="contradiction"), claim("n3")), labels)
    ledger.combine(CombinedBrief(claims=(claim("n1", "n2", kind="contradiction"),)), labels)
    assert ledger.claims[0].type == "contradiction"
    assert ledger.claims[0].notes == ("first", "second")
    assert ledger.left_out[-1].note == "third" and ledger.status == "partial"


def test_selected_but_never_read_is_not_ready_and_unknown_disposition_does_not_account():
    ledger = BriefLedger(("first", "second"))
    assert ledger.status == "failed"
    ledger.read(
        answer(left_out=(BriefDisposition(note="foreign", reason="not relevant"),)),
        {"n1": "first"},
    )
    assert ledger.processed == {"first"} and ledger.status == "partial"
    assert ledger.left_out[0].note == "first"


def test_bad_or_repeated_batch_leaves_existing_ledger_unchanged():
    ledger = BriefLedger(("first", "second"))
    ledger.read(answer(claim("n1")), {"n1": "first"})
    for labels in ({}, {"n1": "foreign"}, {"n1": "first"}, {"x": "second", "y": "second"}):
        before = repr(ledger)
        with pytest.raises(SetupError):
            ledger.read(answer(), labels)
        assert repr(ledger) == before
    for selected in (("same", "same"), ("",)):
        with pytest.raises(SetupError):
            BriefLedger(selected)
