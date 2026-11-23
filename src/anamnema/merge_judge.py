"""Bounded structured repeat judgments through the existing verified model transport."""

from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from pydantic import JsonValue, ValidationError

from anamnema.archive_models import canonical, digest
from anamnema.llama_provider import LlamaProvider
from anamnema.memory_models import MemoryNote
from anamnema.merge_policy import MergePolicy
from anamnema.setup_config import Nonempty, SetupError, StrictModel


class Verdict(StrEnum):
    """The source's four meanings; absence is deferred, never different."""

    CLAIM = "same_claim"
    EVENT = "same_event"
    CHANGED = "changed"
    DIFFERENT = "different"


@dataclass(frozen=True)
class Pair:
    """A new insertion and an owned active candidate, with exact source similarity."""

    new: MemoryNote
    old: dict[str, Any]
    score: float


class Answer(StrictModel):
    """One numbered answer; labels are validated against the actual submitted batch."""

    pair: Nonempty
    verdict: Verdict


class Reply(StrictModel):
    """Reject malformed/extra fields rather than extracting JSON from arbitrary prose."""

    verdicts: tuple[Answer, ...]


class MergeJudge:
    """Model and request/prompt policy together identify every judgment."""

    def __init__(self, provider: LlamaProvider, policy: MergePolicy) -> None:
        self.provider, self.policy = provider, policy
        self.identity = digest(
            {"model": provider.identity, "policy": policy.model_dump(mode="json")}
        )

    def verdicts(self, pairs: list[Pair]) -> dict[int, Verdict]:
        """Incomplete labels are absent; unknown or duplicate labels fail the whole batch."""
        shown: list[JsonValue] = [
            {
                "pair": f"p{index}",
                "A": self._shown(pair.old),
                "B": self._shown(pair.new.model_dump(mode="json")),
            }
            for index, pair in enumerate(pairs, 1)
        ]
        body: dict[str, JsonValue] = {
            **self.policy.model_options,
            "model": str(self.provider.identity["alias"]),
            "stream": False,
            "max_tokens": self.policy.answer_tokens,
            "messages": [
                {
                    "role": "user",
                    "content": self.policy.instructions + "\n" + canonical(shown).decode("utf-8"),
                }
            ],
            "response_format": {
                "type": "json_schema",
                "json_schema": {"schema": Reply.model_json_schema()},
            },
        }
        raw = self.provider.generate(body)
        try:
            reply = Reply.model_validate_json(raw)
        except ValidationError as exc:
            raise SetupError("repeat judge returned an invalid structured answer") from exc
        labels = {f"p{index}": index - 1 for index in range(1, len(pairs) + 1)}
        if len({item.pair for item in reply.verdicts}) != len(reply.verdicts):
            raise SetupError("repeat judge repeated a pair label")
        if any(item.pair not in labels for item in reply.verdicts):
            raise SetupError("repeat judge returned an unknown pair label")
        return {labels[item.pair]: item.verdict for item in reply.verdicts}

    def _shown(self, note: dict[str, Any]) -> dict[str, JsonValue]:
        return {
            "text": str(note["text"])[: self.policy.note_characters],
            "kind": str(note["kind"]),
            "said_at": note.get("said_at"),
            "session": note.get("session"),
            "window": note.get("window"),
        }
