"""Explicit archive-verified localhost extraction; never runs in a dry-run."""

import argparse

from pydantic import JsonValue

from anamnema.archive_access import connect, keyring_secret, keyring_uri
from anamnema.archive_models import Snapshot
from anamnema.archive_mongo import MongoArchive
from anamnema.archive_mongo import chunks as archive_batches
from anamnema.chunks import Chunk
from anamnema.extraction_config import extraction_config
from anamnema.extraction_store import ExtractionStore
from anamnema.extractor import Extractor
from anamnema.ingest_config import ArchiveSettings
from anamnema.llama_provider import LlamaProvider
from anamnema.notes_policy import NotesPolicy
from anamnema.processing import Window
from anamnema.setup_config import SetupError, load_config
from anamnema.speech_policy import SpeechPolicy


def apply(
    args: argparse.Namespace,
    snapshot: Snapshot,
    prepared: list[tuple[Window, tuple[Chunk, ...]]],
    archive: ArchiveSettings,
    notes: NotesPolicy,
    speech: SpeechPolicy,
) -> dict[str, JsonValue]:
    """Verify archived source first, then use explicit model identity and separate results."""
    setup = load_config(args.config)
    if setup.backend != "vulkan":
        raise SetupError("extraction currently supports the local Vulkan llama.cpp route only")
    config = extraction_config(args.extraction_config)
    if {config.chunks_collection, config.attempts_collection} & set(archive.collections.values()):
        raise SetupError("extraction collections must not overlap archive collections")
    if args.model_file.is_symlink() or not args.model_file.is_file():
        raise SetupError("extraction requires an explicit regular model file")
    client = connect(keyring_uri(setup, archive), archive, setup.services.archive)
    try:
        database = client[archive.database]
        stored = MongoArchive(database, archive)
        for batch in archive_batches(list(snapshot.records), archive.batch_size):
            stored.verify(batch)
        if not any(parts for _, parts in prepared):
            return {
                "mode": "apply",
                "archive_verified": True,
                "notes_generated": False,
                "model_calls": 0,
                "created_chunks": 0,
                "reused_chunks": 0,
                "notes": 0,
                "flagged_notes": 0,
            }
        token = None
        if config.model_keyring_entry is not None and config.model_keyring_field is not None:
            token = keyring_secret(
                setup, archive, config.model_keyring_entry, config.model_keyring_field
            )
        provider = LlamaProvider(setup.services.notes, config, args.model_file, token)
        results = ExtractionStore(database, config, notes, speech)
        return Extractor(provider, results).run(prepared)
    finally:
        client.close()
