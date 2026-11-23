"""Immutable per-window batch inputs, not another mutable native transcript copy."""

import hashlib
import lzma
import os
from importlib.resources import files
from pathlib import Path
from typing import Literal, Self

from pydantic import Field, JsonValue, ValidationError, model_validator

from anamnema.archive_models import Record, Snapshot, canonical, digest
from anamnema.batch_models import ContentDigest
from anamnema.private_files import PrivateFiles
from anamnema.processing import Window, prepare
from anamnema.setup_config import Nonempty, SetupError, StrictModel


class PreparationPolicy(StrictModel):
    max_raw_bytes: int = Field(gt=0)
    max_compressed_bytes: int = Field(gt=0)
    max_manifest_bytes: int = Field(gt=0)
    max_decoder_memory_bytes: int = Field(gt=0)
    max_records: int = Field(gt=0)
    compression_preset: int = Field(ge=0, le=9)
    manifest_file: Nonempty
    payload_file: Nonempty
    lock_file: Nonempty
    temporary_prefix: Nonempty
    directory_mode: int
    file_mode: int

    @model_validator(mode="after")
    def private_names(self) -> Self:
        names = (self.manifest_file, self.payload_file, self.lock_file)
        if (
            len(set(names)) != len(names)
            or any(
                Path(name).name != name
                or name in {".", ".."}
                or any(c in name for c in "\\\n\r\x00")
                for name in names
            )
            or not self.temporary_prefix.startswith(".")
            or any(c in self.temporary_prefix for c in "/\\\n\r\x00")
            or (self.directory_mode, self.file_mode) != (0o700, 0o600)
        ):
            raise ValueError("prepared input names/modes must be private local files")
        return self


def preparation_policy(path: Path | None = None) -> PreparationPolicy:
    try:
        raw = (
            path.read_text(encoding="utf-8")
            if path
            else files("anamnema").joinpath("config/batch_prepare.json").read_text(encoding="utf-8")
        )
        return PreparationPolicy.model_validate_json(raw)
    except (OSError, UnicodeError, ValidationError) as exc:
        raise SetupError("batch preparation policy cannot be read or validated") from exc


class PreparedRecord(StrictModel):
    instance: Nonempty
    substrate: Literal["claude"]
    session: Nonempty
    kind: Literal["conversation", "summary", "closing"]
    timestamp: Nonempty
    source_line: int = Field(gt=0)
    source: dict[str, JsonValue]

    def record(self) -> Record:
        return Record(
            self.instance,
            self.substrate,
            self.session,
            self.kind,
            self.timestamp,
            self.source_line,
            self.source,
        )


class PreparedPayload(StrictModel):
    records: tuple[PreparedRecord, ...] = Field(min_length=1)


class PreparedManifest(StrictModel):
    version: Literal[1] = 1
    instance: Nonempty
    session: Nonempty
    window: ContentDigest
    boundary_kind: Literal["summary", "closing"]
    records: tuple[ContentDigest, ...] = Field(min_length=1)
    payload_sha256: ContentDigest
    raw_sha256: ContentDigest
    raw_bytes: int = Field(gt=0)
    compressed_bytes: int = Field(gt=0)

    @property
    def identity(self) -> str:
        return digest(self.model_dump(mode="json"))


class PreparedInputs:
    """The manifest commits last; damaged input is never regenerated over an existing identity."""

    def __init__(self, root: Path, policy: PreparationPolicy) -> None:
        if not root.is_absolute() or root == root.parent:
            raise SetupError("prepared input root must be an explicit absolute non-root directory")
        self.root, self.policy = root, policy
        self.files = PrivateFiles(policy.directory_mode, policy.file_mode, policy.temporary_prefix)

    def _folder(self, instance: str, identity: str, *, create: bool) -> Path:
        if len(identity) != hashlib.sha256().digest_size * 2 or any(
            c not in "0123456789abcdef" for c in identity
        ):
            raise SetupError("prepared input identity must be a content digest")
        folder = self.root / digest(instance) / identity
        try:
            for directory in (self.root, folder.parent, folder):
                if create:
                    self.files.directory(directory)
                else:
                    self.files.existing_directory(directory)
        except OSError as exc:
            raise SetupError(
                f"prepared input directory unavailable ({type(exc).__name__})"
            ) from exc
        return folder

    def save(self, window: Window) -> Path:
        """Commit one closed window without changing the original records or native file."""
        try:
            return self._save_window(window)
        except SetupError:
            raise
        except (OSError, ValueError, TypeError) as exc:
            raise SetupError(f"prepared window cannot be saved ({type(exc).__name__})") from exc

    def _save_window(self, window: Window) -> Path:
        records = (*window.records, window.boundary)
        values = PreparedPayload(
            records=tuple(
                PreparedRecord.model_validate(
                    {
                        "instance": row.instance,
                        "substrate": row.substrate,
                        "session": row.session,
                        "kind": row.kind,
                        "timestamp": row.timestamp,
                        "source_line": row.source_line,
                        "source": row.source,
                    }
                )
                for row in records
            )
        )
        self._window(values)
        raw = canonical(values.model_dump(mode="json"))
        if len(raw) > self.policy.max_raw_bytes or len(records) > self.policy.max_records:
            raise SetupError("prepared window exceeds raw byte/record bounds")
        payload = lzma.compress(raw, preset=self.policy.compression_preset)
        if len(payload) > self.policy.max_compressed_bytes:
            raise SetupError("prepared window exceeds compressed byte bound")
        manifest = PreparedManifest(
            instance=window.boundary.instance,
            session=window.boundary.session,
            window=str(window.report()["key"]),
            boundary_kind="closing" if window.boundary.kind == "closing" else "summary",
            records=tuple(row.identity for row in records),
            payload_sha256=hashlib.sha256(payload).hexdigest(),
            raw_sha256=hashlib.sha256(raw).hexdigest(),
            raw_bytes=len(raw),
            compressed_bytes=len(payload),
        )
        data = canonical(manifest.model_dump(mode="json"))
        if len(data) > self.policy.max_manifest_bytes:
            raise SetupError("prepared manifest exceeds configured byte bound")
        folder = self._folder(manifest.instance, manifest.identity, create=True)
        with self.files.locked(folder / self.policy.lock_file):
            self._immutable(
                folder / self.policy.payload_file, payload, self.policy.max_compressed_bytes
            )
            self._immutable(
                folder / self.policy.manifest_file, data, self.policy.max_manifest_bytes
            )
        self.load(manifest.instance, manifest.identity)
        return folder / self.policy.manifest_file

    def _immutable(self, path: Path, data: bytes, limit: int) -> None:
        if path.exists() or path.is_symlink():
            if self.files.read(path, limit) != data:
                raise SetupError("prepared input differs; no automatic replacement")
            return
        self.files.save(path, data)

    def manifest(self, instance: str, identity: str) -> PreparedManifest:
        folder = self._folder(instance, identity, create=False)
        try:
            body = self.files.read(
                folder / self.policy.manifest_file, self.policy.max_manifest_bytes
            )
            result = PreparedManifest.model_validate_json(body)
        except (OSError, ValidationError) as exc:
            raise SetupError("prepared manifest is missing or corrupt") from exc
        if (
            result.identity != identity
            or hashlib.sha256(body).hexdigest() != identity
            or result.instance != instance
            or result.raw_bytes > self.policy.max_raw_bytes
            or result.compressed_bytes > self.policy.max_compressed_bytes
            or len(result.records) > self.policy.max_records
        ):
            raise SetupError("prepared manifest identity/owner/bounds differ")
        return result

    def path(self, instance: str, identity: str) -> Path:
        """Locate a committed manifest without recreating any input files."""
        self.manifest(instance, identity)
        return self._folder(instance, identity, create=False) / self.policy.manifest_file

    def remove_payload(self, instance: str, identity: str) -> bool:
        """Remove matching input bytes only after the caller verifies archive and journal proof."""
        manifest = self.manifest(instance, identity)
        folder = self._folder(instance, identity, create=False)
        path = folder / self.policy.payload_file
        with self.files.locked(folder / self.policy.lock_file):
            if not path.exists() and not path.is_symlink():
                return False
            before = path.stat(follow_symlinks=False)
            body = self.files.read(path, self.policy.max_compressed_bytes)
            after = path.stat(follow_symlinks=False)
            stamps = [
                (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)
                for info in (before, after)
            ]
            if (
                stamps[0] != stamps[1]
                or len(body) != manifest.compressed_bytes
                or hashlib.sha256(body).hexdigest() != manifest.payload_sha256
            ):
                raise SetupError("prepared payload changed; nothing removed")
            path.unlink()
            descriptor = os.open(folder, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        return True

    def load(self, instance: str, identity: str) -> Window:
        manifest = self.manifest(instance, identity)
        folder = self._folder(instance, identity, create=False)
        try:
            payload = self.files.read(
                folder / self.policy.payload_file, self.policy.max_compressed_bytes
            )
            if (
                len(payload) != manifest.compressed_bytes
                or hashlib.sha256(payload).hexdigest() != manifest.payload_sha256
            ):
                raise SetupError("prepared payload checksum differs")
            decoder = lzma.LZMADecompressor(memlimit=self.policy.max_decoder_memory_bytes)
            raw = decoder.decompress(payload, max_length=self.policy.max_raw_bytes + 1)
            if (
                not decoder.eof
                or decoder.unused_data
                or len(raw) != manifest.raw_bytes
                or hashlib.sha256(raw).hexdigest() != manifest.raw_sha256
            ):
                raise SetupError("prepared payload decompression or content bounds differ")
            values = PreparedPayload.model_validate_json(raw)
            window = self._window(values)
            if (
                tuple(row.identity for row in (*window.records, window.boundary))
                != manifest.records
                or str(window.report()["key"]) != manifest.window
                or window.boundary.instance != instance
                or window.boundary.session != manifest.session
                or window.boundary.kind != manifest.boundary_kind
            ):
                raise SetupError("prepared window scope or source identities differ")
            return window
        except (OSError, lzma.LZMAError, ValidationError) as exc:
            raise SetupError("prepared input cannot be recovered") from exc

    def _window(self, payload: PreparedPayload) -> Window:
        if len(payload.records) > self.policy.max_records:
            raise SetupError("prepared record bound exceeded")
        records = tuple(row.record() for row in payload.records)
        snapshot = Snapshot(self.root, "", records, 0, 0, records[-1].kind == "closing")
        result = prepare(snapshot)
        if (
            len(result.windows) != 1
            or result.pending
            or result.repeats
            or len(result.windows[0].records) + 1 != len(records)
        ):
            raise SetupError("prepared input must contain exactly one closed window")
        return result.windows[0]
