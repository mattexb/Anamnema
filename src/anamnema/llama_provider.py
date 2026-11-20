"""Single-machine llama.cpp transport, exact token preflight and model-file identity."""

from __future__ import annotations

import hashlib
import http.client
import os
from pathlib import Path

from pydantic import JsonValue, TypeAdapter, ValidationError

from anamnema.archive_models import canonical
from anamnema.extraction_config import ExtractionConfig
from anamnema.setup_config import Endpoint, SetupError

OBJECT = TypeAdapter(dict[str, JsonValue])


class LlamaProvider:
    """Only the reviewed local native llama.cpp route; no proxy or redirect handling."""

    def __init__(
        self,
        endpoint: Endpoint,
        config: ExtractionConfig,
        model_file: Path,
        token: str | None = None,
    ) -> None:
        self.endpoint = endpoint
        self.config = config
        self._headers = {"Content-Type": "application/json"}
        if token:
            if any(not 33 <= ord(character) <= 126 for character in token):
                raise SetupError("model credential has invalid bearer-header characters")
            self._headers["Authorization"] = f"Bearer {token}"
        self._model_file = model_file
        self._model_stamp = self._stamp_file()
        self.identity = self._identify(model_file)
        if self._stamp_file() != self._model_stamp:
            raise SetupError("model file changed during provider identification")

    def _identify(self, path: Path) -> dict[str, JsonValue]:
        hashed = hash_model(path, self.config)
        return self._properties(path, hashed)

    def _properties(self, path: Path, hashed: str) -> dict[str, JsonValue]:
        props = self.call("GET", self.config.props_path)
        model_path, alias = props.get("model_path"), props.get("model_alias")
        generation = props.get("default_generation_settings")
        if (
            not isinstance(model_path, str)
            or not isinstance(alias, str)
            or not alias
            or props.get("build_info") is None
        ):
            raise SetupError("llama.cpp properties lack model identity")
        try:
            matches = Path(model_path).resolve(strict=True) == path.resolve(strict=True)
        except (OSError, ValueError) as exc:
            raise SetupError("llama.cpp model path cannot be verified") from exc
        if not matches:
            raise SetupError("llama.cpp loaded model path differs from the explicit model file")
        context = generation.get("n_ctx") if isinstance(generation, dict) else None
        if type(context) is not int or context <= 0:
            raise SetupError("llama.cpp properties lack a valid context bound")
        return {
            "sha256": hashed,
            "alias": alias,
            "build": props.get("build_info"),
            "generation": generation,
            "context_tokens": context,
        }

    def _stamp_file(self) -> tuple[int, int, int, int]:
        try:
            if self._model_file.is_symlink():
                raise SetupError("model file must not become a symlink")
            return _stamp(self._model_file.stat())
        except OSError as exc:
            raise SetupError(f"model identity cannot be checked ({type(exc).__name__})") from exc

    def verify_model(self) -> None:
        """Refuse file/metadata changes during a run rather than mixing model identities."""
        if self._stamp_file() != self._model_stamp:
            raise SetupError("model file changed during extraction")
        current = self._properties(self._model_file, str(self.identity["sha256"]))
        if (
            canonical(current) != canonical(self.identity)
            or self._stamp_file() != self._model_stamp
        ):
            raise SetupError("llama.cpp model identity changed during extraction")

    def generate(self, body: dict[str, JsonValue]) -> str:
        """Count the actual templated prompt before generation, then require a stopped reply."""
        self.verify_model()
        messages = body.get("messages")
        templated = self.call("POST", self.config.template_path, {"messages": messages})
        prompt = templated.get("prompt")
        if not isinstance(prompt, str):
            raise SetupError("llama.cpp template response is invalid")
        counted = self.call(
            "POST", self.config.tokenize_path, {"content": prompt, "parse_special": True}
        )
        tokens = counted.get("tokens")
        answer_limit = body.get("max_tokens")
        context = self.identity["context_tokens"]
        if not isinstance(tokens, list) or not all(type(token) is int for token in tokens):
            raise SetupError("llama.cpp token response is invalid")
        if (
            type(answer_limit) is not int
            or type(context) is not int
            or len(tokens) + answer_limit > context
        ):
            raise SetupError("templated prompt and answer exceed the model context")
        answer = self.call("POST", self.config.chat_path, body)
        choices = answer.get("choices")
        choice = choices[0] if isinstance(choices, list) and len(choices) == 1 else None
        message = choice.get("message") if isinstance(choice, dict) else None
        content = message.get("content") if isinstance(message, dict) else None
        if (
            not isinstance(choice, dict)
            or choice.get("finish_reason") != "stop"
            or not isinstance(content, str)
        ):
            raise SetupError("llama.cpp did not return one complete text answer")
        self.verify_model()
        return content

    def call(
        self, method: str, path: str, body: dict[str, JsonValue] | None = None
    ) -> dict[str, JsonValue]:
        """Bound both directions and never expose response/error bodies in diagnostics."""
        try:
            data = canonical(body) if body is not None else None
        except (ValueError, TypeError) as exc:
            raise SetupError("model request is not valid finite JSON") from exc
        if data is not None and len(data) > self.config.max_request_bytes:
            raise SetupError("model request exceeds configured transport bound")
        connection = http.client.HTTPConnection(
            self.endpoint.host, self.endpoint.port, timeout=self.config.timeout_seconds
        )
        try:
            connection.request(method, path, body=data, headers=self._headers)
            response = connection.getresponse()
            if response.status != 200:
                raise SetupError(f"llama.cpp request failed (HTTP {response.status})")
            raw = response.read(self.config.max_response_bytes + 1)
            if len(raw) > self.config.max_response_bytes:
                raise SetupError("model response exceeds configured transport bound")
            result = OBJECT.validate_json(raw)
            canonical(result)
            return result
        except SetupError:
            raise
        except (OSError, http.client.HTTPException, ValidationError, ValueError) as exc:
            raise SetupError(f"llama.cpp transport failed ({type(exc).__name__})") from exc
        finally:
            connection.close()


def hash_model(path: Path, config: ExtractionConfig) -> str:
    """Hash a stable explicit local file without reading it all into memory."""
    try:
        if path.is_symlink() or not path.is_file():
            raise SetupError("model must be an explicit regular nonsymlink file")
        hashed = hashlib.sha256()
        with path.open("rb") as stream:
            before = os.fstat(stream.fileno())
            if not 0 < before.st_size <= config.max_model_bytes:
                raise SetupError("model file violates configured size bounds")
            total = 0
            while block := stream.read(config.hash_block_bytes):
                total += len(block)
                if total > config.max_model_bytes:
                    raise SetupError("model file grew beyond configured size bounds")
                hashed.update(block)
            after = os.fstat(stream.fileno())
            named = path.stat()
        if _stamp(before) != _stamp(after) or _stamp(after) != _stamp(named) or path.is_symlink():
            raise SetupError("model file changed while hashing")
        return hashed.hexdigest()
    except OSError as exc:
        raise SetupError(f"model file cannot be read ({type(exc).__name__})") from exc


def _stamp(stat: os.stat_result) -> tuple[int, int, int, int]:
    return stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns
