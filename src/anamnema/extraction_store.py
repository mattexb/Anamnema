"""Immutable successful chunks and separate attempt records with independent read-back."""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import JsonValue
from pymongo.database import Database
from pymongo.errors import PyMongoError

from anamnema.archive_models import canonical, digest
from anamnema.chunks import Chunk
from anamnema.extraction_config import ExtractionConfig
from anamnema.notes_policy import NotesPolicy
from anamnema.notes_response import Response, source_notes, validate_response
from anamnema.processing import Window
from anamnema.setup_config import SetupError
from anamnema.speech_policy import SpeechPolicy


class AttemptState(StrEnum):
    """Execution outcome, distinct from the lifecycle of immutable chunk documents."""

    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"
    INTERRUPTED = "interrupted"


class ExtractionStore:
    """No replacement/deletion/repair of successful notes; no live-store fallback."""

    def __init__(
        self,
        database: Database[dict[str, Any]],
        config: ExtractionConfig,
        notes: NotesPolicy,
        speech: SpeechPolicy,
    ) -> None:
        if not database.write_concern.acknowledged:
            raise SetupError("extraction requires acknowledged writes")
        self.database = database
        self.config = config
        self.notes = notes
        self.speech = speech

    def selector(self, window: Window, chunk: Chunk, run: str) -> dict[str, JsonValue]:
        """Exact input identity includes scope, ordered sources and chunk bytes."""
        body: dict[str, JsonValue] = {
            "schema_version": self.config.schema_version,
            "instance": window.boundary.instance,
            "session": window.boundary.session,
            "substrate": window.boundary.substrate,
            "window": window.report()["key"],
            "run": run,
            "chunk": chunk.index,
            "input_sha256": digest(chunk.text),
            "input_text": chunk.text,
            "sources": [row.source.identity for row in chunk.rows],
        }
        return {"_id": digest(body), **body}

    def load(self, selector: dict[str, JsonValue], chunk: Chunk) -> dict[str, Any] | None:
        """Revalidate saved output and all source metadata before permitting reuse."""
        try:
            document = self.database[self.config.chunks_collection].find_one(
                {"_id": selector["_id"]}
            )
        except PyMongoError as exc:
            raise SetupError(f"saved notes lookup failed ({type(exc).__name__})") from exc
        if document is None:
            return None
        if _json({key: document.get(key) for key in selector}) != _json(selector):
            raise SetupError("saved notes ownership/input differs; no automatic repair")
        answer = document.get("answer")
        if not isinstance(answer, str):
            raise SetupError("saved notes answer is missing or invalid")
        validated = validate_response(answer, self.notes, self.speech)
        normalized = canonical(validated.model_dump(mode="json")).decode("utf-8")
        if answer != normalized or document.get("answer_sha256") != digest(answer):
            raise SetupError("saved notes answer is corrupt; no automatic repair")
        if _json(document.get("notes")) != _json(source_notes(validated, chunk, self.notes)):
            raise SetupError("saved note source references differ; no automatic repair")
        return document

    def save(
        self, selector: dict[str, JsonValue], chunk: Chunk, answer: Response, attempt: str
    ) -> bool:
        """Write once, then read and independently derive notes/source references."""
        normalized = canonical(answer.model_dump(mode="json")).decode("utf-8")
        document = {
            **selector,
            "answer": normalized,
            "answer_sha256": digest(normalized),
            "notes": source_notes(answer, chunk, self.notes),
            "attempt": attempt,
        }
        try:
            result = self.database[self.config.chunks_collection].update_one(
                {"_id": selector["_id"]},
                {"$setOnInsert": document},
                upsert=True,
            )
        except PyMongoError as exc:
            raise SetupError(f"saving notes failed ({type(exc).__name__}); safe to resume") from exc
        if self.load(selector, chunk) is None:
            raise SetupError("saved notes read-back is missing; safe to resume")
        return result.upserted_id is not None

    def attempt(self, document: dict[str, JsonValue]) -> None:
        """Attempt transitions are explicit; write failure never counts as success."""
        try:
            collection = self.database[self.config.attempts_collection]
            collection.replace_one({"_id": document["_id"]}, document, upsert=True)
            if canonical(collection.find_one({"_id": document["_id"]})) != canonical(document):
                raise SetupError("extraction attempt read-back differs")
        except PyMongoError as exc:
            raise SetupError(f"recording extraction attempt failed ({type(exc).__name__})") from exc


def _json(value: Any) -> bytes:
    try:
        return canonical(value)
    except (ValueError, TypeError) as exc:
        raise SetupError("saved notes contain invalid JSON values; no automatic repair") from exc
