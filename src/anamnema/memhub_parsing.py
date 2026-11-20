"""MemHub's rules for reading a host transcript record, ported unchanged.

Ported from Tencent Hy-Memory (https://github.com/Tencent-Hunyuan/Hy-Memory), MemHub app,
Copyright (C) 2026 Tencent, licensed under the Apache License 2.0:
- ``memhub/apps/core/src/one_memory/imports/sources/parsing.py``: text_content, tool evidence
  lines, host-envelope unwrapping (text only, without the position bookkeeping), host-generated
  user turns;
- ``memhub/apps/core/src/one_memory/input_sanitization.py``: sanitize_model_input;
- ``memhub/apps/core/src/one_memory/conversation_processing.py:15-31, 242-254``: harness
  injections that are not user turns.
The regular expressions are MemHub's fixed input grammar and stay as written.

Anamnema adaptation: explicit casts at validated container boundaries for strict typing;
the harness scan limit is passed from public policy and unused tool-rendering helpers
are omitted. The retained parsing and sanitization grammar is unchanged.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from typing import Any, cast

HARNESS_PREFIXES = (
    "<codex_internal_context",
    "# agents.md instructions",
    "<environment_context>",
    "<permissions instructions>",
    "<system-reminder>",
    "<system_notification>",
    "<instructions>",
)
_HOST_ENVELOPE = re.compile(
    r"(?:"
    r"^<timestamp>.*?<system_(?:notification|reminder)>|"
    r"^<system_(?:notification|reminder)>|"
    r'^\{\s*"request_id"\s*:.*?"steps"\s*:'
    r")",
    re.IGNORECASE | re.DOTALL,
)
_REMINDER_TAGS = r"additional_data|system[-_]reminder|working_memory_reminder"
_CONTEXT_TAGS = r"user_info|rules|agent_transcripts|system[-_]notification|pipeline_log"
_TAG_ATTRIBUTES = r"""(?:\s+[\w:-]+\s*=\s*(?:"[^"<>]*"|'[^'<>]*'))*\s*"""
_HOST_PREFIX = re.compile(rf"<(?P<tag>{_REMINDER_TAGS}|{_CONTEXT_TAGS}){_TAG_ATTRIBUTES}>")
_REMINDER_START = re.compile(rf"<(?P<tag>{_REMINDER_TAGS}){_TAG_ATTRIBUTES}>")
_USER_QUERY_START = re.compile(r"<(?P<tag>user_query)>")
_QUERY_SUFFIX = re.compile(rf"<(?P<tag>{_REMINDER_TAGS}|image_local_path){_TAG_ATTRIBUTES}>")
_HOST_GENERATED_USER_TURN = re.compile(
    r"(?:"
    r"^<dynamic_tool_catalog>|"
    r"^Briefly inform the user about the task result and perform any follow-up actions|"
    r"^Perform any necessary follow-up actions in response to the subagent completion above"
    r")",
    re.IGNORECASE,
)
_PIPELINE_JSON_SIGNATURE = re.compile(
    r'^\s*\{[\s\S]*"request_id"\s*:[\s\S]*'
    r'(?:(?:"type"\s*:\s*"(?:write|search|delete|digest)"[\s\S]*"steps"\s*:)|'
    r'(?:"reader"\s*:\s*"client"[\s\S]*"recommended_order"\s*:))',
    re.IGNORECASE,
)
_PIPELINE_TYPES = {"write", "search", "delete", "digest"}

REDACTED_SECRET = "<redacted-secret>"  # noqa: S105 - public replacement marker, not a credential
_PRIVATE_KEY = re.compile(
    r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----(?:.*?-----END [A-Z0-9 ]*PRIVATE KEY-----|.*\Z)",
    re.DOTALL,
)
_AUTHORIZATION = re.compile(
    r"(?i)\b(Authorization\s*:\s*(?:Bearer|Basic|Token|ApiKey))\s+[A-Za-z0-9._~+/=-]{8,}"
)
_CLI_SECRET = re.compile(
    r"(?i)(--(?:password|passwd|token|api[-_]?key|apikey|appkey|secret))"
    r"(\s+)([^\s\"']+|\"[^\"]+\"|'[^']+')"
)
_ASSIGNED_SECRET = re.compile(
    r"(?i)(?P<key_quote>[\"']?)(?P<key>password|passwd|token|api[-_]?key|"
    r"apikey|appkey|secret|access[-_]?key|secret[-_]?key)(?P=key_quote)"
    r"(?P<separator>\s*[:=]\s*)"
    r"(?P<value>[^\s,;\"']+|\"[^\"]+\"|'[^']+')"
)
_KNOWN_TOKEN = re.compile(
    r"\b(?:sk-[A-Za-z0-9_-]{12,}|ghp_[A-Za-z0-9]{20,}|"
    r"github_pat_[A-Za-z0-9_]{20,}|AKID[A-Za-z0-9]{12,})\b"
)
_URL_CREDENTIAL = re.compile(
    r"(?P<scheme>https?://)(?P<user>[^/\s:@]+):(?P<password>[^/\s@]+)@", re.IGNORECASE
)


def _redact_assignment(match: re.Match[str]) -> str:
    value = match.group("value")
    quote = value[0] if value.startswith(("'", '"')) else ""
    key = f"{match.group('key_quote')}{match.group('key')}{match.group('key_quote')}"
    return f"{key}{match.group('separator')}{quote}{REDACTED_SECRET}{quote}"


_SANITIZE_STEPS: tuple[tuple[re.Pattern[str], str | Callable[[re.Match[str]], str]], ...] = (
    (_PRIVATE_KEY, REDACTED_SECRET),
    (_AUTHORIZATION, rf"\1 {REDACTED_SECRET}"),
    (_CLI_SECRET, rf"\1\2{REDACTED_SECRET}"),
    (_ASSIGNED_SECRET, _redact_assignment),
    (_KNOWN_TOKEN, REDACTED_SECRET),
    (
        _URL_CREDENTIAL,
        lambda match: f"{match.group('scheme')}{match.group('user')}:{REDACTED_SECRET}@",
    ),
)


def sanitize_model_input(value: str) -> str:
    """Remove credentials before a text crosses into a model."""
    for pattern, replacement in _SANITIZE_STEPS:
        value = pattern.sub(replacement, value)
    return value


def is_harness_injection(text: str, scan_characters: int) -> bool:
    """Whether a user-side text is harness context rather than a person's turn."""
    source = text.strip()
    if source.casefold().startswith(HARNESS_PREFIXES):
        return True
    return _HOST_ENVELOPE.search(source[:scan_characters]) is not None


def is_host_generated_user_turn(value: str) -> bool:
    """Whether a user-side text was generated by the host, not typed."""
    stripped = value.lstrip()
    if _HOST_GENERATED_USER_TURN.match(stripped) is not None:
        return True
    if not stripped.startswith("{"):
        return False
    if _PIPELINE_JSON_SIGNATURE.match(stripped) is not None:
        return True
    try:
        decoded = json.loads(stripped)
    except json.JSONDecodeError:
        return False
    if not isinstance(decoded, dict):
        return False
    payload = cast(dict[str, Any], decoded)
    return (
        isinstance(payload.get("request_id"), str)
        and payload.get("type") in _PIPELINE_TYPES
        and isinstance(payload.get("steps"), list)
    )


def text_content(value: Any, allowed_block_types: frozenset[str]) -> str | None:
    """A message's text: a plain string, or its allowed blocks' text joined by newlines."""
    if isinstance(value, str):
        return value.strip() or None
    if not isinstance(value, list):
        return None
    parts: list[str] = []
    for raw in cast(list[Any], value):
        if not isinstance(raw, dict):
            continue
        item = cast(dict[str, Any], raw)
        text = item.get("text")
        if item.get("type") in allowed_block_types and isinstance(text, str) and text.strip():
            parts.append(text.strip())
    return "\n".join(parts) or None


def strip_host_wrappers(value: str) -> str | None:
    """A user text with the host's outer envelope blocks removed (MemHub's legacy text)."""
    remaining = value.strip()
    has_context = False
    while opening := _HOST_PREFIX.match(remaining):
        closing = _closing_tag(remaining, opening)
        if closing is None:
            return value.strip() or None
        has_context |= _REMINDER_START.match(remaining) is None
        remaining = remaining[closing.end() :].lstrip()
    if query := _USER_QUERY_START.match(remaining):
        closing = _closing_tag(remaining, query)
        if closing is not None and _only_query_suffix(remaining[closing.end() :]):
            return remaining[query.end() : closing.start()].strip() or None
        return value.strip() or None
    if remaining and has_context:
        return value.strip() or None
    end, _valid = _reminder_suffix_end(remaining)
    return remaining[:end].strip() or None


def _closing_tag(value: str, opening: re.Match[str]) -> re.Match[str] | None:
    tag = re.escape(opening.group("tag"))
    tokens = re.compile(rf"</{tag}\s*>|<{tag}{_TAG_ATTRIBUTES}>")
    depth = 1
    for token in tokens.finditer(value, opening.end()):
        depth += -1 if token.group().startswith("</") else 1
        if depth == 0:
            return token
    return None


def _reminder_suffix_end(value: str) -> tuple[int, bool]:
    position = 0
    suffix_start: int | None = None
    while opening := _REMINDER_START.search(value, position):
        closing = _closing_tag(value, opening)
        if closing is None:
            return len(value), False
        if suffix_start is None or value[position : opening.start()].strip():
            suffix_start = opening.start()
        position = closing.end()
    if suffix_start is not None and not value[position:].strip():
        return suffix_start, True
    return len(value), True


def _only_query_suffix(value: str) -> bool:
    remaining = value.strip()
    while opening := _QUERY_SUFFIX.match(remaining):
        closing = _closing_tag(remaining, opening)
        if closing is None:
            return False
        remaining = remaining[closing.end() :].lstrip()
    return not remaining
