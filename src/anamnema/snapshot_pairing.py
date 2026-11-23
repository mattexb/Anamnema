"""Bounded owned snapshot/summary pairing, preserving pending and ambiguous source states."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from bson import BSON
from pydantic import JsonValue, TypeAdapter, ValidationError
from pymongo.errors import PyMongoError

from anamnema.archive_codec import decode_source
from anamnema.archive_models import Record, canonical
from anamnema.continuity import validate_snapshot
from anamnema.continuity_config import ContinuityConfig, ContinuityPolicy
from anamnema.setup_config import SetupError
from anamnema.snapshot_archive import MongoSnapshots, PairStatus

JSON_OBJECT = TypeAdapter(dict[str, JsonValue])


def when(value: Any) -> datetime | None:
    """Unknown or timezone-ambiguous chronology never becomes a guessed pair."""
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


class SnapshotPairing:
    """Late transcript imports may settle pending snapshots; wall-clock age never gives up."""

    def __init__(
        self, archive: MongoSnapshots, config: ContinuityConfig, policy: ContinuityPolicy
    ) -> None:
        self.archive, self.config, self.policy = archive, config, policy

    def _summaries(
        self, session: str, byte_limit: int
    ) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
        archive, policy = self.archive.archive, self.archive.policy
        cursor = (
            archive.database[archive.settings.collections["summary"]]
            .find({"instance": self.config.instance, "session": session, "kind": "summary"})
            .limit(policy.max_summaries + 1)
            .batch_size(1)
        )
        found: list[dict[str, Any]] = []
        size = 0
        invalid: list[dict[str, str]] = []
        for document in cursor:
            if len(found) + len(invalid) >= policy.max_summaries:
                raise SetupError("summary pairing count exceeds configured bound; pairs unchanged")
            encoded = len(BSON.encode(document))
            remaining = byte_limit - size
            size += encoded
            if size > byte_limit:
                raise SetupError("summary pairing bytes exceed configured bound; pairs unchanged")
            settings = archive.settings.model_copy(update={"max_file_bytes": remaining})
            try:
                source = JSON_OBJECT.validate_python(decode_source(document, settings), strict=True)
                line = document.get("source_line")
                if type(line) is not int or line < 1:
                    raise SetupError("summary archive source line is invalid")
                row = Record(
                    self.config.instance,
                    archive.settings.substrate,
                    session,
                    "summary",
                    document["timestamp"],
                    line,
                    source,
                )
                if canonical(row.core()) != canonical({k: document.get(k) for k in row.core()}):
                    raise SetupError("summary archive identity or source differs; pairs unchanged")
            except SetupError:
                raise
            except (KeyError, TypeError, ValidationError, ValueError) as exc:
                raise SetupError(
                    "summary archive source cannot be verified; pairs unchanged"
                ) from exc
            at = when(document.get("timestamp"))
            if at is None:
                invalid.append({"summary_id": row.identity, "reason": "invalid_or_naive_timestamp"})
            else:
                found.append({"id": row.identity, "at": at})
        return found, invalid

    def _pending(self, session: str) -> tuple[list[dict[str, Any]], bool, int]:
        collection = self.archive.archive.database[self.archive.policy.collection]
        cursor = (
            collection.find(
                {
                    "instance": self.config.instance,
                    "session": session,
                    "pair_status": PairStatus.PENDING,
                }
            )
            .limit(self.archive.policy.pair_batch_size + 1)
            .batch_size(1)
        )
        snapshots: list[dict[str, Any]] = []
        size = 0
        for document in cursor:
            if len(snapshots) == self.archive.policy.pair_batch_size:
                return snapshots, True, size
            size += len(BSON.encode(document))
            if size > self.archive.policy.max_pair_bytes:
                raise SetupError("snapshot pairing bytes exceed configured bound; pairs unchanged")
            snapshots.append(document)
        return snapshots, False, size

    def _choice(self, snapshot: dict[str, Any], summaries: list[dict[str, Any]]) -> dict[str, Any]:
        at = when(snapshot.get("written_at"))
        if at is None:
            raise SetupError("snapshot written time is invalid; pair unchanged")
        until = at + timedelta(seconds=self.archive.policy.pair_window_seconds)
        matches = [row for row in summaries if at < row["at"] <= until]
        if len(matches) == 1:
            return {
                "pair_status": PairStatus.PAIRED,
                "summary_id": matches[0]["id"],
                "summary_at": matches[0]["at"].isoformat(),
            }
        if len(matches) > 1:
            return {
                "pair_status": PairStatus.AMBIGUOUS,
                "summary_candidates": [r["id"] for r in matches],
            }
        return {
            "pair_status": PairStatus.UNPAIRED
            if any(r["at"] > until for r in summaries)
            else PairStatus.PENDING
        }

    def run(self, session: str) -> dict[str, Any]:
        """Pair only independently verified owned content; do not invent dates or truncate sets."""
        counts: dict[str, Any] = {status.value: 0 for status in PairStatus}
        counts.update(
            invalid_summaries=0, repair_required=[], blocked_snapshots=0, concurrent=0, more=False
        )
        try:
            collection = self.archive.archive.database[self.archive.policy.collection]
            snapshots, counts["more"], used = self._pending(session)
            summaries, invalid = (
                self._summaries(session, self.archive.policy.max_pair_bytes - used)
                if snapshots
                else ([], [])
            )
            counts["invalid_summaries"] = len(invalid)
            counts["repair_required"] = invalid
            counts["blocked_snapshots"] = len(snapshots) if invalid else 0
            for snapshot in snapshots[: self.archive.policy.pair_batch_size]:
                text = snapshot.get("text")
                written = snapshot.get("written_at")
                if not isinstance(text, str) or not isinstance(written, str):
                    raise SetupError("snapshot archive text/time is invalid; pair unchanged")
                validate_snapshot(text, self.config, self.policy, session)
                expected = self.archive.document(self.config.instance, session, text, written)
                self.archive.verify(expected, False)
                fields = (
                    self._choice(snapshot, summaries)
                    if not invalid
                    else {"pair_status": PairStatus.PENDING}
                )
                if fields["pair_status"] != PairStatus.PENDING:
                    result = collection.update_one(
                        {
                            "_id": expected["_id"],
                            "instance": self.config.instance,
                            "session": session,
                            "text": text,
                            "pair_status": PairStatus.PENDING,
                        },
                        {"$set": fields},
                    )
                    if result.matched_count != 1:
                        counts["concurrent"] += 1
                        continue
                counts[str(fields["pair_status"])] += 1
        except PyMongoError as exc:
            raise SetupError(
                f"snapshot pairing failed ({type(exc).__name__}); safe to retry"
            ) from exc
        return counts
