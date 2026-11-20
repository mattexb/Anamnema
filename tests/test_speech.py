"""Source-attributed spoken input, never tools or injected owner statements."""

import dataclasses
import json
from pathlib import Path

import pytest

from anamnema.archive_models import Record
from anamnema.cli import main
from anamnema.processing import Window
from anamnema.setup_config import SetupError
from anamnema.speech import Row, model_row, own_words, scrub, without_marked
from anamnema.speech_policy import speech_policy
from anamnema.substrates.claude_speech import ClaudeSpeechReader


def record(content, *, number=1, role="user", **extra):
    return Record(
        "a",
        "claude",
        "s",
        "conversation",
        "2026-01-01",
        number,
        {
            "type": role,
            "message": {"content": content},
            **extra,
        },
    )


def read(*sources):
    boundary = Record("a", "claude", "s", "summary", "2026-01-02", 100, {})
    return ClaudeSpeechReader(speech_policy()).read(Window(boundary, tuple(sources)))


def test_mixed_tool_and_thinking_blocks_never_become_spoken_text():
    source = record(
        [
            {"type": "text", "text": "Keep the original conversation."},
            {"type": "tool_result", "content": "tool secret not a person"},
            {"type": "thinking", "thinking": "reasoning not a person"},
        ]
    )
    before = json.dumps(source.source)
    result = read(source)
    assert result.rows[0].text == "Keep the original conversation."
    assert result.rows[0].speaker == "owner"
    assert result.rows[0].source is source
    assert json.dumps(source.source) == before


@pytest.mark.parametrize(
    "content",
    [
        [{"type": "tool_result", "content": "not owner speech"}],
        [{"type": "tool_use", "name": "send", "input": {"text": "not implicitly approved"}}],
        [{"type": "thinking", "thinking": "not dialogue"}],
        {"unexpected": "object"},
        None,
        [],
    ],
)
def test_non_dialogue_content_is_counted_and_excluded(content):
    result = read(record(content))
    assert result.rows == ()
    assert result.dropped == {"no_dialogue_text": 1}


@pytest.mark.parametrize(
    "content,extra,reason",
    [
        ("hello", {"isSidechain": True}, "sidechain"),
        ("injected", {"isMeta": True}, "injected_meta"),
        ("Another Claude session sent a message: peer", {"isMeta": True}, "peer"),
        ("<task-notification>result</task-notification>", {}, "injected"),
        ("# AGENTS.md instructions\nnot a human", {}, "harness"),
        ("<dynamic_tool_catalog>tools</dynamic_tool_catalog>", {}, "harness"),
        ("[tool_result]", {}, "tool_placeholder"),
    ],
)
def test_known_injected_rows_have_visible_reasons(content, extra, reason):
    result = read(record(content, **extra))
    assert not result.rows
    assert result.dropped == {reason: 1}


def test_legacy_text_messages_and_public_artifact_marker():
    source = record("unused", role="assistant")
    source.source["message"] = "We decided to keep sources. [anamnema-artifact:abc]"
    result = read(source)
    assert result.rows[0].speaker == "agent"
    assert result.rows[0].text == "We decided to keep sources."


def test_row_number_stays_in_source_sequence_after_an_exclusion():
    first = record("injected", isMeta=True)
    second = record("keep this", number=5, role="assistant")
    result = read(first, second)
    assert len(result.rows) == 1
    assert result.rows[0].n == 1
    assert result.rows[0].source.source_line == 5


def test_code_png_and_other_room_members_are_not_owner_statements():
    policy = speech_policy()
    assert own_words("before ```secret code``` after <svg>data</svg>", policy) == "before  after"
    png = "before \ufffdPNG\r\n\x1a\nraw-dataIENDCRC! after"
    cleaned = own_words(png, policy)
    assert "raw-data" not in cleaned
    assert "before" in cleaned
    partial = "before \ufffdPNG\r\n\x1a\nraw-data"
    assert "raw-data" not in own_words(partial, policy)
    turns = "[1] Someone (agent): peer opinion\n[2] Owner (human): my decision"
    assert own_words(turns, policy) == "my decision"


def test_marked_lines_are_removed_only_when_said_not_quoted_or_fenced():
    policy = speech_policy()
    text = "fact: capture separately\n> fact: quoted\n```\nfact: example\n```\nordinary words"
    kept = without_marked(text, policy)
    assert "capture separately" not in kept
    assert "> fact: quoted" in kept
    assert "fact: example" in kept
    assert "ordinary words" in kept
    assert "fact: content" not in without_marked("- fact: content", policy)
    assert "fact(entity): named" not in without_marked("fact(entity): named", policy)


def test_secret_masking_leaves_raw_archive_unchanged_and_counts_rows():
    secret = "Demo" + "Password" + "42!"
    source = record("Use " + secret + " once")
    before = source.source.copy()
    result = read(source)
    assert secret not in result.rows[0].text
    assert "[secret]" in result.rows[0].text
    assert result.masked == 1
    assert source.source == before
    cleaned, count = scrub("password=example-value token=other-example", speech_policy())
    assert "example-value" not in cleaned
    assert "other-example" not in cleaned
    assert count == 1
    assert scrub("ordinary prose", speech_policy()) == ("ordinary prose", 0)


def test_speech_filter_drops_noise_and_agent_receipts():
    policy = speech_policy()
    source = record("unused")
    assert model_row(Row(0, "owner", "/compact", source), policy) == (None, 0)
    assert model_row(Row(0, "agent", "ANAMNEMA_READ_V1 abc", source), policy) == (None, 0)
    assert not read(record("fact: captured separately", role="assistant")).rows


def test_unknown_native_role_is_not_guessed_as_owner():
    result = read(record("not automatically the owner", role="A"))
    assert result.dropped == {"unsupported_native_role": 1}


def test_native_keys_are_configurable_not_embedded_in_shared_reader():
    policy = speech_policy()
    custom = policy.model_copy(
        update={
            "claude": policy.claude.model_copy(
                update={
                    "type_key": "nativeRole",
                    "message_key": "nativeMessage",
                }
            )
        }
    )
    original = record("unused")
    source = dataclasses.replace(
        original,
        source={
            "nativeRole": "user",
            "nativeMessage": {"content": "custom schema"},
        },
    )
    reader = ClaudeSpeechReader(custom)
    assert reader.translate(source, 2).text == "custom schema"


@pytest.mark.parametrize(
    "change",
    [
        {"secret_word_pattern": "["},
        {"owner_room_turn": "no named group"},
        {"marked_pattern": "no named group"},
        {"unexpected": True},
    ],
)
def test_bad_complete_policy_fails_without_echoing_values(tmp_path: Path, change):
    value = speech_policy().model_dump(mode="json")
    value.update(change)
    path = tmp_path / "policy.json"
    path.write_text(json.dumps(value), encoding="utf-8")
    with pytest.raises(SetupError, match="cannot be read or validated"):
        speech_policy(path)


def test_process_preview_counts_speech_without_echoing_it(tmp_path: Path, capsys):
    path = tmp_path / "source.jsonl"
    rows = [
        {"type": "user", "timestamp": "2026-01-01", "message": "private words"},
        {"type": "assistant", "timestamp": "2026-01-02", "message": "my response"},
    ]
    path.write_text("\n".join(json.dumps(row) for row in rows), encoding="utf-8")
    before = path.read_bytes()
    assert (
        main(["process", "--file", str(path), "--instance", "a", "--session", "s", "--closed"]) == 0
    )
    output = capsys.readouterr().out
    report = json.loads(output)
    assert report["speech"][0]["eligible_rows"] == 2
    assert len(report["speech_policy_sha256"]) == 64
    assert "private words" not in output
    assert "my response" not in output
    assert path.read_bytes() == before
