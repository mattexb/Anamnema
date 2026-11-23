"""Owned evidence to structured recall and an immutable, independently verified bundle."""

from typing import Any

from pydantic import JsonValue, TypeAdapter

from anamnema.archive_models import canonical, digest
from anamnema.briefing_condense import condense
from anamnema.briefing_files import BriefFiles
from anamnema.briefing_inputs import prepare_briefing
from anamnema.briefing_models import BriefAnswer, BriefLedger, CombinedBrief
from anamnema.briefing_policy import BriefingPolicy
from anamnema.briefing_render import render_briefing
from anamnema.briefing_select import BriefSelector, brief_selector, select_briefing
from anamnema.graph_store import GraphStore
from anamnema.memory_sources import SourceLookup
from anamnema.native_judge import NativeJudge
from anamnema.setup_config import SetupError
from anamnema.speech import scrub
from anamnema.speech_policy import SpeechPolicy

JSON_VALUE = TypeAdapter(JsonValue)
JSON_OBJECT = TypeAdapter(dict[str, JsonValue])


class BriefBuilder:
    """A failed read never becomes processed; selection alone is never successful recall."""

    def __init__(
        self,
        graph: GraphStore,
        sources: SourceLookup,
        judge: NativeJudge,
        policy: BriefingPolicy,
        speech: SpeechPolicy,
        files: BriefFiles,
    ) -> None:
        self.graph, self.sources, self.judge = graph, sources, judge
        self.policy, self.speech, self.files = policy, speech, files

    def rebuild(self, instance: str, identity: str) -> dict[str, Any]:
        """Refresh the same declared focus; unchanged ready snapshots need no model call."""
        previous, pages = self.files.read(instance, identity)
        subject, keys = previous.metadata.get("subject"), previous.metadata.get("selectors")
        if not isinstance(subject, str) or not subject.strip() or not isinstance(keys, list):
            raise SetupError("briefing has no valid rebuild subject or selector provenance")
        if not keys or any(not isinstance(key, str) for key in keys):
            raise SetupError("briefing rebuild selectors are invalid")
        selectors = tuple(brief_selector(key) for key in keys if isinstance(key, str))
        selected = select_briefing(self.graph, instance, selectors, self.graph.settings.max_items)
        unchanged = (
            previous.metadata.get("status") == "ready"
            and previous.metadata.get("input") == digest(JSON_VALUE.validate_python(selected.notes))
            and previous.metadata.get("policy") == digest(self.policy.model_dump(mode="json"))
            and previous.metadata.get("speech") == digest(self.speech.model_dump(mode="json"))
            and previous.metadata.get("judge") == self.judge.identity
            and previous.metadata.get("unresolved") == list(selected.unresolved)
        )
        if unchanged:
            return {
                "briefing": identity,
                "status": "ready",
                "pages": [str(page) for page in pages],
                "metadata": previous.metadata,
                "unchanged": True,
                "rebuilt_from": identity,
            }
        return {
            **self.build(instance, subject, selectors),
            "unchanged": False,
            "rebuilt_from": identity,
        }

    def build(
        self, instance: str, subject: str, selectors: tuple[BriefSelector, ...]
    ) -> dict[str, Any]:
        """Generate only from owned source-verified notes; persist partial accounting visibly."""
        subject = " ".join(scrub(subject, self.speech)[0].split())
        if not subject:
            raise SetupError("briefing subject is empty")
        selected = select_briefing(self.graph, instance, selectors, self.graph.settings.max_items)
        if not selected.notes:
            raise SetupError("briefing selectors resolved no owned notes; nothing generated")
        ledger = BriefLedger(tuple(str(note["id"]) for note in selected.notes))
        ledger.problems.extend("unresolved selector: " + key for key in selected.unresolved)
        sources: dict[str, list[str]] = {}
        eligible: list[dict[str, Any]] = []
        for note in selected.notes:
            identity = str(note["id"])
            try:
                sources[identity] = self._rows(instance, note)
            except SetupError as exc:
                ledger.problems.append("source unavailable: " + identity + ": " + str(exc))
                continue
            eligible.append(note)
        inputs = prepare_briefing(eligible, sources, self.policy, self.speech)
        models: set[str] = set()
        for batch in inputs.batches:
            prompt = self.policy.instructions + "\n\nSubject: " + subject + "\n\n" + batch.text
            try:
                result = self.judge.judge(prompt, BriefAnswer)
            except SetupError as exc:
                ledger.problems.append("read batch failed: " + str(exc))
                continue
            ledger.read(result.answer, batch.labels)
            models.update(result.models)
        if len(inputs.batches) > 1 and ledger.claims:
            self._combine(subject, ledger, models)
        rendered = render_briefing(subject, ledger, self.policy)
        condensed = False
        if ledger.claims and rendered.tokens_estimated[0] > self.policy.ceiling:
            header = rendered.pages[0].split("\n## ", 1)[0]
            result = condense(subject, ledger, self.policy, self.judge, header)
            if result is not None:
                ledger.claims = result.front
                models.update(result.models)
                rendered = render_briefing(
                    subject, ledger, self.policy, carried_overflow=result.overflow
                )
                condensed = True
        metadata: dict[str, Any] = {
            "subject": subject,
            "status": ledger.status,
            "selected": list(ledger.selected),
            "read": sorted(ledger.processed),
            "unread": sorted(set(ledger.selected) - ledger.processed),
            "selectors": [selector.key for selector in selectors],
            "unresolved": list(selected.unresolved),
            "problems": list(ledger.problems),
            "models": sorted(models),
            "judge": self.judge.identity,
            "policy": digest(self.policy.model_dump(mode="json")),
            "speech": digest(self.speech.model_dump(mode="json")),
            "input": digest(JSON_VALUE.validate_python(selected.notes)),
            "broad": inputs.broad,
            "tokens_estimated": list(rendered.tokens_estimated),
            "oversized_pages": list(rendered.oversized_pages),
            "condensed": condensed,
        }
        identity = self.files.save(instance, rendered, JSON_OBJECT.validate_python(metadata))
        manifest, pages = self.files.read(instance, identity)
        return {
            "briefing": identity,
            "status": ledger.status,
            "pages": [str(p) for p in pages],
            "metadata": manifest.metadata,
            "condensed": condensed,
        }

    def _rows(self, instance: str, note: dict[str, Any]) -> list[str]:
        opened = self.sources.open_note(self.graph, instance, str(note["id"]))
        context, rows = opened.get("context"), opened.get("rows")
        if not isinstance(context, dict) or context.get("status") == "unavailable":
            raise SetupError("briefing requires verified attributed source context")
        if not isinstance(rows, list) or not rows:
            raise SetupError("briefing source context has no dialogue rows")
        valid = [row for row in rows if isinstance(row, dict)]
        cited = {index for index, row in enumerate(valid) if row.get("cited") is True}
        if not cited:
            raise SetupError("briefing source context has no attributed cited rows")
        chosen = sorted(
            sorted(range(len(valid)), key=lambda i: (i not in cited, i))[: self.policy.source_rows]
        )
        return [
            f"{valid[index].get('speaker', '')}: {valid[index].get('text', '')}" for index in chosen
        ]

    def _combine(self, subject: str, ledger: BriefLedger, models: set[str]) -> None:
        labels = {f"n{index}": identity for index, identity in enumerate(ledger.selected, 1)}
        reverse = {identity: label for label, identity in labels.items()}
        claims = [
            {
                **claim.model_dump(mode="json"),
                "notes": [reverse[identity] for identity in claim.notes],
            }
            for claim in ledger.claims
        ]
        prompt = (
            self.policy.combine_instructions
            + "\n\nSubject: "
            + subject
            + "\n\n"
            + canonical(JSON_VALUE.validate_python({"claims": claims})).decode("utf-8")
        )
        try:
            result = self.judge.judge(prompt, CombinedBrief)
        except SetupError as exc:
            ledger.problems.append("combine failed; partial claims retained: " + str(exc))
            return
        ledger.combine(result.answer, labels)
        models.update(result.models)
