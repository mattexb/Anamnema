"""A neutralized Secret Service lookup and explicit localhost-only Mongo connector."""

from __future__ import annotations

from typing import Any

from pymongo import MongoClient
from pymongo.errors import PyMongoError
from pymongo.uri_parser import parse_uri

from anamnema.ingest_config import ArchiveSettings
from anamnema.secret_service import SecretService
from anamnema.setup_config import Endpoint, SetupConfig, SetupError


def keyring_uri(setup: SetupConfig, settings: ArchiveSettings) -> str:
    """Resolve the configured archive URI through the shared project lookup."""
    return keyring_secret(setup, settings, settings.keyring_entry, settings.keyring_field)


def keyring_secret(setup: SetupConfig, settings: ArchiveSettings, entry: str, field: str) -> str:
    """Reuse service/entry/field attributes; never log command output or secret values."""
    return SecretService(setup.keyring, settings).lookup(entry, field)


def connect(
    uri: str, settings: ArchiveSettings, endpoint: Endpoint | None = None
) -> MongoClient[dict[str, Any]]:
    """Reject remote targets and URI-selected database changes; never print the URI."""
    try:
        if not uri.startswith("mongodb://"):
            raise SetupError("archive requires an explicit mongodb:// localhost target")
        parsed = parse_uri(uri)
        if any(host not in {"127.0.0.1", "localhost", "::1"} for host, _ in parsed["nodelist"]):
            raise SetupError("archive target must be on localhost")
        if len(parsed["nodelist"]) != 1 or parsed["options"].get("proxyHost"):
            raise SetupError("archive requires one direct local server, without a proxy")
        if endpoint is not None and parsed["nodelist"][0][1] != endpoint.port:
            raise SetupError("archive URI port differs from configured local service")
        if parsed["database"] not in (None, settings.database):
            raise SetupError("archive URI database differs from configured database")
        client: MongoClient[dict[str, Any]] = MongoClient(
            uri,
            directConnection=True,
            serverSelectionTimeoutMS=settings.timeout_ms,
            connectTimeoutMS=settings.timeout_ms,
            socketTimeoutMS=settings.timeout_ms,
        )
        try:
            client.admin.command("ping")
        except PyMongoError:
            client.close()
            raise
        return client
    except SetupError:
        raise
    except (PyMongoError, ValueError) as exc:
        raise SetupError(f"archive connection failed ({type(exc).__name__})") from exc
