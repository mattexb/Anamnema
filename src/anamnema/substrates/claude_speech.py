"""Claude-only native message translation, adapted from the reviewed reader."""

import re
from typing import Literal

from anamnema.archive_models import Record
from anamnema.memhub_parsing import (
    is_harness_injection,
    is_host_generated_user_turn,
    strip_host_wrappers,
    text_content,
)
from anamnema.speech import Row
from anamnema.substrates.speech_base import SpeechReader


class ClaudeSpeechReader(SpeechReader):
    """Tool/peer/relay/injected rows never become owner speech for the model."""

    def translate(self, record: Record, number: int) -> Row | str:
        """Extract only native dialogue text; archive still retains every original block."""
        schema = self.policy.claude
        source = record.source
        if source.get(schema.sidechain_key):
            return "sidechain"
        message = source.get(schema.message_key)
        content = message.get(schema.content_key) if isinstance(message, dict) else message
        text = text_content(content, frozenset({schema.text_block_type}))
        if text is None:
            return "no_dialogue_text"
        if source.get(schema.meta_key):
            return "peer" if text.startswith(schema.peer_prefix) else "injected_meta"
        if text.strip() in schema.tool_placeholders:
            return "tool_placeholder"
        text = re.sub(schema.artifact_marker_pattern, "", text)
        native_type = source.get(schema.type_key)
        speaker: Literal["owner", "agent"]
        if native_type == schema.assistant_type:
            speaker = "agent"
        elif native_type in schema.user_types:
            speaker = "owner"
            if text.startswith(schema.injected_prefixes):
                return "injected"
            if is_harness_injection(
                text, schema.harness_scan_characters
            ) or is_host_generated_user_turn(text):
                return "harness"
        else:
            return "unsupported_native_role"
        stripped = strip_host_wrappers(text)
        if not stripped:
            return "no_dialogue_text"
        if speaker == "owner" and is_harness_injection(stripped, schema.harness_scan_characters):
            return "harness"
        return Row(number, speaker, stripped, record)
