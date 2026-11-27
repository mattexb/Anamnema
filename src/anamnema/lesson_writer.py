"""Structured multi-lesson aggregation with strict label and source provenance."""

import re
from dataclasses import dataclass
from typing import Annotated, Any

from pydantic import JsonValue, ValidationError, WithJsonSchema, field_validator

from anamnema.archive_models import canonical, digest
from anamnema.lesson_models import LessonContent, LessonEpisode, LessonRoute, validate_support
from anamnema.lesson_policy import LessonPolicy
from anamnema.llama_provider import LlamaProvider
from anamnema.setup_config import Nonempty, SetupError, StrictModel
from anamnema.speech import scrub
from anamnema.speech_policy import SpeechPolicy, speech_policy

LABEL = re.compile(r"\b(?:n\d+|E\d+)\b")
NEW_LABEL = re.compile(r"L[1-9][0-9]*\Z")
JUDGED_STATUSES = ("reviewed", "conflict")


class PlacedEpisode(StrictModel):
    """One note may join multiple lessons, or explicitly none."""

    lesson: Nonempty
    evidence: Nonempty
    event: Nonempty
    event_reason: str


class JudgedContent(LessonContent):
    """Conflicts are explicit, never guessed settled by parser fallback.

    A new lesson beside existing ones names the nearest (``nearest``, an E label) and writes how it
    differs from it (``differs``): one rule had splintered into fourteen lessons, each too weak to
    be delivered. Both fields are answer-only and never stored.
    """

    status: Annotated[Nonempty, WithJsonSchema({"type": "string", "enum": list(JUDGED_STATUSES)})]
    nearest: str = ""
    differs: str = ""

    @field_validator("status")
    @classmethod
    def known_status(cls, value: str) -> str:
        """Keep runtime validation consistent with the model-facing schema enum."""
        if value not in JUDGED_STATUSES:
            raise ValueError("lesson status must be reviewed or conflict")
        return value


class LessonAnswer(StrictModel):
    """The source aggregator's complete labelled answer shape."""

    lessons: dict[str, JudgedContent]
    notes: dict[str, tuple[PlacedEpisode, ...]]


def answer_schema(policy: LessonPolicy) -> dict[str, Any]:
    """Expose configured vocabulary constraints to generation as well as local validation."""
    schema = LessonAnswer.model_json_schema()
    schema["$defs"]["PlacedEpisode"]["properties"]["evidence"]["enum"] = list(policy.evidence_types)
    schema["$defs"]["JudgedContent"]["properties"]["severity"]["enum"] = list(policy.severities)
    return schema


@dataclass(frozen=True)
class JudgedLesson:
    """Checked content ready for embedding and revision-guarded persistence."""

    id: str
    revision: int
    content: LessonContent
    episodes: tuple[LessonEpisode, ...]


class LessonWriter:
    """No free-text JSON extraction, private CLI runner or unknown-label dropping."""

    def __init__(
        self, provider: LlamaProvider, policy: LessonPolicy, speech: SpeechPolicy | None = None
    ) -> None:
        self.provider, self.policy = provider, policy
        self.speech = speech or speech_policy()
        self.identity = digest(
            {
                "model": provider.identity,
                "policy": policy.model_dump(mode="json"),
                "speech": self.speech.model_dump(mode="json"),
            }
        )

    def judge(
        self, instance: str, notes: list[dict[str, Any]], existing: list[dict[str, Any]]
    ) -> tuple[list[JudgedLesson], list[str]]:
        """Every note is addressed; unresolved is visible, never a guessed placement."""
        if not 1 <= len(notes) <= self.policy.batch or len(existing) > self.policy.candidates:
            raise SetupError("lesson generation input exceeds configured batch/candidate bound")
        payload: dict[str, JsonValue] = {
            "concepts": dict(self.policy.concepts),
            "notes": [
                {"label": f"n{i}", "text": note["text"], "source": note["source"]}
                for i, note in enumerate(notes, 1)
            ],
            "existing": [
                {
                    "label": f"E{i}",
                    **{
                        field: item.get(field)
                        for field in (
                            "statement",
                            "mechanism",
                            "routes",
                            "severity",
                            "status",
                            "conflict",
                            "concepts",
                        )
                    },
                }
                for i, item in enumerate(existing, 1)
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
                    "content": self.policy.instructions
                    + "\n"
                    + scrub(canonical(payload).decode("utf-8"), self.speech)[0],
                }
            ],
            "response_format": {
                "type": "json_schema",
                "json_schema": {"schema": answer_schema(self.policy)},
            },
        }
        raw = self.provider.generate(body)
        try:
            answer = LessonAnswer.model_validate_json(raw)
            return self._checked(instance, answer, notes, existing, digest(payload))
        except ValidationError as exc:
            raise SetupError("lesson writer returned an invalid structured answer") from exc

    def _checked(
        self,
        instance: str,
        answer: LessonAnswer,
        notes: list[dict[str, Any]],
        existing: list[dict[str, Any]],
        call: str,
    ) -> tuple[list[JudgedLesson], list[str]]:
        note_ids = {f"n{i}": note for i, note in enumerate(notes, 1)}
        old = {f"E{i}": item for i, item in enumerate(existing, 1)}
        if set(answer.notes) != set(note_ids):
            raise SetupError("lesson answer must address exactly the supplied note labels")
        if len(answer.lessons) > self.policy.max_lessons:
            raise SetupError("lesson answer exceeds configured lesson count")
        if any(label not in old and not NEW_LABEL.fullmatch(label) for label in answer.lessons):
            raise SetupError("lesson answer names an unknown lesson label")
        if old and any(
            label not in old and (content.nearest.strip() not in old or not content.differs.strip())
            for label, content in answer.lessons.items()
        ):
            raise SetupError(
                "a new lesson does not say how it differs from the nearest existing one"
            )
        keys = {
            label: str(old[label]["lesson_id"])
            if label in old
            else digest([instance, self.identity, call, label])
            for label in answer.lessons
        }
        ids = {
            **{label: str(note["note"]) for label, note in note_ids.items()},
            **{label: str(item["lesson_id"]) for label, item in old.items()},
        }
        episodes: dict[str, list[LessonEpisode]] = {label: [] for label in answer.lessons}
        for label, placements in answer.notes.items():
            if len({place.lesson for place in placements}) != len(placements):
                raise SetupError("lesson answer repeats a note's placement")
            for place in placements:
                if place.lesson not in episodes or place.evidence not in self.policy.evidence_types:
                    raise SetupError("lesson placement has unknown lesson or evidence type")
                episodes[place.lesson].append(
                    LessonEpisode(
                        note=ids[label],
                        evidence=place.evidence,
                        event=digest([call, place.event]),
                        event_reason=LABEL.sub(
                            lambda m: ids.get(m.group(), m.group()), place.event_reason
                        ),
                        records=tuple(note_ids[label]["source_ids"]),
                    )
                )
        result = [
            self._lesson(content, keys[label], old.get(label), episodes[label], ids)
            for label, content in answer.lessons.items()
        ]
        return result, [ids[label] for label, placements in answer.notes.items() if not placements]

    def _lesson(
        self,
        content: JudgedContent,
        key: str,
        previous: dict[str, Any] | None,
        episodes: list[LessonEpisode],
        ids: dict[str, str],
    ) -> JudgedLesson:
        if not episodes:
            raise SetupError("a generated lesson has no supplied source-backed episodes")
        if (
            content.severity not in self.policy.severities
            or not set(content.concepts) <= self.policy.concepts.keys()
        ):
            raise SetupError("lesson severity or concepts are outside configured vocabulary")
        if (content.status == "conflict") != bool(content.conflict.strip()):
            raise SetupError("lesson conflict status and explanation disagree")
        supported = {episode.note for episode in episodes}
        routes: list[LessonRoute] = []
        for route in content.routes:
            if any(note not in ids or ids[note] not in supported for note in route.notes):
                raise SetupError("lesson route cites unknown or unrelated evidence")
            routes.append(route.model_copy(update={"notes": tuple(ids[n] for n in route.notes)}))
        try:
            validate_support(tuple(episodes), tuple(routes))
        except ValueError as exc:
            raise SetupError(f"invalid lesson evidence: {exc}") from exc

        def replace(text: str) -> str:
            return LABEL.sub(lambda m: ids.get(m.group(), m.group()), text)

        mapped = content.model_copy(
            update={
                "statement": replace(content.statement),
                "mechanism": replace(content.mechanism),
                "conflict": replace(content.conflict),
                "routes": tuple(
                    route.model_copy(
                        update={
                            "condition": replace(route.condition),
                            "correction": replace(route.correction),
                        }
                    )
                    for route in routes
                ),
            }
        )
        stored = LessonContent.model_validate(mapped.model_dump(exclude={"nearest", "differs"}))
        return JudgedLesson(
            key, int(previous["revision"]) if previous else 0, stored, tuple(episodes)
        )
