"""Condense binds carries to accepted claims; nothing a kept claim does not carry is lost."""

import json
from typing import Any, cast

from anamnema.briefing_condense import CUT_FOR_SIZE, CondensedBrief, condense, room
from anamnema.briefing_models import BriefClaim, BriefLedger
from anamnema.briefing_policy import briefing_policy
from anamnema.native_judge import Judgement, NativeJudge
from anamnema.setup_config import SetupError


class Judge:
    def __init__(self, answer: dict[str, Any] | None) -> None:
        self.answer, self.prompts = answer, cast(list[str], [])

    def judge(self, prompt: str, schema: type[CondensedBrief]) -> Judgement[CondensedBrief]:
        self.prompts.append(prompt)
        if self.answer is None:
            raise SetupError("fixture worker failed")
        answer = schema.model_validate_json(json.dumps(self.answer))
        return Judgement(answer, ("fixture-model",), "identity")


def _ledger() -> BriefLedger:
    ledger = BriefLedger(("a", "b", "c"))
    ledger.claims = [
        BriefClaim(type="fact", text="A first.", notes=("a",)),
        BriefClaim(type="fact", text="A second, same note.", notes=("a",)),
        BriefClaim(type="lesson", text="C stands alone.", notes=("c",)),
    ]
    return ledger


def _run(answer: dict[str, Any] | None) -> tuple[Any, BriefLedger, Judge]:
    ledger, judge = _ledger(), Judge(answer)
    result = condense("subject", ledger, briefing_policy(), cast(NativeJudge, judge), "# header\n")
    return result, ledger, judge


def _claim(text: str, notes: list[str], carries: list[str]) -> dict[str, Any]:
    return {"type": "fact", "text": text, "notes": notes, "carries": carries}


def test_an_uncarried_draft_on_a_kept_note_goes_to_the_further_pages() -> None:
    result, ledger, judge = _run({"claims": [_claim("A, short.", ["n1"], ["d1"])]})
    assert [c.text for c in result.front] == ["A, short."] and result.front[0].notes == ("a",)
    assert [c.text for c in result.overflow] == ["A second, same note.", "C stands alone."]
    assert [(i.note, i.reason) for i in ledger.left_out] == [("c", CUT_FOR_SIZE)]
    assert "d1" in judge.prompts[0] and "Size cap" in judge.prompts[0]


def test_a_dropped_claim_twin_of_a_kept_one_carries_nothing() -> None:
    forged = _claim("A, short.", ["zz"], ["d2"])
    result, ledger, _judge = _run({"claims": [_claim("A, short.", ["n1"], ["d1"]), forged]})
    assert [c.text for c in result.overflow] == ["A second, same note.", "C stands alone."]
    assert "condensed claim dropped whole: empty or unknown citations" in ledger.problems


def test_unknown_carry_labels_carry_nothing_and_everything_carried_leaves_no_overflow() -> None:
    every = _claim("All of it.", ["n1", "n2"], ["d1", "d2", "d3", "d9", "x"])
    result, ledger, _judge = _run({"claims": [every]})
    assert result.overflow == [] and ledger.left_out == []


def test_a_failed_condense_keeps_the_draft_and_records_a_problem() -> None:
    result, ledger, _judge = _run(None)
    assert result is None and len(ledger.claims) == 3
    assert ledger.problems[0].startswith("condense failed; the draft first page is kept")


def test_room_is_never_negative() -> None:
    assert room(briefing_policy(), "x" * 10_000_000) == 0
