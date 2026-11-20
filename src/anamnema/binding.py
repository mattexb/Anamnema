"""Explicit owner answers to immediately preceding numbered proposals."""

import re
from dataclasses import dataclass

from anamnema.notes_policy import Names, NotesPolicy
from anamnema.setup_config import SetupError
from anamnema.speech import Row


@dataclass(frozen=True)
class Binding:
    """Named agreements/rejections only; unnamed items remain unresolved."""

    answers: int
    rejected: tuple[int, ...]
    accepted: tuple[int, ...]
    unresolved: tuple[int, ...]


def bindings(rows: tuple[Row, ...], policy: NotesPolicy) -> dict[int, Binding]:
    """Bind direct owner replies without inferring agreement from silence."""
    found: dict[int, Binding] = {}
    proposal: Row | None = None
    for row in rows:
        if row.speaker == "agent":
            items = re.findall(policy.numbered_pattern, row.text)
            proposal = row if len(items) >= policy.minimum_items else None
        else:
            if proposal is not None:
                found[row.n] = bind_answer(row.text, proposal, policy)
            proposal = None
    return found


def bind_answer(answer: str, proposal: Row, policy: NotesPolicy) -> Binding:
    """Interpret explicit item numbers, giving rejection precedence in a conflict."""

    def numbers(pattern: str) -> set[int]:
        matches = re.findall(pattern, answer, re.IGNORECASE)
        return {int(n) for text in matches for n in re.findall(policy.numbers_pattern, text)}

    try:
        items = {int(n) for n in re.findall(policy.numbered_pattern, proposal.text)}
        rejected = numbers(policy.disagree_pattern) & items
        accepted = (numbers(policy.agree_pattern) & items) - rejected
    except ValueError as exc:
        raise SetupError("binding patterns must produce integer item numbers") from exc
    return Binding(
        proposal.n,
        tuple(sorted(rejected)),
        tuple(sorted(accepted)),
        tuple(sorted(items - rejected - accepted)),
    )


def describe(binding: Binding, names: Names) -> str:
    """Label code's interpretation explicitly rather than attributing it to the owner."""
    parts = [f"this {names.owner} row answers {names.agent}'s numbered list in #{binding.answers}"]
    if binding.rejected:
        parts.append(f"REJECTED items: {', '.join(map(str, binding.rejected))}")
    if binding.accepted:
        parts.append(f"accepted items: {', '.join(map(str, binding.accepted))}")
    if binding.unresolved:
        parts.append(f"items not addressed by name: {', '.join(map(str, binding.unresolved))}")
    return f"[binding by code, not {names.owner}'s words: " + "; ".join(parts) + "]"
