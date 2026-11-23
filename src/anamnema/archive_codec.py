"""Lossless source representation, adapted from transcript compression/recovery."""

import json
import lzma
from typing import Any

from bson import BSON, Binary
from bson.errors import InvalidDocument
from pydantic import JsonValue, TypeAdapter, ValidationError

from anamnema.archive_models import Kind, Record, canonical
from anamnema.ingest_config import ArchiveSettings
from anamnema.setup_config import SetupError

JSON_SOURCE = TypeAdapter(dict[str, JsonValue])
RECORD_KIND = TypeAdapter(Kind)


def recover_record(document: dict[str, Any], settings: ArchiveSettings) -> Record:
    """Recompute the normalized envelope and raw-source digest for any archived record kind."""
    try:
        raw = canonical(decode_source(document, settings))
        if len(raw) > settings.max_file_bytes:
            raise SetupError("source record exceeds configured byte limit")
        source = JSON_SOURCE.validate_json(raw)
        line = document["source_line"]
        if (
            type(line) is not int
            or line < 1
            or not all(
                isinstance(document[key], str) and document[key]
                for key in ("instance", "substrate", "session", "timestamp")
            )
        ):
            raise ValueError("invalid record envelope")
        record = Record(
            document["instance"],
            document["substrate"],
            document["session"],
            RECORD_KIND.validate_python(document["kind"], strict=True),
            document["timestamp"],
            line,
            source,
        )
        if canonical(record.core()) != canonical({key: document.get(key) for key in record.core()}):
            raise ValueError("record digest differs")
        return record
    except SetupError:
        raise
    except (KeyError, TypeError, ValueError, ValidationError) as exc:
        raise SetupError("archived source is corrupt; no automatic repair") from exc


def encode_record(record: Record, settings: ArchiveSettings) -> dict[str, Any]:
    """Keep JSON whole; compress oversized or non-BSON-native JSON losslessly."""
    document: dict[str, Any] = {
        **record.core(),
        "source_line": record.source_line,
        "source": record.source,
    }
    try:
        encoded_size = len(BSON.encode(document))
    except InvalidDocument, OverflowError:
        # For example, JSON integers wider than BSON's signed 64-bit integer.
        encoded_size = None
    if encoded_size is not None and encoded_size <= settings.max_document_bytes:
        return document
    document.pop("source")
    document["source_lzma"] = Binary(
        lzma.compress(canonical(record.source), preset=settings.compression_preset)
    )
    if len(BSON.encode(document)) > settings.max_document_bytes:
        raise SetupError("source remains oversized after lossless compression; no truncation")
    return document


def decode_source(document: dict[str, Any], settings: ArchiveSettings) -> Any:
    """Bound decompression and reject truncation, trailing data and invalid JSON."""
    if "source_lzma" not in document:
        return document.get("source")
    try:
        decoder = lzma.LZMADecompressor()
        raw = decoder.decompress(document["source_lzma"], max_length=settings.max_file_bytes + 1)
        if len(raw) > settings.max_file_bytes or not decoder.eof or decoder.unused_data:
            raise SetupError("compressed source violates archive bounds")
        return json.loads(raw)
    except (lzma.LZMAError, ValueError, TypeError) as exc:
        raise SetupError("compressed archive source cannot be recovered") from exc
