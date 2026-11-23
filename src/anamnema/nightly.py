"""The nightly finishing chain after a window batch has stored its notes.

Each stage runs the installed command a person would run by hand, so the chain adds no second
code path: link due notes to entities, rebuild the cards those links made stale, name pending
notes, and aggregate advice notes not yet judged into lessons. What is due comes from the graph.
A failed stage is recorded and the chain goes on: the stages are independent and every one of
them leaves its own work pending on failure. One chain runs at a time per state directory, and
each run's record is kept beside it.
"""

import argparse
import json
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Self, cast

from pydantic import Field, ValidationError, model_validator

from anamnema.briefing_refresh import held_briefings, record_replacements
from anamnema.graph_store import GraphStore, connect_graph
from anamnema.ingest_config import archive_settings
from anamnema.memory_config import load_memory
from anamnema.naming_graph import NamingGraph
from anamnema.private_files import PrivateFiles
from anamnema.setup_config import Nonempty, SetupError, StrictModel, load_config
from anamnema.snapshot_worker import run_command as bounded_command


class NightlyBriefings(StrictModel):
    """Where held briefings live and the judge that rebuilds them; absent means no stage."""

    briefing_root: Path
    stack_root: Path
    judge_config: Path
    max_rebuilds: int = Field(gt=0)
    max_replacements_bytes: int = Field(default=1048576, gt=0)


class NightlyConfig(StrictModel):
    """Written by the installer: the command, the shared configuration and the model files."""

    instance: Nonempty
    executable: Path
    state_directory: Path
    common: tuple[Nonempty, ...] = Field(
        description="configuration options every memory command takes, e.g. --config FILE"
    )
    extraction_config: Path | None = Field(
        default=None, description="execution profile: notes endpoint, bearer and model identity"
    )
    search_config: Path | None = None
    model_file: Path
    card_model_file: Path
    lesson_model_file: Path
    lesson_kinds: tuple[Nonempty, ...] = ("failure", "principle")
    link_limit: int = Field(gt=0)
    naming_rounds: int = Field(gt=0)
    max_cards: int = Field(gt=0)
    lesson_batch: int = Field(gt=0)
    stage_deadline_seconds: float = Field(gt=0, allow_inf_nan=False)
    max_output_bytes: int = Field(gt=0)
    diagnostic_tail_bytes: int = Field(
        default=4096, gt=0, description="stderr kept privately from a failed stage, at most"
    )
    history: int = Field(gt=0)
    briefings: NightlyBriefings | None = None

    @model_validator(mode="after")
    def explicit_paths(self) -> Self:
        paths = (
            self.executable,
            self.state_directory,
            self.model_file,
            self.card_model_file,
            self.lesson_model_file,
            *(p for p in (self.extraction_config, self.search_config) if p is not None),
        )
        if any(not path.is_absolute() for path in paths):
            raise ValueError("nightly paths must be absolute")
        if self.diagnostic_tail_bytes > self.max_output_bytes:
            raise ValueError("diagnostic_tail_bytes must not exceed max_output_bytes")
        return self


def add_nightly(parser: argparse.ArgumentParser) -> None:
    """``anamnema nightly --nightly-config FILE``; the graph options locate the store to select."""
    parser.add_argument("--nightly-config", type=Path, required=True)
    for option in ("config", "archive-config", "memory-config"):
        parser.add_argument(f"--{option}", type=Path)


def run_nightly(args: argparse.Namespace) -> dict[str, Any]:
    config = nightly_config(args.nightly_config)
    setup, archive = load_config(args.config), archive_settings(args.archive_config)
    graph = connect_graph(setup, archive, load_memory(args.memory_config))
    try:
        return Nightly(config, graph).run()
    finally:
        graph.close()


def nightly_config(path: Path) -> NightlyConfig:
    try:
        return NightlyConfig.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValidationError) as exc:
        raise SetupError(f"invalid nightly configuration ({type(exc).__name__})") from exc


Runner = Callable[[list[str], float, int], dict[str, Any]]
READ_CHUNK_BYTES = 65536
# A runner hands a failed stage's raw stderr under this key; _stage turns it into the private
# diagnostic, so raw child output never reaches the record, the journal or stdout.
STDERR_KEY = "_stderr"
# The parsers that accept each option (memory_cli.py, entity_link_cli.py); brief takes no
# --extraction-config and link-entities no --search-config.
EXTRACTION_COMMANDS = frozenset({"link-entities", "build-cards", "name-notes", "build-lessons"})
SEARCH_COMMANDS = frozenset({"build-cards", "name-notes", "build-lessons", "brief"})


@dataclass(frozen=True)
class StageBounds:
    """One stage's whole deadline and output bound, enforced while draining (snapshot_worker)."""

    timeout_seconds: float
    max_output_bytes: int
    read_chunk_bytes: int


@dataclass(frozen=True)
class StageDirectory:
    cwd: Path


def run_command(command: list[str], deadline: float, limit: int) -> dict[str, Any]:
    """One stage through the shared bounded transport: both pipes drained under one byte bound
    while reading, one whole deadline, the process group always killed and reaped."""
    started = time.monotonic()
    bounds = StageBounds(deadline, limit, min(limit, READ_CHUNK_BYTES))
    stage = " ".join(command[1:2]) or "stage"
    try:
        done = bounded_command(command, StageDirectory(Path("/")), bounds, f"nightly {stage}")
    except SetupError as exc:
        return {"exit": None, "error": str(exc), "seconds": round(time.monotonic() - started, 3)}
    result: dict[str, Any] = {"exit": done.code, "seconds": round(time.monotonic() - started, 3)}
    try:
        result["summary"] = json.loads(done.stdout.decode("utf-8")) if done.stdout.strip() else None
    except UnicodeError, json.JSONDecodeError:
        result["error"] = "output is not JSON"
    if done.code != 0 and done.stderr.strip():
        result[STDERR_KEY] = done.stderr
    return result


def diagnostic(stderr: bytes, tail: int) -> dict[str, str]:
    """A failed stage's reason: the structured SetupError text when the child printed one,
    otherwise the last bytes of its stderr; never more than ``tail`` bytes either way."""
    try:
        parsed: object = json.loads(stderr.decode("utf-8"))
    except UnicodeError, json.JSONDecodeError:
        parsed = None
    if isinstance(parsed, dict):
        error = cast(dict[str, Any], parsed).get("error")
        if isinstance(error, str):
            return {"error": error.encode("utf-8")[:tail].decode("utf-8", errors="ignore")}
    return {"stderr_tail": stderr[-tail:].decode("utf-8", errors="replace")}


def due_entities(graph: GraphStore, instance: str, limit: int) -> list[str]:
    """Live entities whose card is stale and that have at least one linked note."""
    s = graph.settings
    rows = graph.read(
        f"MATCH (e:{s.entity_label} {{instance:$instance}}) "
        "WHERE e.status<>'dropped' AND NOT coalesce(e.card_valid,false) "
        f"AND EXISTS {{ MATCH (:{s.note_label})-[:ABOUT_ENTITY]->(e) }} "
        "RETURN e.name AS name ORDER BY e.updated_at LIMIT $limit",
        instance=instance,
        limit=limit,
    )
    return [row["name"] for row in rows]


def lesson_notes(graph: GraphStore, instance: str, kinds: tuple[str, ...], limit: int) -> list[str]:
    """Active advice notes no lesson has judged yet, oldest first."""
    rows = graph.read(
        f"MATCH (n:{graph.settings.note_label} {{instance:$instance}}) "
        "WHERE n.insertion_complete=true AND n.status='active' AND n.kind IN $kinds "
        "AND n.lesson_state IS NULL AND NOT coalesce(n.wrong_open,false) "
        "RETURN n.note_id AS id ORDER BY n.created_at, n.note_id LIMIT $limit",
        instance=instance,
        kinds=list(kinds),
        limit=limit,
    )
    return [row["id"] for row in rows]


class Nightly:
    """Sequential stages under one lock; the run record is the only state this module owns."""

    def __init__(self, config: NightlyConfig, graph: GraphStore, runner: Runner = run_command):
        self.config, self.graph, self.runner = config, graph, runner
        self.files = PrivateFiles(0o700, 0o600, ".nightly-")

    def _base(self, command: str) -> list[str]:
        """Each option goes only to the commands whose parser accepts it; an unknown one exits 2."""
        c = self.config
        argv = [str(c.executable), command, "--instance", c.instance, *c.common]
        if c.extraction_config is not None and command in EXTRACTION_COMMANDS:
            argv += ["--extraction-config", str(c.extraction_config)]
        if c.search_config is not None and command in SEARCH_COMMANDS:
            argv += ["--search-config", str(c.search_config)]
        return argv

    def _stage(self, name: str, command: list[str]) -> dict[str, Any]:
        c = self.config
        result = self.runner(command, c.stage_deadline_seconds, c.max_output_bytes)
        stderr = result.pop(STDERR_KEY, None)
        if isinstance(stderr, bytes):
            result["diagnostic"] = diagnostic(stderr, c.diagnostic_tail_bytes)
            result["diagnostic_kept"] = True
        return {"stage": name, **result}

    def run(self) -> dict[str, Any]:
        c = self.config
        self.files.directory(c.state_directory)
        with self.files.locked(c.state_directory / ".nightly.lock"):
            started = datetime.now(UTC).isoformat()
            stages = [
                self._links(),
                *self._cards(),
                *self._naming(),
                *self._lessons(),
                *self._briefings(),
            ]
            failed = [s["stage"] for s in stages if s.get("exit") != 0]
            record = {
                "started": started,
                "finished": datetime.now(UTC).isoformat(),
                "stages": stages,
                "failed": failed,
            }
            self._keep(record)
            return {
                **record,
                "stages": [
                    {k: v for k, v in stage.items() if k != "diagnostic"} for stage in stages
                ],
            }

    def _links(self) -> dict[str, Any]:
        c = self.config
        command = [
            *self._base("link-entities"),
            "--model-file",
            str(c.model_file),
            "--limit",
            str(c.link_limit),
            "--apply",
        ]
        return self._stage("links", command)

    def _cards(self) -> list[dict[str, Any]]:
        c = self.config
        return [
            self._stage(
                f"card:{name}",
                [
                    *self._base("build-cards"),
                    "--entity",
                    name,
                    "--card-model-file",
                    str(c.card_model_file),
                ],
            )
            for name in due_entities(self.graph, c.instance, c.max_cards)
        ]

    def _naming(self) -> list[dict[str, Any]]:
        c = self.config
        naming = NamingGraph(self.graph)
        results: list[dict[str, Any]] = []
        for _ in range(c.naming_rounds):
            if not naming.pending(c.instance, 1):
                break
            command = [
                *self._base("name-notes"),
                "--pending",
                "--model-file",
                str(c.model_file),
                "--apply",
            ]
            results.append(self._stage("naming", command))
            if results[-1].get("exit") != 0:
                break
        return results

    def _lessons(self) -> list[dict[str, Any]]:
        c = self.config
        notes = lesson_notes(self.graph, c.instance, c.lesson_kinds, c.lesson_batch)
        if not notes:
            return []
        command = [*self._base("build-lessons"), "--lesson-model-file", str(c.lesson_model_file)]
        for note in notes:
            command += ["--note", note]
        return [self._stage("lessons", command)]

    def _briefings(self) -> list[dict[str, Any]]:
        """Rebuild held bundles; a changed ready rebuild becomes that bundle's replacement."""
        c, b = self.config, self.config.briefings
        if b is None:
            return []
        results: list[dict[str, Any]] = []
        replaced: dict[str, str] = {}
        for identity in held_briefings(b.stack_root, c.instance)[: b.max_rebuilds]:
            command = [
                *self._base("brief"),
                "--rebuild",
                identity,
                "--judge-config",
                str(b.judge_config),
                "--briefing-root",
                str(b.briefing_root),
                "--apply",
            ]
            result = self._stage(f"briefing:{identity}", command)
            summary = result.get("summary")
            if (
                result.get("exit") == 0
                and isinstance(summary, dict)
                and cast(dict[str, Any], summary).get("status") == "ready"
                and cast(dict[str, Any], summary).get("unchanged") is False
            ):
                replaced[identity] = str(cast(dict[str, Any], summary)["briefing"])
            results.append(result)
        record_replacements(c.state_directory, c.instance, replaced, b.max_replacements_bytes)
        return results

    def _keep(self, record: dict[str, Any]) -> None:
        runs = self.config.state_directory / "runs"
        self.files.directory(runs)
        stamp = record["started"].replace(":", "").replace("+", "Z")
        self.files.save(runs / f"{stamp}.json", json.dumps(record, indent=2).encode("utf-8"))
        kept = sorted(runs.glob("*.json"))
        for old in kept[: max(len(kept) - self.config.history, 0)]:
            old.unlink()
