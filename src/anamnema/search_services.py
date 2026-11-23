"""Bounded localhost encoder/reranker clients preserving tested model contracts."""

from __future__ import annotations

import http.client
import math
from importlib.resources import files
from pathlib import Path
from typing import Annotated, Literal

from pydantic import Field, JsonValue, TypeAdapter, ValidationError, model_validator

from anamnema.archive_models import canonical
from anamnema.memory_models import Piece, Rewrite
from anamnema.readiness import ServiceNotReady
from anamnema.setup_config import Endpoint, Nonempty, SetupError, StrictModel

Positive = Annotated[int, Field(gt=0)]
Route = Annotated[str, Field(pattern=r"^/[A-Za-z0-9_./-]+$")]
OBJECT = TypeAdapter(dict[str, JsonValue])
TOKENS = TypeAdapter(list[Annotated[int, Field(strict=True)]])


class SearchServices(StrictModel):
    """Connection-independent CPU model policy and local credential selectors."""

    schema_version: Literal[1]
    timeout_seconds: Annotated[float, Field(gt=0, allow_inf_nan=False)]
    rerank_timeout_seconds: Annotated[float, Field(gt=0, allow_inf_nan=False)]
    max_request_bytes: Positive
    max_response_bytes: Positive
    embeddings_path: Route
    tokenize_path: Route
    detokenize_path: Route
    rerank_path: Route
    encoder_model: Nonempty
    dimensions: Positive
    query_prefix: Nonempty
    passage_prefix: Nonempty
    input_characters: Positive
    chunk_overlap: Annotated[int, Field(ge=0)]
    max_tokens: Positive
    cut_attempts: Positive
    batch: Positive
    max_documents: Positive
    encoder_keyring_entry: Nonempty
    reranker_keyring_entry: Nonempty
    token_field: Nonempty

    @model_validator(mode="after")
    def progressing_pieces(self) -> SearchServices:
        """An overlap must leave a positive step through a long note."""
        if self.chunk_overlap >= self.input_characters:
            raise ValueError("passage overlap must be smaller than its size")
        return self


def load_search_services(path: Path | None = None) -> SearchServices:
    """Load explicit policy or the packaged neutral model defaults."""
    try:
        text = (
            path.read_text(encoding="utf-8")
            if path
            else files("anamnema")
            .joinpath("config/search_services.json")
            .read_text(encoding="utf-8")
        )
        return SearchServices.model_validate_json(text)
    except (OSError, UnicodeError, ValidationError) as exc:
        raise SetupError(f"invalid search service policy ({type(exc).__name__})") from exc


class LocalJSON:
    """No environment proxy, redirects, remote address or raw response diagnostics."""

    def __init__(
        self, endpoint: Endpoint, policy: SearchServices, token: str, timeout: float
    ) -> None:
        if not token or any(not 33 <= ord(c) <= 126 for c in token):
            raise SetupError("search service needs a valid project bearer credential")
        self.endpoint, self.policy, self.timeout = endpoint, policy, timeout
        self.headers = {"Content-Type": "application/json", "Authorization": f"Bearer {token}"}

    def call(self, path: str, body: dict[str, JsonValue]) -> dict[str, JsonValue]:
        """Bound both transport directions and reject malformed/nonfinite JSON."""
        data = canonical(body)
        if len(data) > self.policy.max_request_bytes:
            raise SetupError("search service request exceeds configured bound")
        connection = http.client.HTTPConnection(
            self.endpoint.host, self.endpoint.port, timeout=self.timeout
        )
        try:
            connection.request("POST", path, data, self.headers)
            with connection.getresponse() as response:
                if response.status != 200:
                    if response.status == 503:
                        raise ServiceNotReady("search service is not ready (HTTP 503)")
                    raise SetupError(f"search service rejected request (HTTP {response.status})")
                raw = response.read(self.policy.max_response_bytes + 1)
                if len(raw) > self.policy.max_response_bytes:
                    raise SetupError("search service response exceeds configured bound")
            answer = OBJECT.validate_json(raw)
            canonical(answer)
            return answer
        except (ConnectionError, TimeoutError, http.client.RemoteDisconnected) as exc:
            raise ServiceNotReady(f"search service call failed ({type(exc).__name__})") from exc
        except (OSError, http.client.HTTPException, ValidationError, ValueError) as exc:
            if isinstance(exc, SetupError):
                raise
            raise SetupError(f"search service call failed ({type(exc).__name__})") from exc
        finally:
            connection.close()


class CPUEncoder:
    """Native llama.cpp e5 encoder with its own exact tokenizer preflight."""

    def __init__(self, connection: LocalJSON) -> None:
        self.connection = connection
        self.policy = connection.policy

    def _tokens(self, text: str, special: bool) -> list[int]:
        result = self.connection.call(
            self.policy.tokenize_path, {"content": text, "add_special": special}
        )
        try:
            return TOKENS.validate_python(result.get("tokens"), strict=True)
        except ValidationError as exc:
            raise SetupError("encoder tokenizer returned invalid tokens") from exc

    def within_tokens(self, text: str) -> str:
        """Preserve the source's bounded head trimming and re-count after detokenization."""
        total = len(self._tokens(text, True))
        if total <= self.policy.max_tokens:
            return text
        plain = self._tokens(text, False)
        keep = self.policy.max_tokens - (total - len(plain))
        for _ in range(self.policy.cut_attempts):
            if keep <= 0:
                break
            answer = self.connection.call(
                self.policy.detokenize_path, {"tokens": [i for i in plain[:keep]]}
            )
            cut = answer.get("content")
            if not isinstance(cut, str):
                raise SetupError("encoder detokenizer returned invalid content")
            count = len(self._tokens(cut, True))
            if count <= self.policy.max_tokens:
                return cut
            keep -= count - self.policy.max_tokens
        raise SetupError("encoder input could not fit its configured token limit")

    def encode(
        self, texts: list[str], passage: bool, *, subjects: list[str] | None = None
    ) -> tuple[tuple[float, ...], ...]:
        """Return one finite nonzero dimension-checked vector for each input, by index."""
        vectors: list[tuple[float, ...]] = []
        if subjects is not None and (not passage or len(subjects) != len(texts)):
            raise SetupError("subject embeddings require one subject per passage")
        prefix = self.policy.passage_prefix if passage else self.policy.query_prefix
        for start in range(0, len(texts), self.policy.batch):
            batch = texts[start : start + self.policy.batch]
            prepared = [
                self.within_tokens(
                    prefix
                    + (subjects[start + index] + ". " if subjects is not None else "")
                    + text[: self.policy.input_characters]
                )
                for index, text in enumerate(batch)
            ]
            answer = self.connection.call(
                self.policy.embeddings_path,
                {"model": self.policy.encoder_model, "input": [t for t in prepared]},
            )
            rows = answer.get("data")
            found: dict[int, tuple[float, ...]] = {}
            if not isinstance(rows, list):
                raise SetupError("encoder returned no indexed vectors")
            for row in rows:
                if not isinstance(row, dict):
                    raise SetupError("encoder returned an invalid vector row")
                index, embedding = row.get("index"), row.get("embedding")
                if type(index) is not int or index in found or not isinstance(embedding, list):
                    raise SetupError("encoder returned duplicate or invalid vector indexes")
                normalized = tuple(number(x) for x in embedding)
                if len(normalized) != self.policy.dimensions or not any(normalized):
                    raise SetupError(
                        "encoder returned invalid dimensions or nonfinite/zero vectors"
                    )
                found[index] = normalized
            if sorted(found) != list(range(len(batch))):
                raise SetupError("encoder vector indexes differ from requested inputs")
            vectors.extend(found[i] for i in range(len(batch)))
        return tuple(vectors)

    def embed_note(self, text: str) -> Rewrite:
        """Build the source's query/passage pair and later overlapping passage pieces."""
        size, overlap = self.policy.input_characters, self.policy.chunk_overlap
        offsets: list[int] = []
        covered = size
        while covered < len(text):
            offsets.append(covered - overlap)
            covered = offsets[-1] + size
        vectors = self.encode([text[offset : offset + size] for offset in offsets], passage=True)
        return Rewrite(
            query_vector=self.encode([text], passage=False)[0],
            passage_vector=self.encode([text], passage=True)[0],
            embedded_with=self.policy.encoder_model,
            pieces=tuple(
                Piece(offset=offset, vector=vector)
                for offset, vector in zip(offsets, vectors, strict=True)
            ),
        )


class CPUReranker:
    """Map the ONNX server's raw logits by result index, never by response position."""

    def __init__(self, connection: LocalJSON) -> None:
        self.connection = connection

    def scores(self, question: str, documents: list[str]) -> list[float]:
        """Require exactly one finite raw logit for every requested document."""
        if not 1 <= len(documents) <= self.connection.policy.max_documents:
            raise SetupError("reranker document count violates configured limit")
        answer = self.connection.call(
            self.connection.policy.rerank_path,
            {"query": question, "documents": [d for d in documents]},
        )
        results = answer.get("results")
        if not isinstance(results, list):
            raise SetupError("reranker returned no indexed scores")
        found: dict[int, float] = {}
        for result in results:
            if not isinstance(result, dict):
                raise SetupError("reranker returned an invalid score row")
            index, score = result.get("index"), result.get("relevance_score")
            if type(index) is not int or index in found or type(score) not in (float, int):
                raise SetupError("reranker returned duplicate or invalid score indexes")
            found[index] = number(score)
        if sorted(found) != list(range(len(documents))):
            raise SetupError("reranker score indexes differ from requested documents")
        return [found[index] for index in range(len(documents))]


def number(value: JsonValue) -> float:
    """Only finite JSON numbers may enter a vector or raw-logit score."""
    if isinstance(value, bool) or not isinstance(value, (float, int)) or not math.isfinite(value):
        raise SetupError("search service returned a nonfinite or nonnumeric value")
    return float(value)
