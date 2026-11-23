"""Pinned resource bytes and publication failures without network or model downloads."""

import hashlib
import io
import json
import urllib.error
from http.client import HTTPMessage

import pytest
from pydantic import ValidationError

from anamnema.resource_download import (
    DownloadPolicy,
    HTTPSOnlyRedirect,
    PinnedResource,
    ResourceDownloads,
    model_catalog,
)
from anamnema.setup_config import SetupError


@pytest.fixture
def resource():
    return PinnedResource(
        filename="fixture.gguf",
        url="https://example.org/model/fixture.gguf",
        sha256=hashlib.sha256(b"synthetic weights").hexdigest(),
        size_bytes=17,
        license_url="https://example.org/LICENSE",
        revision="fixture-commit",
    )


@pytest.fixture
def downloads(tmp_path):
    return ResourceDownloads(
        tmp_path / "models",
        DownloadPolicy(
            chunk_bytes=4,
            idle_seconds=1.0,
            transfer_seconds=5.0,
        ),
    )


def response(monkeypatch, raw=b"synthetic weights", status=200):
    class Response(io.BytesIO):
        pass

    data = Response(raw)
    data.status = status

    class Opener:
        def open(self, request, timeout):
            assert request.full_url == "https://example.org/model/fixture.gguf"
            assert request.get_header("Accept-encoding") == "identity" and timeout > 0
            return data

    monkeypatch.setattr(
        "anamnema.resource_download.urllib.request.build_opener", lambda *_: Opener()
    )


def test_verified_publication_replays_without_network(downloads, resource, monkeypatch):
    response(monkeypatch)
    path, reused = downloads.fetch(resource)
    assert not reused and path.read_bytes() == b"synthetic weights"
    assert path.stat().st_mode & 0o777 == 0o600
    monkeypatch.setattr(
        "anamnema.resource_download.urllib.request.build_opener",
        lambda *_: pytest.fail("network on replay"),
    )
    assert downloads.fetch(resource) == (path, True)
    assert downloads.inspect(resource) == path
    assert not list(downloads.root.glob("*.part"))


@pytest.mark.parametrize("raw", [b"short", b"synthetic weightX", b"synthetic weights plus"])
def test_bad_transfer_never_publishes_or_leaves_partial(downloads, resource, monkeypatch, raw):
    response(monkeypatch, raw)
    with pytest.raises(SetupError, match="pin|checksum"):
        downloads.fetch(resource)
    assert not (downloads.root / resource.filename).exists()
    assert not list(downloads.root.glob("*.part"))


def test_bad_existing_file_and_redirect_are_not_overwritten(downloads, resource, monkeypatch):
    downloads.files.directory(downloads.root)
    target = downloads.root / resource.filename
    downloads.files.save(target, b"operator file")
    with pytest.raises(SetupError, match="different size"):
        downloads.fetch(resource)
    assert target.read_bytes() == b"operator file"
    target.unlink()
    target.symlink_to(downloads.root / "elsewhere")
    with pytest.raises(OSError):
        downloads.fetch(resource)
    assert target.is_symlink()


def test_refused_status_and_network_errors_are_sanitized(downloads, resource, monkeypatch):
    response(monkeypatch, status=206)
    with pytest.raises(SetupError, match="complete artifact"):
        downloads.fetch(resource)

    class Opener:
        def open(self, *_args, **_kwargs):
            raise urllib.error.URLError("synthetic private diagnostic")

    monkeypatch.setattr(
        "anamnema.resource_download.urllib.request.build_opener", lambda *_: Opener()
    )
    with pytest.raises(SetupError) as error:
        downloads.fetch(resource)
    assert "synthetic private diagnostic" not in str(error.value)
    assert not list(downloads.root.glob("*.part"))


def test_https_downgrade_is_refused():
    with pytest.raises(SetupError, match="non-HTTPS"):
        HTTPSOnlyRedirect().redirect_request(
            None, None, 302, "redirect", HTTPMessage(), "http://example.org/file"
        )


@pytest.mark.parametrize(
    "url",
    [
        "http://example.org/a",
        "https://user:secret@example.org/a",
        "https://example.org/a?token=secret",
        "https://example.org/a#fragment",
    ],
)
def test_resource_pins_refuse_credential_and_plaintext_urls(resource, url):
    with pytest.raises(ValidationError):
        PinnedResource.model_validate_json(
            json.dumps({**resource.model_dump(mode="json"), "url": url})
        )


def test_interrupted_transfer_is_discarded_on_retry(downloads, resource, monkeypatch):
    from anamnema.archive_models import digest

    downloads.files.directory(downloads.root)
    partial = downloads.root / (".download-" + digest(resource.model_dump(mode="json")) + ".part")
    downloads.files.save(partial, b"old partial")
    response(monkeypatch)
    assert downloads.fetch(resource)[0].read_bytes() == b"synthetic weights"
    assert not partial.exists()


def test_packaged_model_catalog_has_immutable_public_pins():
    catalog = model_catalog()
    assert catalog.encoder.size_bytes == 36685088
    assert catalog.reranker_model.filename == "model.onnx"
    assert catalog.reranker_tokenizer.filename == "tokenizer.json"
    assert catalog.reranker_specials.filename == "special_tokens_map.json"
    for model in catalog.model_dump(mode="json").values():
        assert model["revision"] in model["url"] and len(model["sha256"]) == 64
