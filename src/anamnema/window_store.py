"""Verified immutable references preserve captured window order without copying raw sources."""

from __future__ import annotations

from typing import Annotated, Any, Literal, Self

from bson import BSON
from bson.errors import InvalidDocument
from pydantic import Field, JsonValue, ValidationError, model_validator
from pymongo.errors import PyMongoError

from anamnema.archive_models import canonical, digest
from anamnema.archive_mongo import MongoArchive
from anamnema.processing import Window
from anamnema.setup_config import Nonempty, SetupError, StrictModel
from anamnema.source_policy import SourcePolicy

Identity = Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]


class WindowReference(StrictModel):
    """Immutable source ordering, not another authoritative copy of conversation text."""

    key: Identity = Field(alias="_id")
    schema_version: Literal[1]
    instance: Nonempty
    session: Nonempty
    substrate: Nonempty
    boundary: Identity
    records: tuple[Identity, ...]

    @model_validator(mode="after")
    def identity_and_order(self) -> Self:
        """The captured order participates in identity; repeated records cannot count twice."""
        if len(set(self.records)) != len(self.records):
            raise ValueError("window repeats a source identity")
        if digest([self.boundary, [identity for identity in self.records]]) != self.key:
            raise ValueError("window identity does not match its ordered references")
        return self


class WindowStore:
    """Only insert/read reference manifests in an explicit project collection."""

    def __init__(self, archive: MongoArchive, policy: SourcePolicy) -> None:
        if policy.windows_collection in archive.settings.collections.values():
            raise SetupError("window reference collection must differ from archive collections")
        self.archive, self.policy = archive, policy

    def save(self, window: Window) -> None:
        """Verify every original before recording captured order; replay never replaces it."""
        scope = (window.boundary.instance, window.boundary.session, window.boundary.substrate)
        if window.boundary.kind not in ("summary", "closing") or any(
            (record.instance, record.session, record.substrate) != scope
            or record.kind != "conversation"
            for record in window.records
        ):
            raise SetupError("window references mix ownership or record kinds")
        if window.boundary.kind == "closing" and (
            not window.records
            or window.boundary.source.get("after") != window.records[-1].identity
            or window.boundary.source.get("at") != window.records[-1].timestamp
        ):
            raise SetupError("window closing marker does not reference its last conversation")
        document: dict[str, JsonValue] = {
            "_id": str(window.report()["key"]),
            "schema_version": self.policy.schema_version,
            "instance": scope[0],
            "session": scope[1],
            "substrate": scope[2],
            "boundary": window.boundary.identity,
            "records": [record.identity for record in window.records],
        }
        self._validated(document)
        self.archive.verify([*window.records, window.boundary])
        try:
            collection = self.archive.database[self.policy.windows_collection]
            collection.update_one({"_id": document["_id"]}, {"$setOnInsert": document}, upsert=True)
        except PyMongoError as exc:
            raise SetupError(
                f"window reference write failed ({type(exc).__name__}); safe to resume"
            ) from exc
        saved = self.load(scope[0], scope[1], scope[2], str(document["_id"]))
        if saved is None or canonical(saved.model_dump(mode="json", by_alias=True)) != canonical(
            document
        ):
            raise SetupError("window reference read-back differs; no automatic repair")

    def load(self, instance: str, session: str, substrate: str, key: str) -> WindowReference | None:
        """Read only the named assistant/session/window; no timestamp-based reconstruction."""
        try:
            document = self.archive.database[self.policy.windows_collection].find_one(
                {
                    "_id": key,
                    "instance": instance,
                    "session": session,
                    "substrate": substrate,
                }
            )
        except PyMongoError as exc:
            raise SetupError(f"window reference lookup failed ({type(exc).__name__})") from exc
        return self._validated(document) if document is not None else None

    def _validated(self, document: dict[str, Any]) -> WindowReference:
        try:
            if len(BSON.encode(document)) > self.policy.max_manifest_bytes:
                raise SetupError("window reference exceeds configured byte bound")
            reference = WindowReference.model_validate_json(canonical(document))
            if len(reference.records) > self.policy.max_window_records:
                raise SetupError("window reference exceeds configured record bound")
            return reference
        except (ValidationError, ValueError, TypeError, InvalidDocument) as exc:
            if isinstance(exc, SetupError):
                raise
            raise SetupError("window references are corrupt; no automatic repair") from exc
