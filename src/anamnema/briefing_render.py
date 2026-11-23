"""Bounded first-page recall and whole-claim overflow pages, preserving every citation."""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import get_args

from anamnema.briefing_models import BriefClaim, BriefLedger, ClaimKind
from anamnema.briefing_policy import BriefingPolicy

# Source algorithm order: contradictions and open points may not leave page one.
MOVABLE: tuple[ClaimKind, ...] = (
    "low_relevance",
    "inference",
    "rejected",
    "fact",
    "instruction",
    "lesson",
)


@dataclass(frozen=True)
class RenderedBrief:
    """Only the first page is delivered; further pages retain claims for explicit reading."""

    pages: tuple[str, ...]
    tokens_estimated: tuple[int, ...]
    oversized_pages: tuple[int, ...]
    review: str


def sections(claims: list[BriefClaim]) -> str:
    """Known claim kinds have predictable sections; every claim stays one cited line."""
    result: list[str] = []
    for kind in get_args(ClaimKind):
        selected = [claim for claim in claims if claim.type == kind]
        if not selected:
            continue
        title = kind.replace("_", " ").capitalize()
        if kind == "contradiction":
            title += " (open: check both sources with the owner)"
        result.extend(["", "## " + title, ""])
        result.extend(f"- {claim.text} [{', '.join(claim.notes)}]" for claim in selected)
    return "\n".join(result) + "\n"


def render_briefing(
    subject: str,
    ledger: BriefLedger,
    policy: BriefingPolicy,
    *,
    carried_overflow: Sequence[BriefClaim] = (),
) -> RenderedBrief:
    """Fit whole claims after the optional condense step; size never conceals a missing claim.

    ``carried_overflow`` holds the draft claims a condensed first page does not carry: they go
    to the further pages first, ahead of any claim the size moves below take off page one.
    """
    subject = " ".join(subject.split())
    cited = len({i for claim in ledger.claims for i in claim.notes})
    header = (
        f"# Briefing: {subject}\n\n- Status: {ledger.status}\n"
        f"- Notes: {len(ledger.selected)} selected, {len(ledger.processed)} read, {cited} cited\n"
        f"- Review: {len(ledger.suspect)} suspect, {len(ledger.left_out)} left out, "
        f"{len(ledger.problems)} integrity problems\n"
        "- Statements are as of their notes' dates; contradictions remain open.\n"
    )
    front = list(enumerate(ledger.claims))
    for kind in MOVABLE:
        while policy.estimate(header + sections([claim for _, claim in front])) > policy.ceiling:
            movable = [position for position, (_, claim) in enumerate(front) if claim.type == kind]
            if not movable or len(front) <= 1:
                break
            front.pop(movable[-1])
    retained = {index for index, _ in front}
    overflow = [
        *carried_overflow,
        *(claim for index, claim in enumerate(ledger.claims) if index not in retained),
    ]
    pages = [header + sections([claim for _, claim in front])]
    pages.extend(
        _overflow(subject, overflow, len(ledger.claims) + len(carried_overflow) + 1, policy)
    )
    estimated = tuple(policy.estimate(page) for page in pages)
    oversized = tuple(index for index, size in enumerate(estimated, 1) if size > policy.ceiling)
    return RenderedBrief(tuple(pages), estimated, oversized, _review(subject, ledger))


def _overflow(
    subject: str, claims: list[BriefClaim], maximum_pages: int, policy: BriefingPolicy
) -> list[str]:
    groups: list[list[BriefClaim]] = []
    for claim in claims:
        candidate = [*groups[-1], claim] if groups else [claim]
        header = f"# Briefing: {subject} (page {maximum_pages})\n\n- Read on demand.\n"
        if groups and policy.estimate(header + sections(candidate)) <= policy.ceiling:
            groups[-1] = candidate
        else:
            groups.append([claim])
    return [
        f"# Briefing: {subject} (page {number})\n\n- Read on demand.\n" + sections(group)
        for number, group in enumerate(groups, 2)
    ]


def _review(subject: str, ledger: BriefLedger) -> str:
    lines = [f"# Review: {subject}", "", "No stored memory was changed."]
    for heading, items in (("Suspect", ledger.suspect), ("Left out", ledger.left_out)):
        lines.extend(["", "## " + heading])
        lines.extend(f"- {item.note}: {' '.join(item.reason.split())}" for item in items)
    lines.extend(["", "## Integrity problems", *["- " + p for p in ledger.problems]])
    never_read = sorted(set(ledger.selected) - ledger.processed)
    lines.extend(["", "## Never read", *["- " + identity for identity in never_read]])
    return "\n".join(lines) + "\n"
