"""Owned immutable snapshot insertion and exact independent Mongo read-back."""

from __future__ import annotations

import hashlib
import re
from datetime import datetime
from enum import StrEnum
from importlib.resources import files
from pathlib import Path
from typing import Any

from bson import BSON
from pydantic import Field, ValidationError, model_validator
from pymongo.errors import PyMongoError

from anamnema.archive_models import canonical, digest
from anamnema.archive_mongo import MongoArchive
from anamnema.setup_config import Nonempty, SetupError, StrictModel


class SnapshotArchivePolicy(StrictModel):
    """No alternate database, secret or private host is embedded in snapshot policy."""

    collection: Nonempty
    batch_size: int = Field(gt=0)
    pair_batch_size: int = Field(gt=0)
    pair_window_seconds: int = Field(gt=0)
    max_summaries: int = Field(gt=0)
    max_pair_bytes: int = Field(gt=0)

    @model_validator(mode="after")
    def safe_collection(self) -> SnapshotArchivePolicy:
        """Keep collection selection distinct from arbitrary Mongo command syntax."""
        if re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*", self.collection) is None:
            raise ValueError("invalid snapshot collection name")
        return self


def snapshot_archive_policy(path: Path | None = None) -> SnapshotArchivePolicy:
    """Load a complete packaged or explicit policy without connecting to the archive."""
    try:
        raw = (
            path.read_text(encoding="utf-8")
            if path
            else files("anamnema")
            .joinpath("config/snapshot_archive.json")
            .read_text(encoding="utf-8")
        )
        return SnapshotArchivePolicy.model_validate_json(raw)
    except (OSError, ValidationError) as exc:
        raise SetupError(f"invalid snapshot archive policy ({type(exc).__name__})") from exc


class PairStatus(StrEnum):
    """Source pairing states, distinct from operational command success/failure."""

    PENDING = "pending"
    PAIRED = "paired"
    AMBIGUOUS = "ambiguous"
    UNPAIRED = "unpaired"


class MongoSnapshots:
    """Snapshots are immutable archive events, never reclassified as native dialogue."""

    def __init__(self, archive: MongoArchive, policy: SnapshotArchivePolicy) -> None:
        if policy.collection in archive.settings.collections.values():
            raise SetupError("snapshot collection must differ from native archive collections")
        self.archive, self.policy = archive, policy

    @staticmethod
    def document(instance: str, session: str, text: str, written_at: str) -> dict[str, Any]:
        """Scope the content identity by assistant and session; keep first-write provenance."""
        hashed = hashlib.sha256(text.encode("utf-8")).hexdigest()
        return {
            "_id": digest(["snapshot", instance, session, hashed]),
            "instance": instance,
            "session": session,
            "substrate": "claude",
            "sha256": hashed,
            "snapshot_digest": hashlib.sha256(text.rstrip().encode("utf-8")).hexdigest(),
            "text": text,
            "written_at": written_at,
            "pair_status": PairStatus.PENDING,
        }

    def ensure_schema(self) -> None:
        """Only apply creates indexes for the real owned lookup paths."""
        try:
            self.archive.database[self.policy.collection].create_index(
                [("instance", 1), ("session", 1), ("pair_status", 1)]
            )
            self.archive.database[self.archive.settings.collections["summary"]].create_index(
                [("instance", 1), ("session", 1)]
            )
        except PyMongoError as exc:
            raise SetupError(
                f"snapshot archive indexes unavailable ({type(exc).__name__})"
            ) from exc

    def keep(self, document: dict[str, Any]) -> bool:
        """Never overwrite an earlier source, even if its body or owner has become corrupt."""
        if len(BSON.encode(document)) > self.archive.settings.max_document_bytes:
            raise SetupError("snapshot exceeds archive document bound; local file retained")
        try:
            result = self.archive.database[self.policy.collection].update_one(
                {"_id": document["_id"]},
                {"$setOnInsert": document},
                upsert=True,
            )
            return result.upserted_id is not None
        except PyMongoError as exc:
            raise SetupError(
                f"snapshot archive write failed ({type(exc).__name__}); safe to retry"
            ) from exc

    def verify(self, document: dict[str, Any], inserted: bool) -> None:
        """Re-read source and ownership, not merely cached hashes or write acknowledgements."""
        try:
            stored = self.archive.database[self.policy.collection].find_one(
                {"_id": document["_id"]}
            )
        except PyMongoError as exc:
            raise SetupError(
                f"snapshot read-back failed ({type(exc).__name__}); local file retained"
            ) from exc
        keys = ("_id", "instance", "session", "substrate", "text", "sha256", "snapshot_digest")
        try:
            matches = stored is not None and canonical(
                {k: stored.get(k) for k in keys}
            ) == canonical({k: document[k] for k in keys})
        except (TypeError, ValueError) as exc:
            raise SetupError(
                "snapshot read-back has malformed fields; local file retained"
            ) from exc
        if not matches or stored is None:
            raise SetupError("snapshot read-back content or ownership differs; local file retained")
        if hashlib.sha256(stored["text"].encode("utf-8")).hexdigest() != document["sha256"]:
            raise SetupError("snapshot read-back hash differs; local file retained")
        try:
            timestamp = datetime.fromisoformat(stored["written_at"])
        except (KeyError, TypeError, ValueError) as exc:
            raise SetupError("snapshot archive timestamp is invalid; local file retained") from exc
        if timestamp.tzinfo is None or (
            inserted and stored["written_at"] != document["written_at"]
        ):
            raise SetupError("snapshot archive timestamp differs; local file retained")
