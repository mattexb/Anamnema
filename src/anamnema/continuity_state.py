"""One locked atomic journal and immutable managed content per assistant/session."""

from __future__ import annotations

import hashlib
import os
import re
import stat
import tempfile
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path

from pydantic import ValidationError

from anamnema.continuity_config import ContinuityConfig, ContinuityPolicy
from anamnema.private_files import PrivateFiles
from anamnema.setup_config import Nonempty, SetupError, StrictModel
from anamnema.worker_registry import RegistryConfig, register, release


def source_text(path: Path, limit: int) -> str:
    """Bound read-only regular source files and detect replacement/change while copying."""
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as stream:
        before = os.fstat(stream.fileno())
        if not stat.S_ISREG(before.st_mode):
            raise SetupError("continuity source must be a regular file")
        data = stream.read(limit + 1)
        after = os.fstat(stream.fileno())
    current = path.stat(follow_symlinks=False)

    def signature(item: os.stat_result) -> tuple[int, int, int, int]:
        return item.st_dev, item.st_ino, item.st_size, item.st_mtime_ns

    if signature(before) != signature(after) or signature(before) != signature(current):
        raise SetupError("continuity source changed while being read; retry")
    if len(data) > limit:
        raise SetupError("continuity source exceeds configured bound")
    return data.decode("utf-8")


class RequiredRead(StrictModel):
    """The nonce is only published in managed content, never in injected context."""

    producer: Nonempty
    blob: Nonempty
    token: Nonempty
    key: str | None = None
    content_digest: str | None = None


class Snapshot(StrictModel):
    """Applied is the public equivalent of the source's application marker."""

    blob: Nonempty
    digest: Nonempty
    applied: bool = False


class ContinuityState(StrictModel):
    """An acknowledgement transitions all local obligations in one atomic replacement."""

    instance: Nonempty
    session: Nonempty
    initialized: bool = False
    reads: tuple[RequiredRead, ...] = ()
    snapshot: Snapshot | None = None
    spool: tuple[Snapshot, ...] = ()
    last_snapshot: Snapshot | None = None
    failure: str | None = None


class StateFiles:
    """All mutation is restricted to one private session directory under an exclusive lock."""

    def __init__(self, config: ContinuityConfig, policy: ContinuityPolicy, session: str) -> None:
        if (
            re.fullmatch(policy.session_pattern, session) is None
            or re.fullmatch(r"[A-Za-z0-9_-]+", session) is None
        ):
            raise SetupError("unsafe continuity session id")
        self.config, self.policy, self.session = config, policy, session
        self.private = PrivateFiles(
            policy.directory_mode, policy.file_mode, policy.temporary_prefix
        )
        owner = hashlib.sha256(config.instance.encode("utf-8")).hexdigest()
        self.folder = config.state_root / owner / session

    def _directory(self, path: Path) -> None:
        self.private.directory(path)

    def _existing_directory(self, path: Path) -> None:
        self.private.existing_directory(path)

    @contextmanager
    def temporary_inputs(self, root: Path) -> Generator[Path]:
        """Private worker inputs live outside session cleanup and never hold its lock."""
        if root.resolve().is_relative_to(self.config.state_root.resolve()):
            raise SetupError("snapshot worker inputs must be outside managed state")
        self._directory(root)
        with tempfile.TemporaryDirectory(prefix=self.policy.temporary_prefix, dir=root) as folder:
            path = Path(folder)
            path.chmod(self.policy.directory_mode)
            yield path

    @contextmanager
    def registered_inputs(
        self, root: Path, registry: RegistryConfig | None
    ) -> Generator[tuple[Path, Path | None]]:
        """Retain recovery intent until private inputs, not merely their worker, are gone."""
        folder: Path | None = None
        record: Path | None = None
        try:
            with self.temporary_inputs(root) as folder:
                if registry is not None:
                    record = register(registry, (folder,))
                yield folder, record
        finally:
            if (
                registry is not None
                and record is not None
                and folder is not None
                and not folder.exists()
                and not folder.is_symlink()
            ):
                release(registry, record)

    def _check_file(self, descriptor: int) -> None:
        self.private.check_file(descriptor)

    def read(self, name: str, limit: int) -> bytes:
        """Bound managed reads and refuse symlinks/hardlinks before reading content."""
        return self._read_in(self.folder, name, limit)

    def _read_in(self, folder: Path, name: str, limit: int) -> bytes:
        self._name(name)
        return self.private.read(folder / name, limit)

    def _name(self, name: str) -> None:
        if Path(name).name != name or name in (".", "..") or "\\" in name:
            raise SetupError("unsafe managed continuity filename")

    def save(self, name: str, content: bytes) -> None:
        """Flush new bytes before atomic publication, then persist the directory change."""
        self._save_in(self.folder, name, content)

    def _save_in(
        self, folder: Path, name: str, content: bytes, *, written_ns: int | None = None
    ) -> None:
        self._name(name)
        self.private.save(folder / name, content, written_ns=written_ns)

    def blob(self, content: str) -> str:
        """Content-addressed publication allows a failed journal write to be retried safely."""
        data = content.encode("utf-8")
        if len(data) > self.policy.max_content_bytes:
            raise SetupError("continuity content exceeds configured bound")
        name = hashlib.sha256(data).hexdigest() + self.policy.blob_suffix
        path = self.folder / name
        if path.exists() or path.is_symlink():
            if self.read(name, self.policy.max_content_bytes) != data:
                raise SetupError("managed continuity content hash conflict")
        else:
            self.save(name, data)
        return name

    def content(self, name: str) -> str:
        """Verify the immutable name/content identity on every use."""
        data = self.read(name, self.policy.max_content_bytes)
        if name != hashlib.sha256(data).hexdigest() + self.policy.blob_suffix:
            raise SetupError("managed continuity content changed")
        return data.decode("utf-8")

    def commit(self, state: ContinuityState) -> None:
        """Never truncate a journal or publish it into another instance/session."""
        if (state.instance, state.session) != (self.config.instance, self.session):
            raise SetupError("foreign continuity state")
        data = state.model_dump_json().encode("utf-8")
        if len(data) > self.policy.max_state_bytes:
            raise SetupError("continuity state exceeds configured bound")
        self.save(self.policy.state_file, data)

    def spool_snapshot(self, snapshot: Snapshot) -> None:
        """Publish acknowledged content before clearing its active journal reference."""
        folder = self.folder / self.policy.spool_directory
        self._directory(folder)
        data = self.content(snapshot.blob).encode("utf-8")
        if (
            hashlib.sha256(data.decode("utf-8").rstrip().encode("utf-8")).hexdigest()
            != snapshot.digest
        ):
            raise SetupError("snapshot spool digest conflict")
        self._clean_spool_temporary(folder)
        target = folder / snapshot.blob
        if target.exists() or target.is_symlink():
            if self._read_in(folder, snapshot.blob, self.policy.max_content_bytes) != data:
                raise SetupError("snapshot spool content conflict")
        else:
            self._save_in(
                folder,
                snapshot.blob,
                data,
                written_ns=(self.folder / snapshot.blob).stat().st_mtime_ns,
            )

    def reserve_spool(self) -> None:
        """Bound new staging before creating an obligation; never expire archived content."""
        folder = self.folder / self.policy.spool_directory
        self._directory(folder)
        self._clean_spool_temporary(folder)
        with os.scandir(folder) as entries:
            for count, _ in enumerate(entries, start=1):
                if count >= self.policy.max_spool_items:
                    raise SetupError("snapshot spool is full; archive sync required before staging")

    def _clean_spool_temporary(self, folder: Path) -> None:
        for path in folder.iterdir():
            if not path.name.startswith(self.policy.temporary_prefix):
                continue
            descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            try:
                self._check_file(descriptor)
            finally:
                os.close(descriptor)
            path.unlink()

    def _migrate_spool(self, state: ContinuityState) -> ContinuityState:
        if not state.spool:
            return state
        for snapshot in state.spool:
            self.spool_snapshot(snapshot)
        updated = state.model_copy(update={"spool": (), "last_snapshot": state.spool[-1]})
        self.commit(updated)
        return updated

    def peek(self) -> ContinuityState:
        """Inspect existing journal state without lock creation, migration or cleanup writes."""
        if any(p.is_symlink() for p in (self.folder, *self.folder.parents)):
            raise SetupError("continuity directories cannot contain symlinks")
        if not self.folder.exists():
            return ContinuityState(instance=self.config.instance, session=self.session)
        try:
            for path in (self.config.state_root, self.folder.parent, self.folder):
                self._existing_directory(path)
            journal = self.folder / self.policy.state_file
            state = (
                ContinuityState.model_validate_json(
                    self.read(journal.name, self.policy.max_state_bytes)
                )
                if journal.exists() or journal.is_symlink()
                else ContinuityState(instance=self.config.instance, session=self.session)
            )
            if (state.instance, state.session) != (self.config.instance, self.session):
                raise SetupError("foreign continuity journal")
            return state
        except (OSError, ValidationError) as exc:
            raise SetupError(f"continuity inspection failed ({type(exc).__name__})") from exc

    def spooled_names(self, limit: int) -> tuple[list[str], bool, int]:
        """A bounded inventory; temporary entries are counted, never silently called snapshots."""
        folder = self.folder / self.policy.spool_directory
        if any(p.is_symlink() for p in (folder, *folder.parents)):
            raise SetupError("continuity directories cannot contain symlinks")
        if not folder.exists():
            return [], False, 0
        self._existing_directory(folder)
        names: list[str] = []
        temporary = 0
        with os.scandir(folder) as entries:
            for entry in entries:
                if entry.name.startswith(self.policy.temporary_prefix):
                    temporary += 1
                    continue
                if (
                    re.fullmatch(r"[a-f0-9]{64}" + re.escape(self.policy.blob_suffix), entry.name)
                    is None
                ):
                    raise SetupError("unrecognized snapshot spool entry; no files removed")
                names.append(entry.name)
                if len(names) > limit:
                    return sorted(names[:limit]), True, temporary
        return sorted(names), False, temporary

    def read_spooled(self, name: str) -> tuple[bytes, int]:
        """Capture immutable owned bytes and the original write time under the caller's lock."""
        folder = self.folder / self.policy.spool_directory
        self._existing_directory(folder)
        data = self._read_in(folder, name, self.policy.max_content_bytes)
        if name != hashlib.sha256(data).hexdigest() + self.policy.blob_suffix:
            raise SetupError("snapshot spool content changed; file retained")
        return data, (folder / name).stat().st_mtime_ns

    def remove_spooled(self, name: str, data: bytes, written_ns: int) -> bool:
        """Only delete the same verified copy; another successful sync may have removed it."""
        path = self.folder / self.policy.spool_directory / name
        if not path.exists() and not path.is_symlink():
            return False
        current, timestamp = self.read_spooled(name)
        if current != data or timestamp != written_ns:
            raise SetupError("snapshot changed during sync; local file retained")
        path.unlink()
        return True

    def cleanup(self, state: ContinuityState) -> None:
        """Only orphaned managed blobs/temporary files in this locked session are removable."""
        retained = {r.blob for r in state.reads} | {s.blob for s in state.spool}
        if state.snapshot:
            retained.add(state.snapshot.blob)
        for path in self.folder.iterdir():
            if path.name in (
                self.policy.state_file,
                self.policy.lock_file,
                self.policy.spool_directory,
            ):
                continue
            owned = re.fullmatch(r"[a-f0-9]{64}" + re.escape(self.policy.blob_suffix), path.name)
            if path.name not in retained and (
                owned or path.name.startswith(self.policy.temporary_prefix)
            ):
                descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
                try:
                    self._check_file(descriptor)
                finally:
                    os.close(descriptor)
                path.unlink()

    @contextmanager
    def locked(self) -> Generator[ContinuityState]:
        """Refuse busy, corrupt or foreign state rather than silently resetting required reads."""
        # Stack delivery takes its lock first; code under this lock must never acquire stack locks.
        try:
            import fcntl
        except ImportError as exc:
            raise SetupError("continuity locking requires a POSIX host") from exc
        for path in (self.config.state_root, self.folder.parent, self.folder):
            self._directory(path)
        descriptor = os.open(
            self.folder / self.policy.lock_file,
            os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW,
            self.policy.file_mode,
        )
        try:
            self._check_file(descriptor)
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise SetupError("continuity session is busy; retry the event") from exc
            path = self.folder / self.policy.state_file
            state = (
                ContinuityState.model_validate_json(
                    self.read(path.name, self.policy.max_state_bytes)
                )
                if path.exists() or path.is_symlink()
                else ContinuityState(instance=self.config.instance, session=self.session)
            )
            if (state.instance, state.session) != (self.config.instance, self.session):
                raise SetupError("foreign continuity journal")
            for blob in [r.blob for r in state.reads] + (
                [state.snapshot.blob] if state.snapshot else []
            ):
                self.content(blob)
            state = self._migrate_spool(state)
            self.cleanup(state)
            yield state
        except SetupError:
            raise
        except (OSError, ValueError, ValidationError) as exc:
            raise SetupError(f"continuity state unavailable ({type(exc).__name__})") from exc
        finally:
            os.close(descriptor)
