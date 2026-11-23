"""Owned atomic judgement, lesson history and evidence storage following KinTools 0.7.21."""

from __future__ import annotations

import json
from typing import Any

from neo4j import ManagedTransaction
from pydantic import JsonValue

from anamnema.archive_models import canonical, digest
from anamnema.graph_store import GraphStore, MemoryConflict, MemoryDenied, query, vector_check
from anamnema.lesson_models import (
    CONTENT_FIELDS,
    NOTE_CONTENT_FIELDS,
    NOTE_FIELDS,
    SNAPSHOT_FIELDS,
    LessonAgainst,
    LessonDocument,
    LessonJudgement,
    LessonWrite,
)
from anamnema.setup_config import SetupError


class LessonPending(SetupError):
    """A correction invalidated this lesson; history remains available but is not current."""


class LessonGraph:
    """Content, source links and full prior snapshots commit together in an explicit store."""

    def __init__(self, store: GraphStore) -> None:
        self.store = store

    def get(self, instance: str, identity: str, *, current: bool = True) -> dict[str, Any]:
        """Read one owned lesson; explicitly historical reads may inspect a pending rebuild."""
        rows = self.store.read(
            f"MATCH (l:{self.store.settings.lesson_label} {{id:$id,instance:$instance}}) "
            "RETURN l {.*,passage_vector:null,judged_at:toString(l.judged_at),"
            "created_at:toString(l.created_at)} AS lesson",
            id=digest([instance, identity]),
            instance=instance,
        )
        if not rows:
            raise MemoryDenied("lesson is absent or belongs to another assistant")
        result = rows[0]["lesson"]
        if current and not result["valid"]:
            raise LessonPending("lesson rebuild is pending for corrected evidence")
        result["routes"] = json.loads(result["routes"])
        result["history"] = [json.loads(item) for item in result["history"]]
        result["episodes"] = [json.loads(item) for item in result["episodes"]]
        result["against"] = [json.loads(item) for item in result.get("against", [])]
        return result

    def sources(self, instance: str, notes: tuple[str, ...]) -> list[dict[str, Any]]:
        """Bound input snapshots before model generation; no cross-owner source may enter it."""
        if not instance or not notes or len(notes) > self.store.settings.max_items:
            raise SetupError("lesson source input violates configured graph bounds")
        if len(set(notes)) != len(notes):
            raise SetupError("lesson source input repeats a note")
        return [self._source(instance, note) for note in sorted(notes)]

    def assert_revision(
        self, instance: str, identity: str, revision: int, *, current: bool
    ) -> None:
        """A multi-read view must not publish a mixed pre/post-correction snapshot."""
        rows = self.store.read(
            f"MATCH (l:{self.store.settings.lesson_label} {{id:$id,instance:$instance}}) "
            "RETURN l.revision AS revision,l.valid AS valid",
            id=digest([instance, identity]),
            instance=instance,
        )
        if not rows:
            raise MemoryDenied("lesson is absent or belongs to another assistant")
        if current and not rows[0]["valid"]:
            raise LessonPending("lesson changed while evidence was read; rebuild pending")
        if rows[0]["revision"] != revision:
            raise MemoryConflict("lesson changed while evidence was read; retry")

    def _source(self, instance: str, identity: str) -> dict[str, Any]:
        note = self.store.note(instance, identity)
        payload = {field: note[field] for field in NOTE_FIELDS}
        return {"note": identity, **payload, "token": digest(payload)}

    def pending(self, instance: str, identities: tuple[str, ...]) -> tuple[str, ...]:
        """Select unjudged owned inputs; committed or unresolved inputs need explicit rejudging."""
        if (
            not instance
            or not identities
            or len(set(identities)) != len(identities)
            or len(identities) > self.store.settings.max_items
        ):
            raise SetupError("lesson input identities violate configured graph bounds")
        rows = self.store.read(
            f"MATCH (n:{self.store.settings.note_label} {{instance:$instance}}) "
            "WHERE n.id IN $ids RETURN n.note_id AS note,n.lesson_state AS state",
            instance=instance,
            ids=[digest([instance, identity]) for identity in identities],
        )
        states = {row["note"]: row["state"] for row in rows}
        if set(states) != set(identities):
            raise MemoryDenied("lesson input needs owned existing notes")
        return tuple(identity for identity in identities if states[identity] is None)

    def write(
        self,
        instance: str,
        lesson: LessonDocument,
        revision: int,
        source_tokens: dict[str, str],
    ) -> int:
        """A single-lesson judgement retains the same source and revision guards."""
        return self.write_judgement(
            instance,
            LessonJudgement(
                lessons=(LessonWrite(lesson=lesson, revision=revision),),
                source_tokens=source_tokens,
                model=lesson.model,
            ),
        )[0]

    def write_judgement(self, instance: str, judgement: LessonJudgement) -> list[int]:
        """Commit all lessons, history, links and note decisions of one judgement together."""
        settings = self.store.settings
        if not instance or any(
            len(items) > settings.max_items
            for items in (
                judgement.lessons,
                judgement.unresolved,
                judgement.source_tokens,
            )
        ):
            raise SetupError("lesson judgement exceeds configured item bounds")
        if len(canonical(judgement.model_dump(mode="json"))) > settings.max_source_bytes:
            raise SetupError("lesson judgement exceeds configured byte bound")
        for item in judgement.lessons:
            self._bounded(instance, item.lesson, item.revision)

        def work(tx: ManagedTransaction) -> list[int]:
            sources = self._sources_in(tx, instance, judgement)
            versions = {
                item.lesson.id: self._write_in(tx, instance, item, sources)
                for item in sorted(judgement.lessons, key=lambda item: item.lesson.id)
            }
            self._mark_judged(tx, instance, judgement)
            return [versions[item.lesson.id] for item in judgement.lessons]

        return self.store.write(work)

    def _write_in(
        self, tx: ManagedTransaction, instance: str, item: LessonWrite, sources: dict[str, Any]
    ) -> int:
        lesson = item.lesson
        evidence_tokens = self._evidence(lesson, sources)
        previous = self._locked(tx, instance, lesson.id)
        if previous["revision"] != item.revision:
            raise MemoryConflict("lesson inputs changed during generation; rebuild pending")
        content = lesson.model_dump(mode="json", exclude={"embedding", "episodes", "concepts"})
        content["routes"] = canonical(content["routes"]).decode("utf-8")
        changed = previous["version"] > 0 and any(
            previous.get(field) != content[field] for field in CONTENT_FIELDS
        )
        history = list(previous["history"])
        if changed:
            snapshot = {field: previous.get(field) for field in SNAPSHOT_FIELDS}
            history.append(canonical(snapshot).decode("utf-8"))
        if len(history) > self.store.settings.max_items:
            raise SetupError("lesson history exceeds configured graph bound")
        version = previous["version"] + int(changed or previous["version"] == 0)
        self._save(tx, instance, lesson, previous, content, history, version, evidence_tokens)
        return version

    def _bounded(self, instance: str, lesson: LessonDocument, revision: int) -> None:
        settings = self.store.settings
        if not instance or type(revision) is not int or revision < 0:
            raise SetupError("lesson requires an instance and nonnegative input revision")
        vector_check(lesson.embedding, settings)
        values = lesson.model_dump(mode="json", exclude={"embedding"})
        if len(canonical(values)) > settings.max_source_bytes:
            raise SetupError("lesson payload exceeds configured byte bound")
        if any(
            len(items) > settings.max_items
            for items in (lesson.routes, lesson.episodes, lesson.concepts)
        ):
            raise SetupError("lesson item count exceeds configured graph bound")
        if any(
            len(text) > settings.max_note_characters
            for text in (
                lesson.statement,
                lesson.mechanism,
                lesson.conflict,
            )
        ):
            raise SetupError("lesson text exceeds configured graph bound")

    def _sources_in(
        self, tx: ManagedTransaction, instance: str, judgement: LessonJudgement
    ) -> dict[str, Any]:
        # Lock every input before any derived node, in one order across the whole batch.
        sources: dict[str, Any] = {}
        for identity, token in sorted(judgement.source_tokens.items()):
            rows = query(
                tx,
                f"MATCH (n:{self.store.settings.note_label} {{id:$id,instance:$instance}}) "
                "SET n.lesson_lock=true RETURN n AS note",
                id=digest([instance, identity]),
                instance=instance,
            )
            if not rows:
                raise MemoryDenied("lesson episode needs an owned existing note")
            note = rows[0]["note"]
            if digest({field: note[field] for field in NOTE_FIELDS}) != token:
                raise MemoryConflict("lesson evidence changed during generation; rebuild pending")
            if note["wrong_open"] or note["status"] != "active":
                raise MemoryDenied("wrong or historical evidence cannot support a current lesson")
            if judgement.pending_only and note.get("lesson_state") is not None:
                raise MemoryConflict("lesson input was already judged; retry pending selection")
            sources[identity] = note
            query(
                tx,
                f"MATCH (n:{self.store.settings.note_label} {{id:$id}}) REMOVE n.lesson_lock",
                id=digest([instance, identity]),
            )
        return sources

    def _evidence(self, lesson: LessonDocument, sources: dict[str, Any]) -> dict[str, str]:
        evidence_tokens: dict[str, str] = {}
        for episode in lesson.episodes:
            note = sources[episode.note]
            if set(episode.records) != set(note["source_ids"]):
                raise MemoryDenied("lesson episode records must equal its note's actual sources")
            evidence_tokens[episode.note] = digest(
                {field: note[field] for field in NOTE_CONTENT_FIELDS}
            )
        return evidence_tokens

    def _mark_judged(
        self, tx: ManagedTransaction, instance: str, judgement: LessonJudgement
    ) -> None:
        placed = {episode.note for item in judgement.lessons for episode in item.lesson.episodes}
        for state, identities in (
            ("episode", sorted(placed)),
            ("unresolved", judgement.unresolved),
        ):
            query(
                tx,
                f"MATCH (n:{self.store.settings.note_label} {{instance:$instance}}) "
                "WHERE n.id IN $ids SET n.lesson_state=$state,n.lesson_model=$model,"
                "n.lesson_at=datetime()",
                instance=instance,
                ids=[digest([instance, identity]) for identity in identities],
                state=state,
                model=judgement.model,
            )

    def _locked(self, tx: ManagedTransaction, instance: str, identity: str) -> dict[str, Any]:
        return query(
            tx,
            f"MERGE (l:{self.store.settings.lesson_label} {{id:$id}}) "
            "ON CREATE SET l.instance=$instance,l.lesson_id=$identity,l.revision=0,l.version=0,"
            "l.history=[],l.episodes=[],l.created_at=datetime() "
            "SET l.write_lock=true "
            "RETURN l {.*,judged_at:toString(l.judged_at)} AS lesson",
            id=digest([instance, identity]),
            instance=instance,
            identity=identity,
        )[0]["lesson"]

    def _save(
        self,
        tx: ManagedTransaction,
        instance: str,
        lesson: LessonDocument,
        previous: dict[str, Any],
        content: dict[str, Any],
        history: list[str],
        version: int,
        source_tokens: dict[str, str],
    ) -> None:
        s, key = self.store.settings, digest([instance, lesson.id])
        episodes = {json.loads(item)["note"]: item for item in previous["episodes"]}
        episodes.update(
            {
                item.note: canonical(
                    {**item.model_dump(mode="json"), "content_token": source_tokens[item.note]}
                ).decode("utf-8")
                for item in lesson.episodes
            }
        )
        if len(episodes) > s.max_items:
            raise SetupError("lesson evidence history exceeds configured graph bound")
        stored: dict[str, JsonValue] = {
            "content": content,
            "history": [entry for entry in history],
            "episodes": list(episodes.values()),
        }
        if len(canonical(stored)) > s.max_source_bytes:
            raise SetupError("lesson history/evidence exceed configured byte bound")
        query(
            tx,
            f"MATCH (l:{s.lesson_label} {{id:$id}}) SET l += $content,"
            "l.id=$id,l.lesson_id=$identity,l.version=$version,l.history=$history,"
            "l.episodes=$episodes,l.concepts=$concepts,l.passage_vector=$vector,"
            "l.valid=true,l.judged_at=datetime(),"
            "l.revision=l.revision+1 REMOVE l.write_lock",
            id=key,
            identity=lesson.id,
            content=content,
            history=history,
            version=version,
            episodes=list(episodes.values()),
            concepts=list(lesson.concepts),
            vector=list(lesson.embedding),
        )
        query(tx, f"MATCH (l:{s.lesson_label} {{id:$id}})-[r:APPLIES_WHEN]->() DELETE r", id=key)
        for name in lesson.concepts:
            query(
                tx,
                f"MATCH (l:{s.lesson_label} {{id:$id}}) "
                f"MERGE (c:{s.concept_label} {{id:$concept}}) "
                "ON CREATE SET c.instance=$instance,c.name=$name "
                "MERGE (l)-[:APPLIES_WHEN]->(c)",
                id=key,
                concept=digest([instance, name]),
                instance=instance,
                name=name,
            )
        for episode in lesson.episodes:
            query(
                tx,
                f"MATCH (l:{s.lesson_label} {{id:$id}}),"
                f"(n:{s.note_label} {{id:$note,instance:$instance}}) "
                "MERGE (n)-[r:EPISODE_OF]->(l) SET r=$props,r.model=$model,r.at=datetime()",
                id=key,
                note=digest([instance, episode.note]),
                instance=instance,
                props=episode.model_dump(mode="json"),
                model=lesson.model,
            )

    def affected(self, instance: str, check: str) -> list[str]:
        """Deletion cannot erase which lessons the correction owes a rebuild."""
        rows = self.store.read(
            f"MATCH (c:{self.store.settings.check_label} {{id:$id,instance:$instance}}) "
            "RETURN coalesce(c.affected_lessons,[]) AS lessons",
            id=digest([instance, check]),
            instance=instance,
        )
        return list(rows[0]["lessons"]) if rows else []

    def add_against(
        self,
        instance: str,
        identity: str,
        nomination: LessonAgainst,
        revision: int,
        source_token: str,
    ) -> None:
        """Nominate one source-backed event; repeats never manufacture additional votes."""
        s = self.store.settings
        if not instance or type(revision) is not int or revision < 1:
            raise SetupError("counterexample requires an owned lesson revision")
        if any(
            not text.strip() or len(text) > s.max_note_characters
            for text in (nomination.condition, nomination.reason, nomination.judged_by)
        ):
            raise SetupError("counterexample requires bounded condition, reason and judge")

        def work(tx: ManagedTransaction) -> None:
            rows = query(
                tx,
                f"MATCH (n:{s.note_label} {{id:$id,instance:$instance}}) "
                "SET n.lesson_lock=true RETURN n AS note",
                id=digest([instance, nomination.note]),
                instance=instance,
            )
            if not rows:
                raise MemoryDenied("counterexample needs an owned source note")
            note = rows[0]["note"]
            if digest({field: note[field] for field in NOTE_FIELDS}) != source_token:
                raise MemoryConflict("counterexample source changed during judgement")
            if note["wrong_open"] or note["status"] != "active" or not nomination.records:
                raise MemoryDenied("counterexample needs eligible source-backed evidence")
            if set(nomination.records) != set(note["source_ids"]):
                raise MemoryDenied("counterexample records differ from its actual source note")
            previous = self._locked(tx, instance, identity)
            if previous["version"] == 0:
                raise MemoryDenied("counterexample target lesson is absent")
            if previous["revision"] != revision:
                raise MemoryConflict("counterexample lesson changed during judgement")
            kept = {json.loads(item)["note"]: item for item in previous.get("against", [])}
            payload = {
                **nomination.model_dump(mode="json"),
                "source_token": source_token,
                "content_token": digest({field: note[field] for field in NOTE_CONTENT_FIELDS}),
            }
            kept[nomination.note] = canonical(payload).decode("utf-8")
            if len(kept) > s.max_items:
                raise SetupError("counterexample history exceeds configured graph bound")
            if len(canonical(list(kept.values()))) > s.max_source_bytes:
                raise SetupError("counterexample history exceeds configured byte bound")
            query(
                tx,
                f"MATCH (l:{s.lesson_label} {{id:$id}}),"
                f"(n:{s.note_label} {{id:$note}}) MERGE (n)-[r:AGAINST]->(l) "
                "SET r=$payload,r.at=datetime(),l.against=$against,l.revision=l.revision+1 "
                "REMOVE l.write_lock,n.lesson_lock",
                id=digest([instance, identity]),
                note=digest([instance, nomination.note]),
                payload=payload,
                against=list(kept.values()),
            )

        self.store.write(work)
