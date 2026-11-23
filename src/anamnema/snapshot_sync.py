"""Archive snapshots without holding a live session lock during network operations."""

from datetime import UTC, datetime
from typing import Any

from anamnema.continuity import validate_snapshot
from anamnema.continuity_config import ContinuityConfig, ContinuityPolicy
from anamnema.continuity_state import StateFiles
from anamnema.setup_config import SetupError
from anamnema.snapshot_archive import MongoSnapshots, SnapshotArchivePolicy
from anamnema.snapshot_pairing import SnapshotPairing


class SnapshotSync:
    """Local deletion follows exact read-back; any interruption is safely retryable."""

    def __init__(
        self,
        config: ContinuityConfig,
        policy: ContinuityPolicy,
        session: str,
        archive_policy: SnapshotArchivePolicy,
    ) -> None:
        self.files = StateFiles(config, policy, session)
        self.archive_policy = archive_policy

    def preview(self) -> dict[str, Any]:
        """No directories, locks, migrations, credentials or database calls in dry-run."""
        state = self.files.peek()
        names, more, temporary = self.files.spooled_names(self.archive_policy.batch_size)
        return {
            "mode": "dry-run",
            "verified": False,
            "spooled": len(names),
            "more": more,
            "temporary_seen": temporary,
            "legacy_spooled": len(state.spool),
        }

    def apply(self, archive: MongoSnapshots) -> dict[str, Any]:
        """Copy under short locks; store/verify outside; conditionally remove under lock."""
        if archive.policy != self.archive_policy:
            raise SetupError("snapshot sync/archive policies differ")
        archive.ensure_schema()
        with self.files.locked():
            names, more, temporary = self.files.spooled_names(
                max(self.archive_policy.batch_size, self.files.policy.max_spool_items)
            )
        inserted = removed = verified = missing = 0
        refused: list[dict[str, str]] = []
        for name in names:
            if verified >= self.archive_policy.batch_size:
                more = True
                break
            with self.files.locked():
                path = self.files.folder / self.files.policy.spool_directory / name
                if not path.exists() and not path.is_symlink():
                    missing += 1
                    continue
                try:
                    data, written_ns = self.files.read_spooled(name)
                except SetupError, OSError:
                    refused.append({"file": name, "reason": "invalid_spool"})
                    continue
            try:
                text = data.decode("utf-8")
                validate_snapshot(text, self.files.config, self.files.policy, self.files.session)
            except SetupError, UnicodeError:
                refused.append({"file": name, "reason": "invalid_spool"})
                continue
            written_at = datetime.fromtimestamp(written_ns / 1_000_000_000, UTC).isoformat()
            document = archive.document(
                self.files.config.instance, self.files.session, text, written_at
            )
            new = archive.keep(document)
            archive.verify(document, new)
            verified += 1
            inserted += int(new)
            with self.files.locked():
                if self.files.remove_spooled(name, data, written_ns):
                    removed += 1
                else:
                    missing += 1
        result: dict[str, Any] = {
            "mode": "apply",
            "verified": verified,
            "inserted": inserted,
            "removed": removed,
            "already_removed": missing,
            "more": more,
            "temporary_seen": temporary,
            "refused": refused,
            "pairs": None,
            "pairing_error": None,
        }
        try:
            result["pairs"] = SnapshotPairing(archive, self.files.config, self.files.policy).run(
                self.files.session
            )
        except SetupError as exc:
            result["pairing_error"] = str(exc)
        return result
