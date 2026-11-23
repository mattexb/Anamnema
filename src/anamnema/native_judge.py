"""Structured tool-free judgement using the same bounded fresh-worker process transport."""

from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue, ValidationError, model_validator

from anamnema.archive_models import canonical, digest
from anamnema.judge_config import JudgeConfig, JudgePolicy
from anamnema.setup_config import Nonempty, SetupError, StrictModel
from anamnema.snapshot_worker import run_worker


class NativeEnvelope(BaseModel):
    """Native metadata is extensible; the judgement-bearing fields remain strict."""

    model_config = ConfigDict(strict=True)
    is_error: bool
    subtype: Literal["success"]
    modelUsage: dict[Nonempty, dict[str, JsonValue]] = Field(min_length=1)
    structured_output: dict[str, JsonValue]

    @model_validator(mode="after")
    def success_only(self) -> NativeEnvelope:
        """A strict boolean false, not a false-like value, denotes native success."""
        if self.is_error:
            raise ValueError("native judgement failed")
        return self


@dataclass(frozen=True)
class Judgement[Answer: StrictModel]:
    """A validated answer and the models actually reported by the native result envelope."""

    answer: Answer
    models: tuple[str, ...]
    identity: str


class NativeJudge:
    """Never falls back to free text, a live session, a shell or an unstructured answer."""

    def __init__(self, config: JudgeConfig, policy: JudgePolicy) -> None:
        self.config, self.policy = config, policy
        self.identity = digest({"model": config.model, "policy": policy.model_dump(mode="json")})

    def judge[Answer: StrictModel](self, prompt: str, schema: type[Answer]) -> Judgement[Answer]:
        """Validate the entire success envelope and schema before returning any judgement."""
        if not prompt.strip() or len(prompt.encode("utf-8")) > self.policy.max_input_bytes:
            raise SetupError("judge prompt is empty or exceeds configured byte limit")
        command = [
            str(self.config.executable),
            self.policy.model_option,
            self.config.model,
            *self.policy.worker_arguments,
            self.policy.schema_option,
            canonical(schema.model_json_schema()).decode("utf-8"),
        ]
        raw = run_worker(
            command,
            self.config,
            self.policy,
            "native judge",
            input_text=prompt,
            input_limit=self.policy.max_input_bytes,
            registry=self.config.registry,
        )
        return self._validated(raw, schema)

    def _validated[Answer: StrictModel](self, raw: str, schema: type[Answer]) -> Judgement[Answer]:
        try:
            envelope = NativeEnvelope.model_validate_json(raw)
            answer = schema.model_validate_json(canonical(envelope.structured_output))
        except (ValueError, TypeError, ValidationError) as exc:
            raise SetupError(
                "native judge returned an invalid structured success envelope"
            ) from exc
        models = tuple(sorted(envelope.modelUsage))
        return Judgement(answer, models, digest([self.identity, list(models)]))
