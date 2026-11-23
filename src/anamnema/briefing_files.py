"""Immutable private briefing bundles; the manifest commits only after every page is durable."""

import hashlib
from importlib.resources import files
from pathlib import Path
from typing import Self

from pydantic import Field, JsonValue, ValidationError, model_validator

from anamnema.archive_models import canonical, digest
from anamnema.briefing_render import RenderedBrief
from anamnema.private_files import PrivateFiles
from anamnema.setup_config import Nonempty, SetupError, StrictModel


class BriefFilePolicy(StrictModel):
    """Names and resource limits are explicit; all generated memory remains owner-only."""

    directory_mode: int
    file_mode: int
    temporary_prefix: Nonempty
    max_pages: int = Field(gt=0)
    max_file_bytes: int = Field(gt=0)
    max_bundle_bytes: int = Field(gt=0)
    manifest_file: Nonempty
    review_file: Nonempty
    page_pattern: Nonempty

    @model_validator(mode="after")
    def private_names(self) -> Self:
        if self.directory_mode != 0o700 or self.file_mode != 0o600:
            raise ValueError("briefing files must be owner-only")
        try:
            pages = [self.page_pattern.format(page=n) for n in range(1, self.max_pages + 1)]
        except (ValueError, KeyError, IndexError) as exc:
            raise ValueError("invalid briefing page pattern") from exc
        names = [*pages, self.manifest_file, self.review_file]
        if len(names) != len(set(names)) or any(
            not name
            or Path(name).name != name
            or name in {".", ".."}
            or "\\" in name
            or any(c in name for c in ("\n", "\r", "\x00"))
            for name in names
        ):
            raise ValueError("briefing filenames must be distinct safe basenames")
        if self.max_file_bytes > self.max_bundle_bytes:
            raise ValueError("briefing file limit exceeds bundle limit")
        if not self.temporary_prefix.startswith(".") or any(
            c in self.temporary_prefix for c in "/\\"
        ):
            raise ValueError("briefing temporary prefix must be a local hidden basename")
        return self


class BriefManifest(StrictModel):
    """A content-addressed build is readable only with every committed file verified."""

    instance: Nonempty
    pages: tuple[Nonempty, ...]
    checksums: dict[str, Nonempty]
    metadata: dict[str, JsonValue]


def brief_file_policy(path: Path | None = None) -> BriefFilePolicy:
    try:
        raw = (
            path.read_text(encoding="utf-8")
            if path
            else files("anamnema")
            .joinpath("config/briefing_files.json")
            .read_text(encoding="utf-8")
        )
        return BriefFilePolicy.model_validate_json(raw)
    except (OSError, UnicodeError, ValidationError) as exc:
        raise SetupError("briefing file policy cannot be read or validated") from exc


class BriefFiles:
    """An interrupted bundle is never announced as a committed briefing; replay can finish it."""

    def __init__(self, root: Path, policy: BriefFilePolicy) -> None:
        if not root.is_absolute() or root == root.parent:
            raise SetupError("briefing root must be an explicit absolute non-root directory")
        self.root, self.policy = root, policy
        self.files = PrivateFiles(policy.directory_mode, policy.file_mode, policy.temporary_prefix)

    def save(self, instance: str, rendered: RenderedBrief, metadata: dict[str, JsonValue]) -> str:
        """Write immutable pages before the manifest; identical replay never rewrites a file."""
        if not instance.strip() or not 1 <= len(rendered.pages) <= self.policy.max_pages:
            raise SetupError("briefing owner or page count is invalid")
        pages = tuple(
            self.policy.page_pattern.format(page=n) for n in range(1, len(rendered.pages) + 1)
        )
        content = {
            name: page.encode("utf-8") for name, page in zip(pages, rendered.pages, strict=True)
        }
        content[self.policy.review_file] = rendered.review.encode("utf-8")
        manifest = BriefManifest(
            instance=instance,
            pages=pages,
            checksums={name: hashlib.sha256(data).hexdigest() for name, data in content.items()},
            metadata=metadata,
        )
        data = canonical(manifest.model_dump(mode="json"))
        identity = hashlib.sha256(data).hexdigest()
        self._bounds([*content.values(), data])
        folder = self._folder(instance, identity, create=True)
        for name, body in content.items():
            self._immutable(folder / name, body)
        self._immutable(folder / self.policy.manifest_file, data)
        return identity

    def read(self, instance: str, identity: str) -> tuple[BriefManifest, tuple[Path, ...]]:
        """Reverify the owner, manifest identity, policy names and all hashes before delivery."""
        try:
            folder = self._folder(instance, identity, create=False)
            data = self.files.read(folder / self.policy.manifest_file, self.policy.max_file_bytes)
            manifest = BriefManifest.model_validate_json(data)
            if hashlib.sha256(data).hexdigest() != identity or manifest.instance != instance:
                raise SetupError("briefing manifest identity or owner differs")
            pages = tuple(
                self.policy.page_pattern.format(page=n) for n in range(1, len(manifest.pages) + 1)
            )
            if not 1 <= len(pages) <= self.policy.max_pages or manifest.pages != pages:
                raise SetupError("briefing manifest pages differ from configured policy")
            if set(manifest.checksums) != {*pages, self.policy.review_file}:
                raise SetupError("briefing manifest file set differs")
            content = [
                self.files.read(folder / name, self.policy.max_file_bytes)
                for name in manifest.checksums
            ]
            self._bounds([data, *content])
            for (name, expected), body in zip(manifest.checksums.items(), content, strict=True):
                if hashlib.sha256(body).hexdigest() != expected:
                    raise SetupError("briefing file checksum differs: " + name)
        except (OSError, ValidationError) as exc:
            raise SetupError("briefing bundle is missing, uncommitted or invalid") from exc
        return manifest, tuple(folder / page for page in pages)

    def _bounds(self, content: list[bytes]) -> None:
        if (
            any(len(data) > self.policy.max_file_bytes for data in content)
            or sum(map(len, content)) > self.policy.max_bundle_bytes
        ):
            raise SetupError("briefing bundle exceeds configured file or total byte limit")

    def _folder(self, instance: str, identity: str, *, create: bool) -> Path:
        if len(identity) != hashlib.sha256().digest_size * 2 or any(
            c not in "0123456789abcdef" for c in identity
        ):
            raise SetupError("briefing identity is not a content digest")
        folder = self.root / digest(instance) / identity
        for directory in (self.root, folder.parent, folder):
            if create:
                self.files.directory(directory)
            else:
                self.files.existing_directory(directory)
        return folder

    def _immutable(self, path: Path, data: bytes) -> None:
        if path.exists() or path.is_symlink():
            if self.files.read(path, self.policy.max_file_bytes) != data:
                raise SetupError("immutable briefing content differs; no automatic replacement")
            return
        self.files.save(path, data)
