"""Retained upstream grammar, including nested wrappers and malformed envelopes."""

import pytest

from anamnema.memhub_parsing import (
    is_harness_injection,
    is_host_generated_user_turn,
    sanitize_model_input,
    strip_host_wrappers,
    text_content,
)


@pytest.mark.parametrize(
    "source,expected",
    [
        ("  words  ", "words"),
        ("<additional_data>context</additional_data>words", "words"),
        ("words<system-reminder>context</system-reminder>", "words"),
        ("<user_info>context</user_info><user_query>words</user_query>", "words"),
        ("<user_query>words</user_query><image_local_path>a.png</image_local_path>", "words"),
        (
            "<additional_data><additional_data>nested</additional_data></additional_data>words",
            "words",
        ),
        ("<additional_data>unterminated", "<additional_data>unterminated"),
        ("words<system-reminder>unterminated", "words<system-reminder>unterminated"),
        ("<user_query>unterminated", "<user_query>unterminated"),
        ("<user_info>context</user_info>ambiguous", "<user_info>context</user_info>ambiguous"),
        (
            "<user_query>words</user_query><image_local_path>unterminated",
            "<user_query>words</user_query><image_local_path>unterminated",
        ),
        ("<system-reminder>only context</system-reminder>", None),
    ],
)
def test_host_wrapper_grammar_is_retained(source, expected):
    assert strip_host_wrappers(source) == expected


@pytest.mark.parametrize(
    "text,expected",
    [
        ('{"request_id":"a","type":"write","steps":[]}', True),
        ('{"request_id":"a","type":"other","steps":[]}', False),
        ("{not-json", False),
        ("[1,2]", False),
        ("ordinary prose", False),
        (
            "Perform any necessary follow-up actions in response to the subagent completion above",
            True,
        ),
    ],
)
def test_host_generated_turns_do_not_become_human_speech(text, expected):
    assert is_host_generated_user_turn(text) is expected


def test_configured_harness_scan_and_mixed_text_block_validation():
    assert is_harness_injection("  <system-reminder>context", 2000)
    assert is_harness_injection("<timestamp>x<system_notification>context", 2000)
    assert not is_harness_injection("ordinary prose", 2000)
    assert (
        text_content(
            [None, "text", {"type": "text", "text": 7}, {"type": "text", "text": "  yes "}],
            frozenset({"text"}),
        )
        == "yes"
    )
    assert text_content("   ", frozenset({"text"})) is None


def test_upstream_credential_patterns_are_sanitized_before_output():
    assert "example-credential" not in sanitize_model_input(
        "Authorization: Bearer example-credential"
    )
    assert "example-value" not in sanitize_model_input("--password example-value")
    assert 'password="<redacted-secret>"' in sanitize_model_input('password="example-value"')
    assert "example-value" not in sanitize_model_input("https://user:example-value@example.test")
    assert "<redacted-secret>" in sanitize_model_input("sk-" + "demo" * 8)
