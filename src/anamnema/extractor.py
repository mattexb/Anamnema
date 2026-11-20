"""Incremental one-worker extraction, retrying model failures at each window's end."""

from __future__ import annotations

import sys
import time
import uuid
from datetime import UTC, datetime

from pydantic import JsonValue

from anamnema.chunks import Chunk
from anamnema.extraction_store import AttemptState, ExtractionStore
from anamnema.llama_provider import LlamaProvider
from anamnema.notes_request import render, request_body, run_identity
from anamnema.notes_response import Response, validate_response
from anamnema.processing import Window
from anamnema.setup_config import SetupError


class GenerationFailed(SetupError):
    """A bounded model/answer failure eligible for the window's final retry."""


class SlowChunk(SetupError):
    """Stop the run when its worker is in a slow state; keep verified chunks."""


class Extractor:
    """Reuse only checked results; no in-memory success without persisted read-back."""

    def __init__(self, provider: LlamaProvider, store: ExtractionStore) -> None:
        self.provider = provider
        self.store = store
        self.run_key = run_identity(provider.identity, store.notes, store.speech)
        self.attempt_id = uuid.uuid4().hex
        self.calls = self.created = self.reused = self.notes = self.flagged = 0
        self.model_failures: list[JsonValue] = []
        self.unresolved: list[JsonValue] = []
        self._started = datetime.now(UTC).isoformat()

    def run(self, prepared: list[tuple[Window, tuple[Chunk, ...]]]) -> dict[str, JsonValue]:
        """Record lifecycle even on interruption, retaining every successful earlier chunk."""
        state = AttemptState.FAILED
        scope = prepared[0][0].boundary if prepared else None
        base: dict[str, JsonValue] = {
            "_id": self.attempt_id,
            "run": self.run_key,
            "started_at": self._started,
            "instance": scope.instance if scope else None,
            "session": scope.session if scope else None,
            "windows": [window.report()["key"] for window, _ in prepared],
            "planned_chunks": sum(len(parts) for _, parts in prepared),
            "model": self.provider.identity,
            "notes_policy": self.store.notes.model_dump(mode="json"),
            "speech_policy": self.store.speech.model_dump(mode="json"),
            "prompt_template": render(self.store.notes, ""),
            "request_shape": {
                key: value
                for key, value in request_body(
                    Chunk(0, (), ""), self.store.notes, str(self.provider.identity["alias"])
                ).items()
                if key != "messages"
            },
        }
        self.store.attempt({**base, "state": AttemptState.RUNNING.value})
        failures = 0
        try:
            for window, parts in prepared:
                failures += self._window(window, parts)
            if failures:
                last = self.unresolved[-1]
                detail = last["reason"] if isinstance(last, dict) else "model failure"
                raise SetupError(
                    f"extraction has {failures} failed chunks; attempt {self.attempt_id}; "
                    f"{detail}; verified chunks retained"
                )
            state = AttemptState.SUCCESS
            return self.report()
        except KeyboardInterrupt:
            state = AttemptState.INTERRUPTED
            raise
        finally:
            failure = sys.exception()
            self.store.attempt(
                {
                    **base,
                    **self.report(),
                    "state": state.value,
                    "ended_at": datetime.now(UTC).isoformat(),
                    "failed_chunks": failures,
                    "error_category": type(failure).__name__ if failure else None,
                    "error_message": str(failure) if isinstance(failure, SetupError) else None,
                }
            )

    def report(self) -> dict[str, JsonValue]:
        """Content-free outcome suitable for stdout and separate attempt state."""
        return {
            "mode": "apply",
            "archive_verified": True,
            "notes_generated": self.created > 0,
            "attempt": self.attempt_id,
            "run": self.run_key,
            "model_calls": self.calls,
            "created_chunks": self.created,
            "reused_chunks": self.reused,
            "notes": self.notes,
            "flagged_notes": self.flagged,
            "model_failures": list(self.model_failures),
            "unresolved_chunks": list(self.unresolved),
        }

    def _window(self, window: Window, chunks: tuple[Chunk, ...]) -> int:
        retry: list[Chunk] = []
        for chunk in chunks:
            try:
                self._chunk(window, chunk)
            except GenerationFailed as exc:
                self._failure(window, chunk, exc, retry=False)
                retry.append(chunk)
        failed = 0
        for chunk in retry:
            try:
                self._chunk(window, chunk)
            except GenerationFailed as exc:
                self._failure(window, chunk, exc, retry=True)
                failed += 1
        return failed

    def _failure(
        self, window: Window, chunk: Chunk, failure: GenerationFailed, *, retry: bool
    ) -> None:
        # These errors contain only our bounded diagnostics, never HTTP bodies or model text.
        record: dict[str, JsonValue] = {
            "window": window.report()["key"],
            "chunk": chunk.index,
            "retry": retry,
            "reason": str(failure),
        }
        self.model_failures.append(record)
        if retry:
            self.unresolved.append(record)

    def _chunk(self, window: Window, chunk: Chunk) -> None:
        self.provider.verify_model()
        elapsed = 0.0
        selector = self.store.selector(window, chunk, self.run_key)
        stored = self.store.load(selector, chunk)
        if stored is not None:
            self.reused += 1
        else:
            response, elapsed = self._generate(chunk)
            created = self.store.save(selector, chunk, response, self.attempt_id)
            self.created += int(created)
            self.reused += int(not created)
            stored = self.store.load(selector, chunk)
            if stored is None:
                raise SetupError("verified notes disappeared before progress could be counted")
        notes = stored["notes"]
        self.notes += len(notes)
        self.flagged += sum(bool(note["problems"]) for note in notes)
        if elapsed > self.store.config.slow_chunk_seconds:
            raise SlowChunk("notes worker exceeded slow-call limit; verified chunks retained")

    def _generate(self, chunk: Chunk) -> tuple[Response, float]:
        config = self.store.config
        if self.calls and config.pause_every and self.calls % config.pause_every == 0:
            time.sleep(config.pause_seconds)
        started = time.monotonic()
        self.calls += 1
        try:
            body = request_body(chunk, self.store.notes, str(self.provider.identity["alias"]))
            content = self.provider.generate(body)
            response = validate_response(content, self.store.notes, self.store.speech)
        except SetupError as exc:
            if time.monotonic() - started > config.slow_chunk_seconds:
                raise SlowChunk(
                    "notes worker exceeded slow-call limit; verified chunks retained"
                ) from exc
            raise GenerationFailed(str(exc)) from exc
        return response, time.monotonic() - started
