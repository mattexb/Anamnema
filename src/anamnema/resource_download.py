"""Explicit pinned HTTPS artifacts, verified before immutable private publication."""

import hashlib
import os
import time
import urllib.error
import urllib.request
from http.client import HTTPMessage
from importlib.resources import files
from pathlib import Path
from typing import IO, Annotated, Self

from pydantic import Field, HttpUrl, ValidationError, model_validator

from anamnema.archive_models import digest
from anamnema.private_files import PrivateFiles
from anamnema.setup_config import SetupError, StrictModel


class PinnedResource(StrictModel):
    filename: Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")]
    url: HttpUrl
    sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    size_bytes: int = Field(gt=0)
    license_url: HttpUrl
    revision: Annotated[str, Field(min_length=1)]

    @model_validator(mode="after")
    def public_https(self) -> Self:
        for url in (self.url, self.license_url):
            if (
                url.scheme != "https"
                or url.username is not None
                or url.password is not None
                or url.query is not None
                or url.fragment is not None
            ):
                raise ValueError("resource URLs must be public HTTPS without credentials or query")
        return self


class DownloadPolicy(StrictModel):
    chunk_bytes: int = Field(gt=0)
    idle_seconds: float = Field(gt=0, allow_inf_nan=False)
    transfer_seconds: float = Field(gt=0, allow_inf_nan=False)


class ModelCatalog(StrictModel):
    encoder: PinnedResource
    reranker_model: PinnedResource
    reranker_tokenizer: PinnedResource
    reranker_specials: PinnedResource


def model_catalog(path: Path | None = None) -> ModelCatalog:
    """Only the tested encoder/reranker; the operator supplies the notes model separately."""
    try:
        raw = (
            path.read_bytes()
            if path
            else files("anamnema").joinpath("config/model_assets.json").read_bytes()
        )
        return ModelCatalog.model_validate_json(raw)
    except (OSError, ValidationError) as exc:
        raise SetupError("model asset catalog cannot be read or validated") from exc


class HTTPSOnlyRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: IO[bytes],
        code: int,
        msg: str,
        headers: HTTPMessage,
        newurl: str,
    ) -> urllib.request.Request | None:
        if not newurl.startswith("https://"):
            raise SetupError("resource redirect refused a non-HTTPS destination")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class ResourceDownloads:
    """No implicit network call: inspect verifies local bytes; fetch is explicit consent."""

    def __init__(self, root: Path, policy: DownloadPolicy) -> None:
        if not root.is_absolute() or ".." in root.parts:
            raise SetupError("resource root must be absolute without traversal")
        self.root, self.policy = root, policy
        self.files = PrivateFiles(0o700, 0o600, ".resource-write-")

    def inspect(self, resource: PinnedResource) -> Path:
        self.files.existing_directory(self.root)
        path = self.root / resource.filename
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(descriptor, "rb") as stream:
            self.files.check_file(stream.fileno())
            if os.fstat(stream.fileno()).st_size != resource.size_bytes:
                raise SetupError("existing resource has a different size; not replaced")
            checksum = hashlib.sha256()
            remaining = resource.size_bytes
            while remaining:
                block = stream.read(min(remaining, self.policy.chunk_bytes))
                if not block:
                    raise SetupError("existing resource changed during verification")
                checksum.update(block)
                remaining -= len(block)
            if stream.read(1) or checksum.hexdigest() != resource.sha256:
                raise SetupError("existing resource checksum differs; not replaced")
        return path

    def fetch(self, resource: PinnedResource) -> tuple[Path, bool]:
        """Return path/reused; failures never replace a prior file or publish partial bytes."""
        self.files.directory(self.root)
        path = self.root / resource.filename
        with self.files.locked(self.root / ".download.lock"):
            if os.path.lexists(path):
                return self.inspect(resource), True
            key = digest(resource.model_dump(mode="json"))
            partial = self.root / (".download-" + key + ".part")
            if os.path.lexists(partial):
                descriptor = os.open(partial, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
                try:
                    self.files.check_file(descriptor)
                finally:
                    os.close(descriptor)
                partial.unlink()
            try:
                self._transfer(resource, partial)
                os.link(partial, path, follow_symlinks=False)
                partial.unlink()
                self._sync()
                return self.inspect(resource), False
            except (OSError, urllib.error.URLError, ValueError) as exc:
                if isinstance(exc, SetupError):
                    raise
                raise SetupError("resource transfer failed; no partial artifact accepted") from exc
            finally:
                partial.unlink(missing_ok=True)

    def _transfer(self, resource: PinnedResource, partial: Path) -> None:
        deadline = time.monotonic() + self.policy.transfer_seconds
        opener = urllib.request.build_opener(HTTPSOnlyRedirect())
        request = urllib.request.Request(  # noqa: S310 - validated public HTTPS pin; redirects reject downgrade
            str(resource.url), headers={"Accept-Encoding": "identity"}
        )
        checksum = hashlib.sha256()
        received = 0
        descriptor = os.open(partial, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with (
            os.fdopen(descriptor, "wb") as output,
            opener.open(
                request, timeout=min(self.policy.idle_seconds, self.policy.transfer_seconds)
            ) as response,
        ):
            if response.status != 200:
                raise SetupError("resource server did not return a complete artifact")
            while True:
                if time.monotonic() >= deadline:
                    raise SetupError("resource transfer exceeded its time budget")
                block = response.read(
                    min(self.policy.chunk_bytes, resource.size_bytes - received + 1)
                )
                if time.monotonic() >= deadline:
                    raise SetupError("resource transfer exceeded its time budget")
                if not block:
                    break
                received += len(block)
                if received > resource.size_bytes:
                    raise SetupError("resource transfer exceeds its pinned size")
                checksum.update(block)
                output.write(block)
            if received != resource.size_bytes or checksum.hexdigest() != resource.sha256:
                raise SetupError("resource transfer size or checksum differs from its pin")
            output.flush()
            os.fsync(output.fileno())

    def _sync(self) -> None:
        descriptor = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
