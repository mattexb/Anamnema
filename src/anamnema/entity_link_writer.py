"""Source-derived entity feed judgement with labelled roster, ambiguity and proposal accounting."""

from dataclasses import dataclass
from typing import Any, Literal

from pydantic import JsonValue, ValidationError

from anamnema.archive_models import canonical, digest
from anamnema.entity_link_policy import EntityLinkPolicy
from anamnema.llama_provider import LlamaProvider
from anamnema.setup_config import Nonempty, SetupError, StrictModel
from anamnema.speech import scrub
from anamnema.speech_policy import SpeechPolicy, speech_policy


class RosterEntity(StrictModel):
    name: Nonempty
    type: Nonempty
    aliases: tuple[Nonempty, ...] = ()
    status: Literal["confirmed", "unconfirmed"]


class EntityProposal(StrictModel):
    name: Nonempty
    type: Nonempty


class EntityAmbiguity(StrictModel):
    name: Nonempty
    candidates: tuple[Nonempty, ...]


class NoteEntities(StrictModel):
    entities: tuple[Nonempty, ...]
    ambiguous: tuple[EntityAmbiguity, ...]
    new: tuple[EntityProposal, ...]


class EntityLinkAnswer(StrictModel):
    links: dict[Nonempty, NoteEntities]


@dataclass(frozen=True)
class EntityLink:
    note: str
    text: str
    entities: tuple[str, ...]
    ambiguous: tuple[EntityAmbiguity, ...]
    proposed: tuple[str, ...]


@dataclass(frozen=True)
class EntityLinking:
    links: tuple[EntityLink, ...]
    proposals: tuple[EntityProposal, ...]
    problems: tuple[str, ...]
    model: str


class EntityLinkWriter:
    """This writer cannot create entities, settle an ambiguity or mark a graph note done."""

    def __init__(
        self, provider: LlamaProvider, policy: EntityLinkPolicy, speech: SpeechPolicy | None = None
    ) -> None:
        self.provider, self.policy, self.speech = provider, policy, speech or speech_policy()
        self.identity = digest(
            {
                "model": provider.identity,
                "policy": policy.model_dump(mode="json"),
                "speech": self.speech.model_dump(mode="json"),
            }
        )

    def judge(self, notes: list[dict[str, Any]], roster: tuple[RosterEntity, ...]) -> EntityLinking:
        if len(notes) > self.policy.batch or len({str(note["id"]) for note in notes}) != len(notes):
            raise SetupError("entity-link batch exceeds bounds or repeats a note")
        if len(roster) > self.policy.max_roster_entities or len(
            {item.name for item in roster}
        ) != len(roster):
            raise SetupError("entity roster exceeds bounds or repeats a canonical name")
        skipped: list[EntityLink] = []
        eligible: list[dict[str, Any]] = []
        problems: list[str] = []
        for note in notes:
            text = str(note["text"])
            secret = bool(scrub(text, self.speech)[1]) or self.speech.secret_mask in text
            if note.get("kind") in self.policy.unlinked_kinds or secret:
                skipped.append(EntityLink(str(note["id"]), text, (), (), ()))
                if secret:
                    problems.append(str(note["id"]) + ": secret-shaped note excluded from linking")
            else:
                eligible.append(note)
        if not eligible:
            return EntityLinking(tuple(skipped), (), tuple(problems), self.identity)
        selected = {
            f"e{index}": item
            for index, item in enumerate(roster, 1)
            if scrub(item.name, self.speech)[0] == item.name
            and self.speech.secret_mask not in item.name
        }
        known = {label: item.name for label, item in selected.items()}
        roster_rows: list[JsonValue] = [
            {
                "label": label,
                "name": item.name,
                "type": scrub(item.type, self.speech)[0],
                "aliases": [scrub(alias, self.speech)[0] for alias in item.aliases],
            }
            for label, item in selected.items()
        ]
        evidence: list[JsonValue] = [
            {
                "label": f"n{index}",
                "text": scrub(str(note["text"]), self.speech)[0],
                "kind": note.get("kind"),
                "said_at": note.get("said_at"),
            }
            for index, note in enumerate(eligible, 1)
        ]
        prompt = (
            self.policy.instructions
            + "\n"
            + canonical(
                {
                    "roster": roster_rows,
                    "notes": evidence,
                    "proposal_types": [kind for kind in self.policy.proposal_types],
                }
            ).decode("utf-8")
        )
        if len(prompt.encode("utf-8")) > self.policy.max_input_bytes:
            raise SetupError("entity-link prompt exceeds configured byte bound")
        body: dict[str, JsonValue] = {
            **self.policy.model_options,
            "model": str(self.provider.identity["alias"]),
            "stream": False,
            "max_tokens": self.policy.answer_tokens,
            "messages": [{"role": "user", "content": prompt}],
            "response_format": {
                "type": "json_schema",
                "json_schema": {"schema": EntityLinkAnswer.model_json_schema()},
            },
        }
        try:
            answer = EntityLinkAnswer.model_validate_json(self.provider.generate(body))
        except ValidationError as exc:
            raise SetupError("entity linker returned an invalid structured answer") from exc
        return self._checked(eligible, known, answer, tuple(skipped), problems)

    def _checked(
        self,
        notes: list[dict[str, Any]],
        known: dict[str, str],
        answer: EntityLinkAnswer,
        skipped: tuple[EntityLink, ...],
        problems: list[str],
    ) -> EntityLinking:
        labels = {f"n{index}": str(note["id"]) for index, note in enumerate(notes, 1)}
        texts = {str(note["id"]): str(note["text"]) for note in notes}
        if set(answer.links) - labels.keys():
            raise SetupError("entity linker returned unknown note labels")
        links = list(skipped)
        proposals: dict[str, EntityProposal] = {}
        for label, identity in labels.items():
            item = answer.links.get(label)
            if item is None:
                problems.append(identity + ": missing entity answer; remains pending")
                continue
            entities = [known[key] for key in item.entities if key in known]
            if any(key not in known for key in item.entities):
                problems.append(identity + ": unknown roster label dropped")
            ambiguous: list[EntityAmbiguity] = []
            for case in item.ambiguous:
                candidates = tuple(sorted({known[key] for key in case.candidates if key in known}))
                name = " ".join(scrub(case.name, self.speech)[0].split())
                if (
                    len(candidates) > 1
                    and name == case.name
                    and self.speech.secret_mask not in name
                    and len(name) <= self.policy.max_name_characters
                ):
                    ambiguous.append(EntityAmbiguity(name=name, candidates=candidates))
                else:
                    problems.append(identity + ": invalid ambiguity dropped")
            proposed: list[str] = []
            for proposal in item.new:
                name = " ".join(scrub(proposal.name, self.speech)[0].split())
                if name in known.values():
                    entities.append(name)
                elif (
                    name
                    and name == proposal.name
                    and self.speech.secret_mask not in name
                    and len(name) <= self.policy.max_name_characters
                    and proposal.type in self.policy.proposal_types
                ):
                    if name in proposals and proposals[name].type != proposal.type:
                        raise SetupError("entity proposals disagree on a canonical name's type")
                    proposals[name] = proposal
                    proposed.append(name)
                else:
                    problems.append(identity + ": unsafe or unsupported entity proposal dropped")
            links.append(
                EntityLink(
                    identity,
                    texts[identity],
                    tuple(sorted(set(entities))),
                    tuple(ambiguous),
                    tuple(sorted(set(proposed))),
                )
            )
        return EntityLinking(
            tuple(links),
            tuple(proposals[name] for name in sorted(proposals)),
            tuple(problems),
            self.identity,
        )
