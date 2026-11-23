"""Source repeat candidate/chain logic; judgments never mutate an older note."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from anamnema.graph_search import GraphSearch
from anamnema.graph_store import GraphStore, MemoryDenied
from anamnema.graph_topics import cosine
from anamnema.memory_models import MemoryNote
from anamnema.merge_graph import MergeGraph, source_key
from anamnema.merge_judge import MergeJudge, Pair, Verdict
from anamnema.merge_policy import MergePolicy
from anamnema.setup_config import SetupError

LOGGER = logging.getLogger(__name__)


@dataclass
class MergeResult:
    """Insertion proposals and occurrences are persisted by the caller before placement."""

    kept: list[MemoryNote]
    occurrences: list[dict[str, Any]] = field(default_factory=list[dict[str, Any]])
    judged: int = 0
    deferred: int = 0
    changed: int = 0
    replayed: int = 0


class RepeatMerger:
    """Compare earlier in-run and stored active notes, then resolve repeat chains."""

    def __init__(self, store: GraphStore, policy: MergePolicy, judge: MergeJudge) -> None:
        if not policy.enabled or policy.candidates + 1 > store.settings.max_items:
            raise SetupError("repeat merge is disabled or candidate budget exceeds graph bound")
        self.store, self.policy, self.judge = store, policy, judge
        self.graph = MergeGraph(store)

    def candidates(self, fresh: list[MemoryNote]) -> list[Pair]:
        """Cosine selects pairs only; wrong/outdated notes are never merge targets."""
        pairs: list[Pair] = []
        earlier: dict[str, MemoryNote] = {}
        for note in fresh:
            close = dict(
                GraphSearch(self.store).meaning(
                    note.instance, note.passage_vector, self.policy.candidates + 1
                )
            )
            close.update(
                {
                    old.id: cosine(note.passage_vector, old.passage_vector)
                    for old in earlier.values()
                }
            )
            close.pop(note.id, None)
            ranked = sorted(close.items(), key=lambda pair: (-pair[1], pair[0]))
            for identity, score in ranked[: self.policy.candidates]:
                if score < self.policy.similarity:
                    continue
                try:
                    old = (
                        earlier[identity].model_dump(mode="json")
                        if identity in earlier
                        else self.store.note(note.instance, identity)
                    )
                except MemoryDenied:
                    LOGGER.info("repeat candidate disappeared before judgment")
                    continue
                if old.get("status", "active") == "active" and not old.get("wrong_open"):
                    pairs.append(Pair(note, old, score))
            earlier[note.id] = note
        return pairs

    def plan(self, fresh: list[MemoryNote]) -> MergeResult:
        """Failed/incomplete batches persist visible deferrals rather than speculative merges."""
        if not fresh:
            return MergeResult([])
        instance = fresh[0].instance
        if any(note.instance != instance for note in fresh):
            raise SetupError("repeat input mixes assistant instances")
        known = self.graph.known_sources(instance, fresh)
        remaining = [note for note in fresh if source_key(note) not in known]
        pairs = self.candidates(remaining)
        done: list[tuple[Pair, Verdict]] = []
        deferred: dict[str, str] = {}
        audit: list[dict[str, Any]] = []
        for start in range(0, len(pairs), self.policy.batch_pairs):
            part = pairs[start : start + self.policy.batch_pairs]
            found: dict[int, Verdict] = {}
            failure = "repeat judge gave no valid verdict for this pair"
            try:
                found = self.judge.verdicts(part)
            except SetupError as exc:
                LOGGER.warning("repeat judgment deferred: %s", exc)
                failure = str(exc)
            for index, pair in enumerate(part):
                verdict = found.get(index)
                if verdict is None:
                    deferred[pair.new.id] = failure
                else:
                    done.append((pair, verdict))
                audit.append(
                    {
                        "new": pair.new.id,
                        "old": pair.old["id"],
                        "score": pair.score,
                        "verdict": verdict.value if verdict else "deferred",
                        "model": self.judge.identity,
                    }
                )
        self.graph.audit(instance, audit)
        result = self._resolve(remaining, done, deferred)
        result.judged, result.replayed = len(pairs), len(fresh) - len(remaining)
        return result

    def _resolve(
        self, fresh: list[MemoryNote], done: list[tuple[Pair, Verdict]], deferred: dict[str, str]
    ) -> MergeResult:
        targets: dict[str, tuple[Pair, Verdict]] = {}
        changes: dict[str, str] = {}
        for pair, verdict in sorted(done, key=lambda item: -item[0].score):
            if verdict in (Verdict.CLAIM, Verdict.EVENT):
                targets.setdefault(pair.new.id, (pair, verdict))
            elif verdict == Verdict.CHANGED:
                changes.setdefault(pair.new.id, str(pair.old["id"]))
        occurrences: list[dict[str, Any]] = []
        for identity, (pair, verdict) in targets.items():
            root, seen = identity, {identity}
            while root in targets and str(targets[root][0].old["id"]) not in seen:
                root = str(targets[root][0].old["id"])
                seen.add(root)
            if root != identity and identity not in deferred:
                occurrences.append(
                    {
                        "note": pair.new,
                        "under": root,
                        "verdict": verdict.value,
                        "model": self.judge.identity,
                        "score": pair.score,
                    }
                )
        merged = {row["note"].id for row in occurrences}
        kept = [
            note.model_copy(
                update={
                    "merge_deferred": deferred.get(note.id),
                    "changes_note": changes.get(note.id),
                }
            )
            for note in fresh
            if note.id not in merged
        ]
        return MergeResult(
            kept,
            occurrences,
            deferred=sum(n.id in deferred for n in kept),
            changed=sum(n.id in changes for n in kept),
        )
