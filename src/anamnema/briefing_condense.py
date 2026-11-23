"""One denser rewrite of an over-size first page, port of KinTools 1cac580 ``_condense``.

The condensed claims name the draft claims they carry (``d1``, ``d2``, ...). A draft claim
that no accepted condensed claim carries goes to the further pages, never away, even when a
kept claim cites the same note. ``carries`` count only for a claim that passed validation: a
dropped claim written with a kept claim's text carries nothing (Elian, 2026-11-22). A claim
citing a note the draft did not cite is dropped whole. A failed condense is a recorded problem
and the draft stays as it was.
"""

import math
from dataclasses import dataclass

from pydantic import JsonValue, TypeAdapter

from anamnema.archive_models import canonical
from anamnema.briefing_models import BriefClaim, BriefDisposition, BriefLedger, ClaimKind
from anamnema.briefing_policy import BriefingPolicy
from anamnema.native_judge import NativeJudge
from anamnema.setup_config import Nonempty, SetupError, StrictModel

JSON_VALUE = TypeAdapter(JsonValue)
CUT_FOR_SIZE = "cut from the first page for size; its draft claim is on a further page"


class CondensedClaim(StrictModel):
    type: ClaimKind
    text: Nonempty
    notes: tuple[Nonempty, ...]
    carries: tuple[str, ...]


class CondensedBrief(StrictModel):
    claims: tuple[CondensedClaim, ...]


@dataclass(frozen=True)
class Condensed:
    """The new first page and the draft claims it does not carry, in draft order."""

    front: list[BriefClaim]
    overflow: list[BriefClaim]
    models: tuple[str, ...]


def room(policy: BriefingPolicy, header: str) -> int:
    """Characters left for claims under the page target, counted the way the estimate counts."""
    allowed = policy.summary_tokens * policy.bytes_per_token / (1 + policy.size_margin)
    return max(0, math.floor(allowed) - len(header.encode("utf-8")))


def condense(
    subject: str, ledger: BriefLedger, policy: BriefingPolicy, judge: NativeJudge, header: str
) -> Condensed | None:
    """Ask once; validate citations against the draft; bind carries to accepted claims only."""
    drafts = list(ledger.claims)
    cited = {identity for claim in drafts for identity in claim.notes}
    labels = {identity: f"n{index}" for index, identity in enumerate(sorted(cited), 1)}
    reals = {label: identity for identity, label in labels.items()}
    draft_labels = {f"d{index}": index - 1 for index in range(1, len(drafts) + 1)}
    payload = [
        {
            "draft": f"d{index}",
            "type": claim.type,
            "text": claim.text,
            "notes": [labels[identity] for identity in claim.notes],
        }
        for index, claim in enumerate(drafts, 1)
    ]
    id_cost = max((len(identity) for identity in cited), default=0) + 2
    prompt = (
        policy.condense_instructions
        + "\n\nSubject: "
        + subject
        + f"\nSize cap: your claims with their ids at most {room(policy, header)} characters "
        + f"in all, counting each note id you cite as {id_cost} characters.\n\nBriefing:\n\n"
        + canonical(JSON_VALUE.validate_python({"claims": payload})).decode("utf-8")
    )
    try:
        result = judge.judge(prompt, CondensedBrief)
    except SetupError as exc:
        ledger.problems.append("condense failed; the draft first page is kept: " + str(exc))
        return None
    front: list[BriefClaim] = []
    carried: set[int] = set()
    for claim in result.answer.claims:
        if not claim.notes or any(label not in reals for label in claim.notes):
            ledger.problems.append("condensed claim dropped whole: empty or unknown citations")
            continue
        front.append(
            BriefClaim(
                type=claim.type,
                text=" ".join(claim.text.split()),
                notes=tuple(dict.fromkeys(reals[label] for label in claim.notes)),
            )
        )
        carried |= {draft_labels[label] for label in claim.carries if label in draft_labels}
    kept = {identity for claim in front for identity in claim.notes}
    ledger.left_out.extend(
        BriefDisposition(note=identity, reason=CUT_FOR_SIZE) for identity in sorted(cited - kept)
    )
    overflow = [claim for index, claim in enumerate(drafts) if index not in carried]
    return Condensed(front, overflow, tuple(result.models))
