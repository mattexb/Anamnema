"""Source-verified naming batches with explicit writes and correction-safe persistence."""

from typing import Any

from anamnema.graph_store import GraphStore
from anamnema.memory_sources import SourceLookup
from anamnema.naming_graph import NamingGraph, SubjectWrite
from anamnema.naming_writer import NamingWriter
from anamnema.search_services import CPUEncoder
from anamnema.setup_config import SetupError


class NamingBuilder:
    """Model/encoder errors leave notes pending; only a current judged snapshot can land."""

    def __init__(
        self, store: GraphStore, writer: NamingWriter, encoder: CPUEncoder, sources: SourceLookup
    ) -> None:
        self.graph, self.writer, self.encoder, self.sources = (
            NamingGraph(store),
            writer,
            encoder,
            sources,
        )
        if writer.policy.batch > store.settings.max_items:
            raise SetupError("naming batch exceeds configured graph bounds")
        if encoder.policy.dimensions != store.settings.dimensions:
            raise SetupError("naming encoder and graph dimensions differ")

    def build(
        self, instance: str, identities: tuple[str, ...], *, apply: bool = False
    ) -> dict[str, Any]:
        """Preview still judges; only explicit apply embeds and writes the proposed subjects."""
        if type(apply) is not bool or not instance:
            raise SetupError("naming requires an owner and an explicit boolean apply mode")
        if len(identities) > self.graph.store.settings.max_items or len(set(identities)) != len(
            identities
        ):
            raise SetupError("naming selection exceeds graph bounds or repeats an identity")
        notes = [self.graph.store.note(instance, identity) for identity in identities]
        calls: list[dict[str, Any]] = []
        for start in range(0, len(notes), self.writer.policy.batch):
            batch = notes[start : start + self.writer.policy.batch]
            calls.append(self._batch(instance, batch, apply))
        return {
            "mode": "apply" if apply else "dry-run",
            "status": "partial"
            if any(call["left_pending"] or call["stale_or_absent"] for call in calls)
            else "success",
            "calls": calls,
            "stored": sum(call["stored"] for call in calls),
        }

    def _rows(self, instance: str, note: dict[str, Any]) -> list[str]:
        opened = self.sources.open_note(self.graph.store, instance, str(note["id"]))
        context, rows = opened.get("context"), opened.get("rows")
        if (
            not isinstance(context, dict)
            or context.get("status") == "unavailable"
            or not isinstance(rows, list)
            or not rows
        ):
            raise SetupError("naming requires verified attributed source context")
        selected = [row for row in rows if isinstance(row, dict) and row.get("cited") is True]
        if not selected:
            raise SetupError("naming context has no cited source rows")
        return [
            f"{row.get('speaker', '')}: {row.get('text', '')}"
            for row in selected[: self.writer.policy.source_rows]
        ]

    def _batch(self, instance: str, notes: list[dict[str, Any]], apply: bool) -> dict[str, Any]:
        if apply:
            self.graph.attempted(instance, notes)
        sources: dict[str, list[str]] = {}
        eligible: list[dict[str, Any]] = []
        problems: list[str] = []
        for note in notes:
            identity = str(note["id"])
            try:
                if len(str(note["text"])) > self.graph.store.settings.max_note_characters:
                    raise SetupError("naming text exceeds configured graph limit")
                sources[identity] = self._rows(instance, note)
            except SetupError as exc:
                problems.append(identity + ": " + str(exc))
                continue
            eligible.append(note)
        judgement = self.writer.judge(eligible, sources)
        by_id = {str(note["id"]): note for note in eligible}
        named = [item for item in judgement.subjects if item.known]
        vectors = (
            self.encoder.encode(
                [str(by_id[item.note]["text"]) for item in named],
                passage=True,
                subjects=[item.subject for item in named],
            )
            if apply and named
            else ()
        )
        vector_of = {item.note: vector for item, vector in zip(named, vectors, strict=apply)}
        proposals = (
            tuple(
                SubjectWrite(
                    note=item.note,
                    text=str(by_id[item.note]["text"]),
                    source_ids=tuple(by_id[item.note]["source_ids"]),
                    state="named" if item.known else "unknown",
                    subject=item.subject,
                    model=judgement.model,
                    version=self.writer.policy.version,
                    vector=vector_of.get(item.note),
                )
                for item in judgement.subjects
            )
            if apply
            else ()
        )
        stored = self.graph.write(instance, proposals) if apply else 0
        return {
            "notes": len(notes),
            "named": len(named),
            "unknown": len(judgement.subjects) - len(named),
            "left_pending": len(notes) - len(judgement.subjects),
            "stored": stored,
            "stale_or_absent": len(proposals) - stored,
            "model": judgement.model,
            "problems": problems + list(judgement.problems),
            "proposed": [
                {"note": item.note, "known": item.known, "subject": item.subject}
                for item in judgement.subjects
            ]
            if not apply
            else [],
        }
