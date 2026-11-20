"""Neutralized ClaudeRecordPolicy: preserve dialogue and compact summaries."""

from pydantic import JsonValue

from anamnema.archive_models import Kind
from anamnema.substrates.base import SubstrateAdapter


class ClaudeAdapter(SubstrateAdapter):
    """Claude semantics only; the shared base owns reading and provenance."""

    def classify(self, source: dict[str, JsonValue]) -> Kind | None:
        """Keep the existing policy, including the legacy A conversation type."""
        schema = self.settings.reader
        record_type = source.get(schema.record_type)
        if source.get(schema.summary_flag) is True or record_type in schema.summary_types:
            return "summary"
        return "conversation" if record_type in schema.conversation_types else None
