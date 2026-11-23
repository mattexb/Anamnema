"""Source-derived naming judgement with strict local JSON and no persistence side effect."""

from dataclasses import dataclass
from typing import Any

from pydantic import JsonValue, ValidationError

from anamnema.archive_models import canonical, digest
from anamnema.llama_provider import LlamaProvider
from anamnema.naming_policy import NamingPolicy
from anamnema.setup_config import Nonempty, SetupError, StrictModel
from anamnema.speech import scrub
from anamnema.speech_policy import SpeechPolicy, speech_policy


class SubjectAnswer(StrictModel):
    """Truthiness cannot promote a string known=false into a named subject."""

    known: bool
    subject: str


class NamingAnswer(StrictModel):
    subjects: dict[Nonempty, SubjectAnswer]


@dataclass(frozen=True)
class NamedSubject:
    note: str
    known: bool
    subject: str


@dataclass(frozen=True)
class NamingJudgement:
    subjects: tuple[NamedSubject, ...]
    model: str
    problems: tuple[str, ...]


def cut_at_word(text: str, limit: int) -> str:
    """Preserve source whole-word clipping, except a single overlong unbroken word."""
    head = text[: limit + 1]
    space = head.rfind(" ")
    return (head[:space] if space > 0 else text[:limit]).rstrip(" ,;:-")


class NamingWriter:
    """Use verified sources and local model identity; a judgement is not a factual verdict."""

    def __init__(
        self, provider: LlamaProvider, policy: NamingPolicy, speech: SpeechPolicy | None = None
    ) -> None:
        self.provider, self.policy = provider, policy
        self.speech = speech if speech is not None else speech_policy()
        self.identity = digest(
            {
                "model": provider.identity,
                "policy": policy.model_dump(mode="json"),
                "speech": self.speech.model_dump(mode="json"),
            }
        )

    def judge(self, notes: list[dict[str, Any]], sources: dict[str, list[str]]) -> NamingJudgement:
        """Missing/empty known labels remain pending; malformed or unknown labels are errors."""
        if not notes:
            return NamingJudgement((), self.identity, ())
        if len(notes) > self.policy.batch or len({note["id"] for note in notes}) != len(notes):
            raise SetupError("naming batch exceeds configured bounds or repeats a note")
        payload: list[JsonValue] = [
            {
                "label": f"n{index}",
                "text": scrub(str(note["text"]), self.speech)[0],
                "kind": note.get("kind"),
                "said_at": note.get("said_at"),
                "sources": [
                    scrub(row[: self.policy.row_characters], self.speech)[0]
                    for row in sources.get(str(note["id"]), [])[: self.policy.source_rows]
                ],
            }
            for index, note in enumerate(notes, start=1)
        ]
        body: dict[str, JsonValue] = {
            **self.policy.model_options,
            "model": str(self.provider.identity["alias"]),
            "stream": False,
            "max_tokens": self.policy.answer_tokens,
            "messages": [
                {
                    "role": "user",
                    "content": self.policy.instructions.replace(
                        "{characters}", str(self.policy.subject_characters)
                    )
                    + "\n"
                    + canonical(payload).decode("utf-8"),
                }
            ],
            "response_format": {
                "type": "json_schema",
                "json_schema": {"schema": NamingAnswer.model_json_schema()},
            },
        }
        raw = self.provider.generate(body)
        try:
            answer = NamingAnswer.model_validate_json(raw)
        except ValidationError as exc:
            raise SetupError("naming writer returned an invalid structured answer") from exc
        return self._checked(notes, answer)

    def _checked(self, notes: list[dict[str, Any]], answer: NamingAnswer) -> NamingJudgement:
        labels = {f"n{index}": str(note["id"]) for index, note in enumerate(notes, start=1)}
        if set(answer.subjects) - labels.keys():
            raise SetupError("naming writer returned unknown note labels")
        limit = int(self.policy.subject_characters * (1 + self.policy.subject_margin))
        subjects: list[NamedSubject] = []
        problems: list[str] = []
        for label, identity in labels.items():
            item = answer.subjects.get(label)
            if item is None:
                problems.append(identity + ": missing subject answer; remains pending")
                continue
            text = " ".join(scrub(item.subject, self.speech)[0].split())
            if item.known and not text:
                problems.append(identity + ": known subject is empty; remains pending")
                continue
            if len(text) > limit:
                text = cut_at_word(text, limit)
                problems.append(identity + ": subject cut at a word within configured bound")
            subjects.append(NamedSubject(identity, item.known, text if item.known else ""))
        return NamingJudgement(tuple(subjects), self.identity, tuple(problems))
