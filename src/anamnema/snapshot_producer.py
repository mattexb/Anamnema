"""Explicit transcript-to-worker-to-staged-snapshot path, without live hook installation."""

import os
import re
from importlib.resources import files
from pathlib import Path
from typing import Any

from anamnema.continuity import Continuity
from anamnema.continuity_state import StateFiles
from anamnema.setup_config import SetupError
from anamnema.snapshot_excerpt import chunk_entries
from anamnema.snapshot_policy import SnapshotProducerPolicy, SnapshotWorkerConfig
from anamnema.snapshot_worker import run_worker
from anamnema.speech_policy import SpeechPolicy
from anamnema.substrates.claude_snapshot import snapshot_dialogue


def _inputs(
    folder: Path, chunks: list[str], fileset: StateFiles, policy: SnapshotProducerPolicy
) -> list[Path]:
    paths: list[Path] = []
    for number, chunk in enumerate(chunks, start=1):
        path = folder / f"dialogue-{number}.txt"
        header = f"# Dialogue newest first: file {number} of {len(chunks)}.\n\n"
        if len(header.encode("utf-8")) > policy.max_header_bytes:
            raise SetupError("snapshot excerpt header exceeds configured bound")
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, fileset.policy.file_mode)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            os.fchmod(stream.fileno(), fileset.policy.file_mode)
            stream.write(header + chunk)
            stream.flush()
            os.fsync(stream.fileno())
        paths.append(path)
    return paths


def _body(body: str, policy: SnapshotProducerPolicy) -> None:
    if not body or body.startswith("```") or body.endswith("```"):
        raise SetupError("snapshot worker returned empty or fenced content")
    if any(marker in body for marker in policy.image_markers):
        raise SetupError("snapshot worker returned image content")
    if any(heading not in body for heading in policy.required_headings):
        raise SetupError("snapshot worker omitted required headings")


class SnapshotProducer:
    """Generate outside the session lock; only validated output enters shared staging."""

    def __init__(
        self,
        core: Continuity,
        policy: SnapshotProducerPolicy,
        worker: SnapshotWorkerConfig,
        speech: SpeechPolicy,
    ) -> None:
        self.core, self.policy, self.worker, self.speech = core, policy, worker, speech

    def produce(self, session: str, transcript: Path, trigger: str) -> dict[str, Any]:
        """Keep prior state on transport/validation failure, and clean all private inputs."""
        fileset = StateFiles(self.core.config, self.core.policy, session)
        if trigger not in self.policy.allowed_triggers:
            raise SetupError("unsupported snapshot trigger")
        before = fileset.peek()
        if before.snapshot and before.snapshot.applied:
            raise SetupError("an applied unread snapshot already exists; no worker started")
        dialogue = snapshot_dialogue(
            transcript, self.core.config.instance, session, self.policy, self.speech
        )
        if not dialogue.entries:
            raise SetupError("snapshot transcript has no eligible dialogue; no worker started")
        chunks = chunk_entries(
            dialogue.entries,
            self.policy.max_dialogue_bytes,
            self.policy.max_dialogue_files,
            self.policy.entry_cut_mark,
        )
        model = dialogue.model or self.worker.model
        if model and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:/-]*", model) is None:
            raise SetupError("snapshot worker model identifier is invalid")
        with fileset.registered_inputs(self.worker.input_root, self.worker.registry) as (
            folder,
            record,
        ):
            paths = _inputs(folder, chunks, fileset, self.policy)
            template = (
                files("anamnema").joinpath("config/snapshot_prompt.txt").read_text(encoding="utf-8")
            )
            prompt = (
                template
                + "\n## Invocation\n\n"
                + "\n".join(
                    f"Dialogue file {number}: {path}" for number, path in enumerate(paths, start=1)
                )
            )
            command = [
                str(self.worker.executable),
                *([self.policy.model_option, model] if model else []),
                self.policy.directory_option,
                str(folder),
                *self.policy.worker_arguments,
                self.policy.prompt_separator,
                prompt,
            ]
            body = run_worker(
                command, self.worker, self.policy, registry=self.worker.registry, record=record
            )
            _body(body, self.policy)
        envelope = (
            f"{self.core.policy.snapshot_marker}\nInstance-ID: {self.core.config.instance}\n"
            f"Session-ID: {session}\nTrigger: {trigger}\nProject: {self.worker.cwd}\n\n{body}\n"
        )
        snapshot = self.core.stage(session, envelope)
        return {
            "session": session,
            "snapshot": snapshot.model_dump(),
            "dialogue_files": len(chunks),
            "dialogue_entries": len(dialogue.entries),
            "model": model,
            "applied": False,
        }
