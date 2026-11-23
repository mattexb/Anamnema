"""Project-packaged neutral CPU ONNX reranker, adapted from the tested native service."""

from __future__ import annotations

import importlib
import math
import threading
from importlib.resources import files
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import Field, ValidationError

from anamnema.memory_models import MemoryInputError
from anamnema.search_services import OBJECT
from anamnema.setup_config import Nonempty, SetupError, StrictModel


class OnnxSettings(StrictModel):
    """Model files and CPU limits, without addresses or credential values."""

    schema_version: Literal[1]
    model_alias: Nonempty
    onnx_file: Nonempty
    tokenizer_file: Nonempty
    special_tokens_file: Nonempty
    max_length: Annotated[int, Field(gt=0)]
    threads: Annotated[int, Field(gt=0)]
    cpu_mem_arena: bool
    max_documents: Annotated[int, Field(gt=0)]
    max_query_characters: Annotated[int, Field(gt=0)]
    max_document_characters: Annotated[int, Field(gt=0)]
    pad_token_name: Nonempty


def load_onnx(path: Path | None = None) -> OnnxSettings:
    """Load the neutral tested model family; alternative models are not auto-selected."""
    try:
        text = (
            path.read_text(encoding="utf-8")
            if path
            else files("anamnema").joinpath("config/reranker.json").read_text(encoding="utf-8")
        )
        return OnnxSettings.model_validate_json(text)
    except (OSError, UnicodeError, ValidationError) as exc:
        raise SetupError(f"invalid ONNX configuration ({type(exc).__name__})") from exc


class ONNXRanker:
    """One CPU session; tokenizer and model use are serialized together per service."""

    def __init__(self, directory: Path, settings: OnnxSettings) -> None:
        self.settings = settings
        self.lock = threading.Lock()
        try:
            # Optional native inference dependencies do not load on ordinary memory CLI import.
            self.np: Any = importlib.import_module("numpy")
            runtime: Any = importlib.import_module("onnxruntime")
            tokenizers: Any = importlib.import_module("tokenizers")
        except ImportError as exc:
            raise SetupError("CPU reranker requires the project's reranker extra") from exc
        state = importlib.import_module("onnxruntime.capi.onnxruntime_pybind11_state")
        self.errors: tuple[type[Exception], ...] = (
            OSError,
            ValueError,
            KeyError,
            TypeError,
            RuntimeError,
            state.Fail,
            state.InvalidArgument,
            state.NoSuchFile,
            state.InvalidProtobuf,
            state.InvalidGraph,
            state.NotImplemented,
            state.RuntimeException,
            state.EPFail,
        )
        try:
            special = OBJECT.validate_json(
                self._file(directory, settings.special_tokens_file).read_text(encoding="utf-8")
            )
            pad = special[settings.pad_token_name]
            pad = pad["content"] if isinstance(pad, dict) else pad
            if not isinstance(pad, str) or not pad:
                raise ValueError("missing pad token")
            self.tokenizer: Any = tokenizers.Tokenizer.from_file(
                str(self._file(directory, settings.tokenizer_file))
            )
            self.query_tokenizer: Any = tokenizers.Tokenizer.from_str(self.tokenizer.to_str())
            self.query_tokenizer.no_truncation()
            self.query_tokenizer.no_padding()
            self.tokenizer.no_padding()
            self.tokenizer.enable_truncation(settings.max_length, strategy="only_second")
            pad_id = self.tokenizer.token_to_id(pad)
            if type(pad_id) is not int:
                raise ValueError("unknown pad token")
            self.tokenizer.enable_padding(pad_id=pad_id, pad_token=pad)
            options = runtime.SessionOptions()
            options.intra_op_num_threads = settings.threads
            options.enable_cpu_mem_arena = settings.cpu_mem_arena
            self.session: Any = runtime.InferenceSession(
                str(self._file(directory, settings.onnx_file)),
                options,
                providers=["CPUExecutionProvider"],
            )
            self.inputs: set[str] = {value.name for value in self.session.get_inputs()}
            if not self.inputs or not self.inputs <= {
                "input_ids",
                "attention_mask",
                "token_type_ids",
            }:
                raise ValueError("incompatible model inputs")
        except self.errors as exc:
            raise SetupError(f"CPU reranker initialization failed ({type(exc).__name__})") from exc

    @staticmethod
    def _file(directory: Path, name: str) -> Path:
        root = directory.resolve(strict=True)
        path = (root / name).resolve(strict=True)
        if not path.is_relative_to(root) or not path.is_file():
            raise SetupError("reranker model files must remain inside the explicit model directory")
        return path

    def scores(self, question: str, documents: list[str]) -> list[float]:
        """Return raw logits without sigmoid/softmax; truncate the document, not the query."""
        s = self.settings
        if (
            not question
            or len(question) > s.max_query_characters
            or not 1 <= len(documents) <= s.max_documents
        ):
            raise MemoryInputError("reranker query/document count violates configured limits")
        if any(not document or len(document) > s.max_document_characters for document in documents):
            raise MemoryInputError("reranker document violates configured size limit")
        try:
            with self.lock:
                if len(self.query_tokenizer.encode(question).ids) >= s.max_length:
                    raise MemoryInputError("reranker query alone exceeds its token limit")
                encodings = self.tokenizer.encode_batch(
                    [(question, document) for document in documents]
                )
                feed = {
                    "input_ids": [e.ids for e in encodings],
                    "attention_mask": [e.attention_mask for e in encodings],
                    "token_type_ids": [e.type_ids for e in encodings],
                }
                inputs = {
                    name: self.np.asarray(values, dtype=self.np.int64)
                    for name, values in feed.items()
                    if name in self.inputs
                }
                logits = self.session.run(None, inputs)[0]
                values = self.np.asarray(logits).reshape(len(documents), -1)
                if values.shape != (len(documents), 1):
                    raise SetupError("reranker model returned an incompatible output shape")
                result = [float(row[0]) for row in values]
                if not all(math.isfinite(value) for value in result):
                    raise SetupError("reranker model returned nonfinite logits")
                return result
        except self.errors as exc:
            if isinstance(exc, SetupError):
                raise
            raise SetupError(f"CPU reranker inference failed ({type(exc).__name__})") from exc
