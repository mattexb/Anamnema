"""Overflow is whole-claim recall, not accidental deletion of evidence from the briefing."""

from anamnema.briefing_models import BriefAnswer, BriefClaim, BriefLedger
from anamnema.briefing_policy import briefing_policy
from anamnema.briefing_render import render_briefing


def ledger(claims):
    labels = {f"n{i}": f"note-{i}" for i in range(1, len(claims) + 1)}
    result = BriefLedger(tuple(labels.values()))
    result.read(BriefAnswer(claims=tuple(claims), suspect=(), left_out=()), labels)
    return result


def test_overflow_keeps_each_whole_claim_and_its_citations_once():
    claims = [
        BriefClaim(type="fact", text=f"Claim{i}: " + "dated detail " * 20, notes=(f"n{i}",))
        for i in range(1, 9)
    ]
    state = ledger(claims)
    original = repr(state)
    policy = briefing_policy().model_copy(update={"summary_tokens": 350})
    rendered = render_briefing("fixture subject", state, policy)
    assert len(rendered.pages) > 1 and not rendered.oversized_pages
    full = "\n".join(rendered.pages)
    for i in range(1, 9):
        assert full.count(f"Claim{i}:") == 1 and full.count(f"[note-{i}]") == 1
    assert repr(state) == original


def test_contradictions_and_open_points_stay_on_page_one_even_if_over_cap():
    state = ledger(
        [
            BriefClaim(type="contradiction", text="Both sources disagree. " * 100, notes=("n1",)),
            BriefClaim(type="open", text="An unresolved scope. " * 100, notes=("n2",)),
            BriefClaim(type="low_relevance", text="Old detail.", notes=("n3",)),
        ]
    )
    rendered = render_briefing(
        "fixture subject", state, briefing_policy().model_copy(update={"summary_tokens": 200})
    )
    assert "Both sources disagree" in rendered.pages[0]
    assert "An unresolved scope" in rendered.pages[0]
    assert "Old detail" not in rendered.pages[0] and "Old detail" in rendered.pages[1]
    assert rendered.oversized_pages == (1,)


def test_empty_briefing_is_failed_and_review_names_never_read_notes():
    state = BriefLedger(("missing",))
    rendered = render_briefing("fixture\nsubject", state, briefing_policy())
    assert "Status: failed" in rendered.pages[0]
    assert "# Briefing: fixture subject" in rendered.pages[0]
    assert "## Never read\n- missing" in rendered.review
