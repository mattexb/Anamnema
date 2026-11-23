"""Real private file publication, interruption, immutable replay and verification boundaries."""

import json

import pytest
from pydantic import ValidationError

from anamnema.briefing_files import BriefFilePolicy, BriefFiles, brief_file_policy
from anamnema.briefing_render import RenderedBrief
from anamnema.setup_config import SetupError


@pytest.fixture
def bundles(tmp_path):
    return BriefFiles(tmp_path / "briefings", brief_file_policy())


@pytest.fixture
def rendered():
    return RenderedBrief(("# Page one\n", "# Page two\n"), (5, 5), (), "# Review\n")


def test_commit_all_pages_private_then_read_hashes_and_replay_without_rewrite(bundles, rendered):
    identity = bundles.save("fixture", rendered, {"subject": "fixture topic"})
    manifest, pages = bundles.read("fixture", identity)
    assert manifest.instance == "fixture" and len(pages) == 2
    assert tuple(p.read_text(encoding="utf-8") for p in pages) == rendered.pages
    assert all(p.stat().st_mode & 0o777 == 0o600 for p in pages)
    assert all(p.parent.stat().st_mode & 0o777 == 0o700 for p in pages)
    times = [p.stat().st_mtime_ns for p in pages]
    assert bundles.save("fixture", rendered, {"subject": "fixture topic"}) == identity
    assert [p.stat().st_mtime_ns for p in pages] == times
    with pytest.raises(SetupError):
        bundles.read("another-owner", identity)


def test_failure_before_manifest_is_not_readable_and_exact_replay_can_finish(
    bundles, rendered, monkeypatch
):
    original = bundles.files.save

    def fail_manifest(path, data, **kwargs):
        if path.name == bundles.policy.manifest_file:
            raise OSError("fixture stop before manifest commit")
        original(path, data, **kwargs)

    monkeypatch.setattr(bundles.files, "save", fail_manifest)
    with pytest.raises(OSError, match="fixture stop"):
        bundles.save("fixture", rendered, {})
    folders = list(bundles.root.glob("*/*"))
    assert len(folders) == 1 and not (folders[0] / bundles.policy.manifest_file).exists()
    identity = folders[0].name
    with pytest.raises(SetupError, match="uncommitted"):
        bundles.read("fixture", identity)
    monkeypatch.setattr(bundles.files, "save", original)
    assert bundles.save("fixture", rendered, {}) == identity
    assert len(bundles.read("fixture", identity)[1]) == 2


def test_changed_or_linked_page_is_refused_not_automatically_repaired(bundles, rendered):
    identity = bundles.save("fixture", rendered, {})
    _, pages = bundles.read("fixture", identity)
    pages[0].write_text("corrupt", encoding="utf-8")
    with pytest.raises(SetupError, match="checksum"):
        bundles.read("fixture", identity)
    with pytest.raises(SetupError, match="immutable"):
        bundles.save("fixture", rendered, {})
    pages[0].unlink()
    pages[0].symlink_to(pages[1])
    with pytest.raises(SetupError):
        bundles.read("fixture", identity)


def test_resource_bounds_and_unsafe_configuration_refuse_before_creating_root(bundles, rendered):
    bundles.policy = bundles.policy.model_copy(update={"max_file_bytes": 3})
    with pytest.raises(SetupError, match="byte limit"):
        bundles.save("fixture", rendered, {})
    assert not bundles.root.exists()
    data = brief_file_policy().model_dump(mode="json")
    for changes in (
        {"page_pattern": "../{page}.md"},
        {"page_pattern": "same.md"},
        {"page_pattern": "{unknown}"},
        {"page_pattern": "{page!q}"},
        {"manifest_file": "review.md"},
        {"file_mode": 0o644},
    ):
        with pytest.raises(ValidationError):
            BriefFilePolicy.model_validate_json(json.dumps({**data, **changes}))
