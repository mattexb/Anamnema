"""Verified immutable archival frees terminal capture quota without discarding exact inputs."""

import re
from importlib.resources import files
from pathlib import Path
from typing import Any, Self

from bson import BSON
from pydantic import ValidationError, model_validator
from pymongo.errors import PyMongoError

from anamnema.archive_models import canonical, digest
from anamnema.archive_mongo import MongoArchive
from anamnema.capture_queue import CaptureJob, CaptureQueue
from anamnema.setup_config import Nonempty, SetupError, StrictModel


class CaptureArchivePolicy(StrictModel):
    """The archive shares the existing owned database, not a new credential or host."""

    collection: Nonempty

    @model_validator(mode="after")
    def safe_collection(self) -> Self:
        if re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*", self.collection) is None:
            raise ValueError("invalid capture failure collection name")
        return self


class FailedCapture(StrictModel):
    """Keep the full event pin, expectations, original config and terminal failure intact."""

    instance: Nonempty
    job: CaptureJob
    reason: Nonempty

    @model_validator(mode="after")
    def terminal(self) -> Self:
        if self.instance != self.job.selection.instance or self.job.error is None:
            raise ValueError("capture archive owner differs or request is not failed")
        return self

    @property
    def identity(self) -> str:
        return digest(
            [
                "capture-failure",
                self.instance,
                self.job.id,
                digest(self.job.model_dump(mode="json")),
            ]
        )

    @property
    def checksum(self) -> str:
        return digest(self.model_dump(mode="json"))


def capture_archive_policy(path: Path | None = None) -> CaptureArchivePolicy:
    try:
        raw = (
            path.read_text(encoding="utf-8")
            if path
            else files("anamnema")
            .joinpath("config/capture_archive.json")
            .read_text(encoding="utf-8")
        )
        return CaptureArchivePolicy.model_validate_json(raw)
    except (OSError, UnicodeError, ValidationError) as exc:
        raise SetupError("capture archive policy cannot be read or validated") from exc


class MongoCaptureFailures:
    """A write acknowledgement never substitutes for independent full-document verification."""

    def __init__(self, archive: MongoArchive, policy: CaptureArchivePolicy) -> None:
        if policy.collection in archive.settings.collections.values():
            raise SetupError("capture failure collection overlaps a native archive collection")
        self.archive, self.policy = archive, policy

    def ensure_schema(self) -> None:
        """Index the owner/job existence lookup only during an explicit archive operation."""
        try:
            self.archive.database[self.policy.collection].create_index(
                [("instance", 1), ("job.id", 1)]
            )
        except PyMongoError as exc:
            raise SetupError(f"capture archive index failed ({type(exc).__name__})") from exc

    def get(
        self, instance: str, job: str, expected: CaptureJob | None = None
    ) -> FailedCapture | None:
        """Verify an exact failure, or existence only when no expected version was supplied."""
        selector = (
            {
                "_id": digest(
                    ["capture-failure", instance, job, digest(expected.model_dump(mode="json"))]
                )
            }
            if expected is not None
            else {"instance": instance, "job.id": job}
        )
        try:
            row = self.archive.database[self.policy.collection].find_one(selector)
            if row is None:
                return None
            if len(BSON.encode(row)) > self.archive.settings.max_document_bytes:
                raise SetupError("capture archive document exceeds configured bound")
            body = {name: value for name, value in row.items() if name not in {"_id", "sha256"}}
            stored = FailedCapture.model_validate_json(canonical(body))
            if (
                stored.instance != instance
                or stored.job.id != job
                or stored.identity != row["_id"]
                or stored.checksum != row.get("sha256")
            ):
                raise SetupError("capture archive identity, ownership or checksum differs")
            return stored
        except PyMongoError as exc:
            raise SetupError(f"capture archive read failed ({type(exc).__name__})") from exc
        except SetupError:
            raise
        except (ValueError, TypeError, KeyError) as exc:
            raise SetupError(
                "capture archive contains malformed data; local request retained"
            ) from exc

    def keep(self, failure: FailedCapture) -> None:
        """First write is immutable; exact replay verifies the original bytes and reason."""
        document: dict[str, Any] = {
            "_id": failure.identity,
            "sha256": failure.checksum,
            **failure.model_dump(mode="json"),
        }
        if len(BSON.encode(document)) > self.archive.settings.max_document_bytes:
            raise SetupError("capture failure exceeds archive document bound; local input retained")
        try:
            self.archive.database[self.policy.collection].update_one(
                {"_id": failure.identity}, {"$setOnInsert": document}, upsert=True
            )
        except PyMongoError as exc:
            raise SetupError(f"capture archive write failed ({type(exc).__name__})") from exc
        if self.get(failure.instance, failure.job.id, failure.job) != failure:
            raise SetupError("capture archive read-back differs; local request retained")


def validate_archive_request(identity: str, reason: str, maximum_reason: int) -> None:
    """Refuse invalid operator input before keyring or network access, including previews."""
    if not reason.strip() or len(reason) > maximum_reason:
        raise SetupError("capture archival requires a bounded reason")
    if re.fullmatch(r"[a-f0-9]{32}", identity) is None:
        raise SetupError("invalid failed capture job identity")


def archive_capture_failure(
    queue: CaptureQueue, archive: MongoCaptureFailures, identity: str, reason: str
) -> dict[str, Any]:
    """Do network work outside the lock, then compare exact failed state before retiring it."""
    validate_archive_request(identity, reason, queue.policy.max_reason_characters)
    archive.ensure_schema()
    instance = queue.deployment.instance
    failed = next((job for job in queue.inspect().failed if job.id == identity), None)
    if failed is None:
        old = archive.get(instance, identity)
        if old is None:
            raise SetupError("capture archival must name a terminal request or its archived copy")
        return {"archived": True, "retired": False, "already_archived": True, "job": identity}
    document = FailedCapture(instance=instance, job=failed, reason=reason)
    archive.keep(document)
    retired = queue.retire(failed, document.checksum, reason)
    return {"archived": True, "retired": retired, "already_archived": False, "job": identity}
