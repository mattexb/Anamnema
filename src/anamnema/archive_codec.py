"""Lossless source representation, adapted from transcript compression/recovery."""

import json
import lzma
from typing import Any

from bson import BSON, Binary
from bson.errors import InvalidDocument

from anamnema.archive_models import Record, canonical
from anamnema.ingest_config import ArchiveSettings
from anamnema.setup_config import SetupError


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
