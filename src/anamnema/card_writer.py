"""Source-backed card statement generation through an explicitly identified local model."""

from dataclasses import dataclass
from typing import Any

from pydantic import JsonValue, ValidationError

from anamnema.archive_models import canonical, digest
from anamnema.card_policy import CardPolicy
from anamnema.llama_provider import LlamaProvider
from anamnema.setup_config import Nonempty, SetupError, StrictModel


class CardLine(StrictModel):
    """Model labels are mapped to known source-note identities by the writer."""

    text: Nonempty
    notes: tuple[Nonempty, ...]
    front: bool


class CardAnswer(StrictModel):
    """JSON booleans cannot be replaced by truthy strings in generated page ordering."""

    statements: tuple[CardLine, ...]


@dataclass(frozen=True)
class WrittenCard:
    """Checked statements plus explicit trimming/citation/pinning problems."""

    statements: list[dict[str, Any]]
    model: str
    problems: list[str]


class CardWriter:
    """The same source algorithm with public strict output and transport identity."""

    def __init__(self, provider: LlamaProvider, policy: CardPolicy) -> None:
        self.provider, self.policy = provider, policy
        self.identity = digest(
            {"model": provider.identity, "policy": policy.model_dump(mode="json")}
        )

    def write(
        self, entity: dict[str, Any], notes: list[dict[str, Any]], moves: list[dict[str, Any]]
    ) -> WrittenCard:
        """Write only supported statements, reporting dropped labels and capacity changes."""
        if not notes:
            return WrittenCard([], "", [])
        move_data: list[JsonValue] = [move for move in moves]
        data: dict[str, JsonValue] = {
            "entity": {
                "name": str(entity["name"]),
                "type": str(entity.get("type", "")),
                "aliases": entity.get("aliases", []),
            },
            "statements": self.policy.statements * self.policy.factor(str(entity["name"])),
            "front": self.policy.who,
            "moves": move_data,
            "notes": [
                {
                    "label": f"n{index}",
                    "text": str(note["text"]),
                    "said_at": note.get("said_at"),
                    "pinned": bool(note.get("pinned")),
                    "historical": note.get("status", "active") != "active",
                }
                for index, note in enumerate(notes, 1)
            ],
        }
        body: dict[str, JsonValue] = {
            **self.policy.model_options,
            "model": str(self.provider.identity["alias"]),
            "stream": False,
            "max_tokens": self.policy.answer_tokens,
            "messages": [
                {
                    "role": "user",
                    "content": self.policy.instructions + "\n" + canonical(data).decode("utf-8"),
                }
            ],
            "response_format": {
                "type": "json_schema",
                "json_schema": {"schema": CardAnswer.model_json_schema()},
            },
        }
        raw = self.provider.generate(body)
        try:
            answer = CardAnswer.model_validate_json(raw)
        except ValidationError as exc:
            raise SetupError("card writer returned an invalid structured answer") from exc
        return self._checked(str(entity["name"]), answer, notes)

    def _checked(self, name: str, answer: CardAnswer, notes: list[dict[str, Any]]) -> WrittenCard:
        ids = {f"n{index}": str(note["id"]) for index, note in enumerate(notes, 1)}
        problems: list[str] = []
        statements: list[dict[str, Any]] = []
        for index, line in enumerate(answer.statements, 1):
            text = " ".join(line.text.split())
            cited = [ids[label] for label in line.notes if label in ids]
            if not text or not cited:
                problems.append(f"statement {index} dropped: no text or known supporting note")
                continue
            if any(label not in ids for label in line.notes):
                problems.append(f"statement {index}: unknown supporting labels dropped")
            statements.append({"text": text, "notes": cited, "front": line.front})
        most = self.policy.statements * self.policy.factor(name)
        if len(statements) > most:
            problems.append("statement cap reached; later statements dropped")
            statements = statements[:most]
        front = [line for line in statements if line["front"]]
        for extra in front[self.policy.who :]:
            extra["front"] = False
            problems.append("identity-line cap reached; line moved to facts")
        statements = [line for line in statements if line["front"]] + [
            line for line in statements if not line["front"]
        ]
        cited = {identity for line in statements for identity in line["notes"]}
        problems.extend(
            "pinned note left off card: " + str(note["id"])
            for note in notes
            if note.get("pinned") and str(note["id"]) not in cited
        )
        return WrittenCard(statements, self.identity, problems)
