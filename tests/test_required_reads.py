"""Real derived-read producers share the existing session gate and immutable managed copies."""

import hashlib
import io
import json

import pytest
from test_continuity import ack, state
from test_continuity import continuity as continuity

from anamnema.cli import main
from anamnema.continuity import Continuity
from anamnema.continuity_state import StateFiles
from anamnema.setup_config import SetupError


def briefing(tmp_path, text="Synthetic derived briefing.\n"):
    source = tmp_path / "briefing.md"
    source.write_text(text, encoding="utf-8")
    return source


def test_derived_and_identity_reads_settle_independently(continuity, tmp_path):
    core = continuity
    source = briefing(tmp_path)
    context = core.require("session-one", "briefings", "subject-one", source)
    files, before = state(core)
    assert not before.initialized and len(before.reads) == 1
    copied = files.content(before.reads[0].blob)
    assert copied.startswith(source.read_text(encoding="utf-8"))
    assert json.loads(copied.splitlines()[-1])["read_token"] == before.reads[0].token
    assert before.reads[0].token not in context
    core.start("session-one", "startup")
    _, held = state(core)
    assert len(held.reads) == 2
    assert core.stop("session-one", ack(core, before))
    assert state(core)[1].reads[0].producer == "identity"
    assert core.stop("session-one", ack(core, held)) == ""
    assert source.read_text(encoding="utf-8") == "Synthetic derived briefing.\n"


def test_replayed_announcement_keeps_token_and_source_unchanged(continuity, tmp_path):
    core = continuity
    source = briefing(tmp_path, "Text with meaningful trailing spaces  \n\n")
    original = source.read_bytes()
    first = core.require("session-one", "briefings", "subject", source)
    files, held = state(core)
    assert core.require("session-one", "briefings", "subject", source) == first
    assert state(core)[1] == held
    assert files.content(held.reads[0].blob).startswith(original.decode("utf-8"))
    assert source.read_bytes() == original


def test_new_derived_version_supersedes_owed_copy_but_old_ack_cannot_pass(continuity, tmp_path):
    core = continuity
    source = briefing(tmp_path)
    core.require("session-one", "briefings", "subject", source)
    files, old = state(core)
    source.write_text("Updated derived briefing", encoding="utf-8")
    changed = core.require("session-one", "briefings", "subject", source)
    _, new = state(core)
    assert len(new.reads) == 1 and new.reads[0].token != old.reads[0].token
    assert not (files.folder / old.reads[0].blob).exists()
    assert core.stop("session-one", ack(core, old)) == changed
    assert core.stop("session-one", ack(core, new)) == ""


@pytest.mark.parametrize("ending", ["\n", "\n\n", "\r\n"])
def test_source_token_tail_is_removed_only_from_managed_copy(continuity, tmp_path, ending):
    core = continuity
    source = briefing(
        tmp_path, "Briefing body.\n\nread_token: old-one" + ending + "read_token: old-two" + ending
    )
    original = source.read_bytes()
    core.require("session-one", "briefings", "subject", source)
    files, held = state(core)
    copied = files.content(held.reads[0].blob)
    assert "read_token: " not in copied
    assert copied.startswith("Briefing body.\n\n")
    assert json.loads(copied.splitlines()[-1]) == {"read_token": held.reads[0].token}
    assert core.stop("session-one", core.policy.read_marker + " old-two")
    assert core.stop("session-one", ack(core, held)) == ""
    assert source.read_bytes() == original


def test_token_only_source_creates_no_debt(continuity, tmp_path):
    source = briefing(tmp_path, "read_token: old\n\n")
    with pytest.raises(SetupError, match="empty"):
        continuity.require("session-one", "briefings", "subject", source)
    assert not continuity.config.state_root.exists()


def test_source_restamping_keeps_existing_proof_for_unchanged_body(continuity, tmp_path):
    source = briefing(tmp_path, "Body.\n\nread_token: old\n")
    first = continuity.require("session-one", "briefings", "subject", source)
    held = state(continuity)[1]
    source.write_text("Body.\n\nread_token: newer\n", encoding="utf-8")
    assert continuity.require("session-one", "briefings", "subject", source) == first
    assert state(continuity)[1] == held


def test_nonterminal_token_example_and_unstamped_whitespace_are_preserved(continuity, tmp_path):
    source = briefing(tmp_path, "read_token: example\nThis is part of the body.  \n\n")
    continuity.require("session-one", "briefings", "subject", source)
    files, held = state(continuity)
    assert files.content(held.reads[0].blob).startswith(source.read_text(encoding="utf-8"))


def test_existing_two_token_copy_is_replaced_on_next_announcement(continuity, tmp_path):
    source = briefing(tmp_path, "Body.\n\nread_token: source-old\n")
    raw = source.read_text(encoding="utf-8")
    continuity.require("session-one", "briefings", "subject", source)
    files, held = state(continuity)
    with files.locked() as before:
        legacy = before.reads[0].model_copy(
            update={
                "blob": files.blob(
                    raw + "\n\n" + json.dumps({"read_token": before.reads[0].token}) + "\n"
                ),
                "content_digest": hashlib.sha256(raw.encode("utf-8")).hexdigest(),
            }
        )
        files.commit(before.model_copy(update={"reads": (legacy,)}))
    continuity.require("session-one", "briefings", "subject", source)
    _, repaired = state(continuity)
    assert repaired.reads[0].token != held.reads[0].token
    assert "read_token: " not in files.content(repaired.reads[0].blob)
    assert not (files.folder / legacy.blob).exists()
    assert continuity.stop("session-one", ack(continuity, held))
    assert continuity.stop("session-one", ack(continuity, repaired)) == ""


def test_queue_reserves_identity_capacity_and_refuses_without_changing_existing_debt(
    continuity, tmp_path
):
    core = Continuity(continuity.config, continuity.policy.model_copy(update={"max_pending": 2}))
    source = briefing(tmp_path)
    core.require("session-one", "briefings", "one", source)
    before = state(core)[1]
    with pytest.raises(SetupError, match="bound"):
        core.require("session-one", "briefings", "two", source)
    assert state(core)[1] == before
    core.start("session-one", "startup")
    assert len(state(core)[1].reads) == 2


@pytest.mark.parametrize(
    "producer,key",
    [
        ("identity", "x"),
        ("../escape", "x"),
        ("briefings", ""),
        ("briefings", "a\nb"),
        ("briefings", "x" * 257),
    ],
)
def test_invalid_producer_or_key_never_creates_state(continuity, tmp_path, producer, key):
    source = briefing(tmp_path)
    with pytest.raises(SetupError):
        continuity.require("session-one", producer, key, source)
    assert not continuity.config.state_root.exists()


def test_missing_or_empty_source_never_creates_impossible_read_debt(continuity, tmp_path):
    source = tmp_path / "missing.md"
    with pytest.raises(OSError):
        continuity.require("session-one", "briefings", "subject", source)
    assert not continuity.config.state_root.exists()
    source.write_text("", encoding="utf-8")
    with pytest.raises(SetupError, match="empty"):
        continuity.require("session-one", "briefings", "subject", source)
    assert not continuity.config.state_root.exists()


def test_failed_new_version_commit_preserves_old_managed_copy_and_proof(
    continuity, tmp_path, monkeypatch
):
    core = continuity
    source = briefing(tmp_path)
    core.require("session-one", "briefings", "subject", source)
    files, old = state(core)
    commit = StateFiles.commit
    source.write_text("New version", encoding="utf-8")

    def interrupted(*_):
        raise OSError("fixture interrupted before required-read publication")

    monkeypatch.setattr(StateFiles, "commit", interrupted)
    with pytest.raises(SetupError):
        core.require("session-one", "briefings", "subject", source)
    assert state(core)[1] == old and (files.folder / old.reads[0].blob).exists()
    monkeypatch.setattr(StateFiles, "commit", commit)
    assert core.stop("session-one", ack(core, old)) == ""


def test_cli_registration_returns_context_without_native_or_model_claim(
    continuity, tmp_path, capsys, monkeypatch
):
    source = briefing(tmp_path)
    config = tmp_path / "continuity.json"
    config.write_text(continuity.config.model_dump_json(), encoding="utf-8")
    monkeypatch.setattr("sys.stdin", io.StringIO("not a native hook payload"))
    assert (
        main(
            [
                "continuity",
                "require",
                "--session",
                "session-one",
                "--producer",
                "briefings",
                "--key",
                "subject",
                "--file",
                str(source),
                "--continuity-config",
                str(config),
            ]
        )
        == 0
    )
    result = json.loads(capsys.readouterr().out)
    assert result["producer"] == "briefings" and result["key"] == "subject"
    assert continuity.policy.read_marker in result["context"]
    held = state(continuity)[1]
    assert held.reads[0].token not in result["context"]
