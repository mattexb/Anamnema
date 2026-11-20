"""A neutralized Secret Service lookup and explicit localhost-only Mongo connector."""

from __future__ import annotations

import os
import subprocess
from typing import Any

from pymongo import MongoClient
from pymongo.errors import PyMongoError
from pymongo.uri_parser import parse_uri

from anamnema.ingest_config import ArchiveSettings
from anamnema.setup_config import Endpoint, SetupConfig, SetupError


def keyring_uri(setup: SetupConfig, settings: ArchiveSettings) -> str:
    """Resolve the configured archive URI through the shared project lookup."""
    return keyring_secret(setup, settings, settings.keyring_entry, settings.keyring_field)


def keyring_secret(setup: SetupConfig, settings: ArchiveSettings, entry: str, field: str) -> str:
    """Reuse service/entry/field attributes; never log command output or secret values."""
    environment = dict(os.environ)
    if not environment.get(settings.bus_env):
        environment[settings.bus_env] = settings.bus_fallback.format(uid=os.getuid())
    command = [
        setup.keyring.executable,
        "lookup",
        "service",
        setup.keyring.namespace,
        "entry",
        entry,
        "field",
        field,
    ]
    try:
        result = subprocess.run(  # noqa: S603 - configured executable, fixed operation, no shell
            command,
            env=environment,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=settings.keyring_timeout_seconds,
            check=False,
        )
    except (OSError, UnicodeError, subprocess.TimeoutExpired) as exc:
        raise SetupError(f"project keyring lookup failed ({type(exc).__name__})") from exc
    if result.returncode or not result.stdout.strip():
        raise SetupError("project credential unavailable; check keyring entry/unlock")
    return result.stdout.strip()


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
