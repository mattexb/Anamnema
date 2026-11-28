"""Which routes of the lessons near an action hold for it, judged by a model and remembered.

Similarity alone cannot tell a fitting lesson from an off-topic one: in the KinTools replay
(2026-11-28, 114 recorded actions, 289 action and lesson pairs labelled blind) fitting and
off-topic lessons both sat between 0.76 and 0.89. The narrow part of a lesson is its route's
written condition, so the condition is read at delivery: a small model is told the action and
each candidate route's condition and says which hold. Only those routes come, with no count
limit; a lesson with none stays out. Measured there: off-topic lessons 72 to 4, fitting ones
22 to 37, the hook's worst time 4.3 s.

- **The worker**: one model process answers ``turns`` judgements in a row and the next is
  started while the old one does its last, so a start never falls in front of an action. It
  sends nothing while idle. A late answer is finished in the background into the cache, never
  killed: killing it made every next worker start cold.
- **The cache**: an answer is kept per action, lesson and condition text, and per policy
  ``version``; a changed condition or policy is judged again. Bounded by ``cache_entries``
  (least recently used out first) and ``cache_days`` unused.
- **No answer in time** (``answer_seconds``): the routes asked about stay out. An uncertain
  lesson does not come.

The worker runs tool-free with no settings, hooks, MCP servers or session persistence, like
the native judge; its arguments are validated against that floor.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import selectors
import subprocess
import threading
import time
from collections.abc import Callable
from importlib.resources import files
from pathlib import Path
from typing import Annotated, Any, Self, cast

from pydantic import Field, ValidationError, model_validator

from anamnema.judge_config import JudgeConfig
from anamnema.setup_config import Nonempty, SetupError, StrictModel

LOG = logging.getLogger(__name__)
Holding = dict[str, list[int]]
Ask = Callable[[str, float, Callable[[list[str]], object] | None], list[str] | None]
# The isolation floor: each flag with the one value it must carry (None: a bare switch).
FLOOR: dict[str, str | None] = {
    "--print": None,
    "--no-session-persistence": None,
    "--strict-mcp-config": None,
    "--no-chrome": None,
    "--verbose": None,
    "--permission-mode": "dontAsk",
    "--tools": "",
    "--setting-sources": "",
    "--settings": '{"disableAllHooks":true}',
    "--mcp-config": '{"mcpServers":{}}',
    "--input-format": "stream-json",
    "--output-format": "stream-json",
}
FREE = {"--effort"}  # flags whose value the policy may choose
SCHEMA = (
    '{"type":"object","properties":{"holds":{"type":"array","items":{"type":"string"}}},'
    '"required":["holds"]}'
)


class ConditionPolicy(StrictModel):
    """The instruction, the worker's life, the answer deadline and the cache's bounds."""

    prompt: Nonempty
    system_prompt: Nonempty
    worker_arguments: tuple[str, ...]
    turns: Annotated[int, Field(gt=1)]
    answer_seconds: Annotated[float, Field(gt=0, allow_inf_nan=False)]
    late_seconds: Annotated[float, Field(gt=0, allow_inf_nan=False)]
    reap_seconds: Annotated[float, Field(gt=0, allow_inf_nan=False)]
    cache_entries: Annotated[int, Field(gt=0)]
    cache_days: Annotated[float, Field(gt=0, allow_inf_nan=False)]
    max_prompt_bytes: Annotated[int, Field(gt=0)]
    version: Nonempty

    @model_validator(mode="after")
    def isolated(self) -> Self:
        """Every floor flag once with its value; nothing else but the free flags."""
        seen: dict[str, str | None] = {}
        arguments = iter(self.worker_arguments)
        for flag in arguments:
            if flag in seen or (flag not in FLOOR and flag not in FREE):
                raise ValueError("condition judge arguments repeat or add an unsafe option")
            seen[flag] = None if FLOOR.get(flag, "") is None else next(arguments, None)
        for flag, value in FLOOR.items():
            if flag not in seen or not _same(seen[flag], value):
                raise ValueError("condition judge arguments lack the tool-free isolation floor")
        if "{action}" not in self.prompt or "{conditions}" not in self.prompt:
            raise ValueError("condition judge prompt must name {action} and {conditions}")
        return self


def _same(given: str | None, required: str | None) -> bool:
    if given is None or required is None:
        return given == required
    try:
        return json.loads(given) == json.loads(required)
    except ValueError:
        return given == required


def condition_policy(path: Path | None = None) -> ConditionPolicy:
    """Read the packaged policy or an explicit replacement."""
    try:
        raw = (
            path.read_text(encoding="utf-8")
            if path
            else files("anamnema").joinpath("config/condition_judge.json").read_text("utf-8")
        )
        return ConditionPolicy.model_validate_json(raw)
    except (OSError, UnicodeError, ValidationError) as exc:
        raise SetupError(f"invalid condition judge policy ({type(exc).__name__})") from exc


class ConditionCache:
    """Answers kept in one JSON file: key to [holds, last used], bounded in entries and age."""

    def __init__(self, path: Path, entries: int, days: float) -> None:
        self._path, self._entries, self._age = path, entries, days * 86400
        self._rows: dict[str, list[Any]] | None = None
        self._lock = threading.Lock()

    def get(self, key: str) -> bool | None:
        """The kept answer for ``key``, or None."""
        with self._lock:
            row = self._load().get(key)
            if row is None:
                return None
            row[1] = time.time()
            return bool(row[0])

    def put(self, answers: dict[str, bool]) -> None:
        """Keep these answers, drop what the bounds push out, then save atomically."""
        with self._lock:
            rows, now = self._load(), time.time()
            rows.update({key: [holds, now] for key, holds in answers.items()})
            fresh = [(k, v) for k, v in rows.items() if now - float(v[1]) <= self._age]
            fresh.sort(key=lambda item: -float(item[1][1]))
            self._rows = dict(fresh[: self._entries])
            spare = self._path.with_suffix(".tmp")
            spare.write_text(json.dumps(self._rows), encoding="utf-8")
            spare.replace(self._path)

    def _load(self) -> dict[str, list[Any]]:
        if self._rows is None:
            try:
                loaded = json.loads(self._path.read_text(encoding="utf-8"))
            except OSError, ValueError:
                loaded = {}
            self._rows = loaded if isinstance(loaded, dict) else {}
        return self._rows


class Worker:
    """One model process that answers ``turns`` judgements, then hands over to a fresh one."""

    def __init__(self, command: tuple[str, ...], cwd: Path, policy: ConditionPolicy) -> None:
        self._command, self._cwd, self._policy = list(command), cwd, policy
        self._process: subprocess.Popen[bytes] | None = None
        self._spare: subprocess.Popen[bytes] | None = None
        self._used = 0
        self._lock = threading.Lock()

    def ask(
        self, prompt: str, seconds: float, late: Callable[[list[str]], object] | None = None
    ) -> list[str] | None:
        """The ids the model says hold, or None when it gives no answer within ``seconds``.

        A late answer is finished in the background and handed to ``late``; while it
        finishes, a new question gets None at once and that action goes without.
        """
        if not self._lock.acquire(blocking=False):
            return None
        handed, started = False, time.monotonic()
        try:
            process = self._current()
            reader = _Reader(process)
            message = {"type": "user", "message": {"role": "user", "content": prompt}}
            try:
                if process.stdin is None:
                    raise OSError("worker has no input")
                process.stdin.write((json.dumps(message) + "\n").encode())
                process.stdin.flush()
                answer = reader.result(time.monotonic() + seconds)
            except OSError, ValueError:
                answer, reader.ended = None, True
            if answer is None and not reader.ended:
                LOG.info("event=condition_judge_late seconds=%.2f", time.monotonic() - started)
                handed = True
                threading.Thread(
                    target=self._finish_late, args=(process, reader, late), daemon=True
                ).start()
                return None
            if answer is None:
                LOG.warning("event=condition_judge_stream_broke")
                self._stop(process, now=True)
                self._process, self._used = None, 0
                return None
            self._turned(process)
            return answer
        finally:
            if not handed:
                self._lock.release()

    def _finish_late(
        self,
        process: subprocess.Popen[bytes],
        reader: _Reader,
        late: Callable[[list[str]], object] | None,
    ) -> None:
        try:
            answer = reader.result(time.monotonic() + self._policy.late_seconds)
            if answer is None:
                self._stop(process, now=True)
                self._process, self._used = None, 0
                return
            self._turned(process)
            if late is not None:
                late(answer)
        finally:
            self._lock.release()

    def _turned(self, process: subprocess.Popen[bytes]) -> None:
        """Count one judgement; start the spare before the last, hand over after it."""
        self._used += 1
        if self._used == self._policy.turns - 1 and self._spare is None:
            self._spare = self._start()
        if self._used >= self._policy.turns:
            self._stop(process)
            self._process, self._spare, self._used = self._spare, None, 0

    def _current(self) -> subprocess.Popen[bytes]:
        if self._process is None or self._process.poll() is not None:
            self._process, self._used = (self._spare or self._start()), 0
            self._spare = None
        return self._process

    def _start(self) -> subprocess.Popen[bytes]:
        try:
            return subprocess.Popen(  # noqa: S603 - explicit validated argv, no shell
                self._command,
                cwd=self._cwd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
            )
        except OSError as exc:
            raise SetupError(f"condition judge did not start ({type(exc).__name__})") from exc

    def _stop(self, process: subprocess.Popen[bytes], now: bool = False) -> None:
        """End a worker without making the action wait: killed when late, else told to end."""
        try:
            if now:
                process.kill()
            elif process.stdin is not None:
                process.stdin.close()
        except OSError:
            process.kill()
        threading.Thread(target=self._reap, args=(process,), daemon=True).start()

    def _reap(self, process: subprocess.Popen[bytes]) -> None:
        try:
            process.wait(timeout=self._policy.reap_seconds)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()


class _Reader:
    """The worker's output read up to its result line; a partial line is kept between reads."""

    def __init__(self, process: subprocess.Popen[bytes]) -> None:
        if process.stdout is None:
            raise SetupError("condition judge worker has no output")
        self._fd = process.stdout.fileno()
        self._buffered = b""
        self.ended = False

    def result(self, deadline: float) -> list[str] | None:
        """The result's ids, or None at the deadline (``ended`` false) or a broken stream."""
        with selectors.DefaultSelector() as selector:
            selector.register(self._fd, selectors.EVENT_READ)
            while (left := deadline - time.monotonic()) > 0:
                if not selector.select(left):
                    return None
                chunk = os.read(self._fd, 65536)
                if not chunk:
                    self.ended = True
                    return None
                self._buffered += chunk
                *lines, self._buffered = self._buffered.split(b"\n")
                for line in lines:
                    event = _mapping(json.loads(line) if line.strip() else {})
                    if event.get("type") == "result":
                        return self._holds(event)
        return None

    def _holds(self, event: dict[str, Any]) -> list[str] | None:
        holds: Any = _mapping(event.get("structured_output")).get("holds")
        if isinstance(holds, list):
            return [str(item) for item in cast(list[Any], holds)]
        self.ended = True
        return None


def _mapping(value: object) -> dict[str, Any]:
    """A JSON object as a mapping; anything else as an empty one."""
    return cast(dict[str, Any], value) if isinstance(value, dict) else {}


class ConditionCheck:
    """The routes of these lessons that hold for one action: cache first, then the worker."""

    def __init__(self, policy: ConditionPolicy, cache: ConditionCache, ask: Ask) -> None:
        self._policy, self._cache, self._ask = policy, cache, ask

    def holding(self, action: str, cwd: str | None, lessons: list[dict[str, Any]]) -> Holding:
        """Lesson id to the indexes of its routes that hold; a lesson with none is absent.

        Args:
            action: The action text the hook sent.
            cwd: The folder it runs in, when known.
            lessons: Lessons with ``id`` and ``routes`` (each with ``condition``).

        Returns:
            The holding routes per lesson; routes the model gave no answer for are left out.
        """
        known: Holding = {}
        asked: dict[str, tuple[str, int, str]] = {}
        for lesson in lessons:
            for index, route in enumerate(lesson["routes"]):
                key = self._key(action, cwd, lesson["id"], route["condition"])
                kept = self._cache.get(key)
                if kept is None:
                    asked[f"r{len(asked) + 1}"] = (lesson["id"], index, key)
                elif kept:
                    known.setdefault(lesson["id"], []).append(index)
        if asked:
            conditions = "\n".join(
                f"{label}: {lessons_route(lessons, item)}" for label, item in asked.items()
            )
            text = f"folder: {cwd or ''}\n{action}"
            prompt = self._policy.prompt.format(action=text, conditions=conditions)
            if len(prompt.encode("utf-8")) > self._policy.max_prompt_bytes:
                return _sorted(known)

            def keep(answer: list[str]) -> Holding:
                said, holding = set(answer), cast(Holding, {})
                self._cache.put({key: label in said for label, (_, _, key) in asked.items()})
                for label, (lesson_id, index, _) in asked.items():
                    if label in said:
                        holding.setdefault(lesson_id, []).append(index)
                return holding

            answer = self._ask(prompt, self._policy.answer_seconds, keep)
            for lesson_id, indexes in (keep(answer) if answer is not None else {}).items():
                known.setdefault(lesson_id, []).extend(indexes)
        return _sorted(known)

    def _key(self, action: str, cwd: str | None, lesson: str, condition: str) -> str:
        raw = json.dumps([self._policy.version, cwd or "", action, lesson, condition])
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def lessons_route(lessons: list[dict[str, Any]], item: tuple[str, int, str]) -> str:
    """The condition text of one asked route."""
    lesson_id, index, _ = item
    lesson = next(lesson for lesson in lessons if lesson["id"] == lesson_id)
    return str(lesson["routes"][index]["condition"])


def _sorted(known: Holding) -> Holding:
    return {lesson: sorted(indexes) for lesson, indexes in known.items()}


def condition_check(
    deployment: JudgeConfig, policy: ConditionPolicy | None = None
) -> ConditionCheck:
    """One check for the life of the memory service, its worker started at the first need."""
    chosen = policy or condition_policy()
    command = (
        str(deployment.executable),
        "--model",
        deployment.model,
        *chosen.worker_arguments,
        "--system-prompt",
        chosen.system_prompt,
        "--json-schema",
        SCHEMA,
    )
    cache = ConditionCache(
        deployment.cwd / "condition-cache.json", chosen.cache_entries, chosen.cache_days
    )
    return ConditionCheck(chosen, cache, Worker(command, deployment.cwd, chosen).ask)
