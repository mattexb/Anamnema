"""Non-destructive file import under the single Anamnema command."""

import argparse

from anamnema.archive_access import connect, keyring_uri
from anamnema.archive_mongo import MongoArchive, import_snapshot
from anamnema.ingest_config import archive_settings
from anamnema.setup_config import load_config
from anamnema.substrates.claude import ClaudeAdapter


def run_import(args: argparse.Namespace) -> dict[str, object]:
    """Prepare fully before any secret lookup or archive connection."""
    settings = archive_settings(args.archive_config)
    snapshot = ClaudeAdapter(settings).read(
        args.file, args.instance, args.session, closed=args.closed
    )
    if not args.apply:
        return {**snapshot.summary(), "mode": "dry-run", "verified": False}
    setup = load_config(args.config)
    with connect(keyring_uri(setup, settings), settings, setup.services.archive) as client:
        return import_snapshot(snapshot, MongoArchive(client[settings.database], settings))
