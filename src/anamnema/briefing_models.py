"""Source briefing citation accounting, independent of workers, files and live stores."""

from dataclasses import dataclass, field
from typing import Literal

from anamnema.setup_config import Nonempty, SetupError, StrictModel

ClaimKind = Literal[
    "fact",
    "instruction",
    "lesson",
    "rejected",
    "inference",
    "contradiction",
    "open",
    "low_relevance",
]


class BriefClaim(StrictModel):
    """A standalone claim is retained only when every cited note was given to this call."""

    type: ClaimKind
    text: Nonempty
    notes: tuple[Nonempty, ...]


class BriefDisposition(StrictModel):
    """A review suggestion or explicit omission does not silently count as recalled content."""

    note: Nonempty
    reason: Nonempty


class BriefAnswer(StrictModel):
    """The reader must account for every note, including suspects and off-subject evidence."""

    claims: tuple[BriefClaim, ...]
    suspect: tuple[BriefDisposition, ...]
    left_out: tuple[BriefDisposition, ...]


class CombinedBrief(StrictModel):
    """Combining cannot invent new supporting notes or silently lose earlier citations."""

    claims: tuple[BriefClaim, ...]


@dataclass
class BriefLedger:
    """Accounting retains all gaps; a worker error never mutates memory or conceals evidence."""

    selected: tuple[str, ...]
    claims: list[BriefClaim] = field(default_factory=list[BriefClaim])
    suspect: list[BriefDisposition] = field(default_factory=list[BriefDisposition])
    left_out: list[BriefDisposition] = field(default_factory=list[BriefDisposition])
    problems: list[str] = field(default_factory=list[str])
    processed: set[str] = field(default_factory=set[str])

    def __post_init__(self) -> None:
        if len(self.selected) != len(set(self.selected)) or any(not i for i in self.selected):
            raise SetupError("briefing selection repeats an identity or contains an empty id")

    def read(self, answer: BriefAnswer, labels: dict[str, str]) -> None:
        """Apply a batch atomically to this ledger; labels resolve only this batch's notes."""
        ids = set(labels.values())
        if not ids or len(ids) != len(labels) or not ids <= set(self.selected):
            raise SetupError("briefing batch has empty, repeated or unselected identities")
        if ids & self.processed:
            raise SetupError("briefing batch repeats notes already accounted for")
        claims = self._claims(answer.claims, labels)
        suspect = self._listed(answer.suspect, labels)
        omitted = self._listed(answer.left_out, labels)
        accounted = {i for claim in claims for i in claim.notes}
        accounted |= {item.note for item in [*suspect, *omitted]}
        omitted.extend(
            BriefDisposition(note=i, reason="neither cited nor explicitly left out")
            for i in sorted(ids - accounted)
        )
        self.problems.extend("unaccounted note: " + i for i in sorted(ids - accounted))
        self.claims.extend(claims)
        self.suspect.extend(suspect)
        self.left_out.extend(omitted)
        self.processed.update(ids)

    def combine(self, answer: CombinedBrief, labels: dict[str, str]) -> None:
        """Only citations already supported by accepted partial claims may survive a combine."""
        cited = {i for claim in self.claims for i in claim.notes}
        allowed = {label: identity for label, identity in labels.items() if identity in cited}
        claims = self._claims(answer.claims, allowed)
        retained = {i for claim in claims for i in claim.notes}
        lost = sorted(cited - retained)
        self.left_out.extend(
            BriefDisposition(note=i, reason="cited in its batch, lost in combining") for i in lost
        )
        self.problems.extend("citation lost in combining: " + i for i in lost)
        self.claims = claims

    def _claims(self, claims: tuple[BriefClaim, ...], labels: dict[str, str]) -> list[BriefClaim]:
        accepted: list[BriefClaim] = []
        for claim in claims:
            if not claim.notes or any(label not in labels for label in claim.notes):
                self.problems.append("claim dropped whole: empty or unknown citations")
                continue
            accepted.append(
                claim.model_copy(
                    update={
                        "text": " ".join(claim.text.split()),
                        "notes": tuple(dict.fromkeys(labels[label] for label in claim.notes)),
                    }
                )
            )
        return accepted

    def _listed(
        self, items: tuple[BriefDisposition, ...], labels: dict[str, str]
    ) -> list[BriefDisposition]:
        accepted: list[BriefDisposition] = []
        for item in items:
            if item.note not in labels:
                self.problems.append("disposition dropped: unknown note label")
                continue
            accepted.append(item.model_copy(update={"note": labels[item.note]}))
        return accepted

    @property
    def status(self) -> Literal["failed", "partial", "ready"]:
        """Ready accounts only for notes actually read, never those merely selected."""
        if not self.selected or not self.processed:
            return "failed"
        if self.problems or set(self.selected) - self.processed:
            return "partial"
        return "ready"
