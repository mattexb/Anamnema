"""Reusable required-read and staged/applied/acknowledged snapshot transitions."""

from __future__ import annotations

import hashlib
import json
import logging
import re
import secrets
from pathlib import Path

from anamnema.continuity_config import ContinuityConfig, ContinuityPolicy
from anamnema.continuity_state import (
    ContinuityState,
    RequiredRead,
    Snapshot,
    StateFiles,
    source_text,
)
from anamnema.setup_config import SetupError

LOGGER = logging.getLogger(__name__)
SOURCE_STAMP_PREFIX = "read_token: "


def unstamped_read(content: str) -> str:
    """Remove only trailing source token lines; never edit the producer's original file."""
    lines = content.splitlines(keepends=True)
    end = len(lines)
    while end and not lines[end - 1].strip():
        end -= 1
    start = end
    while start:
        candidate = start
        while candidate and not lines[candidate - 1].strip():
            candidate -= 1
        if not candidate or not lines[candidate - 1].startswith(SOURCE_STAMP_PREFIX):
            break
        start = candidate - 1
    return "".join(lines[:start]) if start != end else content


def validate_snapshot(
    content: str, config: ContinuityConfig, policy: ContinuityPolicy, session: str
) -> str:
    """Share exact snapshot ownership/header validation between staging and archive sync."""
    content = content.rstrip()
    lines = content.splitlines()
    headers = [policy.snapshot_marker, f"Instance-ID: {config.instance}", f"Session-ID: {session}"]
    if lines[: len(headers)] != headers or not "\n".join(lines[len(headers) :]).strip():
        raise SetupError("snapshot needs its exact marker, instance/session headers and body")
    return content


class Continuity:
    """The shared core has no Claude process, retrieval, database or registration dependency."""

    def __init__(self, config: ContinuityConfig, policy: ContinuityPolicy) -> None:
        self.config, self.policy = config, policy

    def _identity(self, files: StateFiles) -> RequiredRead:
        rows: list[dict[str, str]] = []
        total = 0
        for path in self.config.identity_files:
            data = source_text(path, self.policy.max_content_bytes)
            total += len(data.encode("utf-8"))
            if total > self.policy.max_content_bytes or not data.strip():
                raise SetupError("identity sources are empty or exceed configured bound")
            rows.append({"source": path.name, "text": data})
        token = secrets.token_hex(self.policy.token_bytes)
        content = "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows)
        content += json.dumps({"read_token": token}) + "\n"
        return RequiredRead(producer="identity", blob=files.blob(content), token=token)

    def read_instruction(self, files: StateFiles, item: RequiredRead) -> str:
        """Never reveal the nonce outside the managed document's last line."""
        return (
            f"Read {files.folder / item.blob} completely, including the last line. "
            f"Then add an exact acknowledgement line: {self.policy.read_marker} "
            "followed by a space and the read_token found at the end of that file."
        )

    def snapshot_ack(self, session: str, snapshot: Snapshot) -> str:
        """The complete line binds acknowledgement to the exact applied content."""
        return f"{self.policy.snapshot_ack_marker} Session-ID: {session} SHA256: {snapshot.digest}"

    def require(
        self,
        session: str,
        producer: str,
        key: str,
        path: Path,
        *,
        expected_sha256: str | None = None,
    ) -> str:
        """Register a real derived-document read; replay preserves outstanding proof tokens."""
        if producer == "identity" or re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]*", producer) is None:
            raise SetupError("required-read producer is invalid or reserved")
        if (
            not key.strip()
            or len(key) > self.policy.max_read_key_characters
            or "\n" in key
            or "\r" in key
        ):
            raise SetupError("required-read key must be a bounded nonempty single line")
        files = StateFiles(self.config, self.policy, session)
        original = source_text(path, self.policy.max_content_bytes)
        if (
            expected_sha256 is not None
            and hashlib.sha256(original.encode("utf-8")).hexdigest() != expected_sha256
        ):
            raise SetupError("required-read source checksum differs; no read registered")
        content = unstamped_read(original)
        if not content.strip():
            raise SetupError("required-read source is empty; no read registered")
        fingerprint = hashlib.sha256(content.encode("utf-8")).hexdigest()
        with files.locked() as state:
            previous = next(
                (r for r in state.reads if (r.producer, r.key) == (producer, key)), None
            )
            if previous and previous.content_digest == fingerprint:
                return self.read_instruction(files, previous)
            remaining = tuple(r for r in state.reads if (r.producer, r.key) != (producer, key))
            limit = self.policy.max_pending - int(not state.initialized)
            if len(remaining) >= limit:
                raise SetupError("required reads exceed configured bound; no new read registered")
            token = secrets.token_hex(self.policy.token_bytes)
            body = content + "\n\n" + json.dumps({"read_token": token}) + "\n"
            item = RequiredRead(
                producer=producer,
                key=key,
                content_digest=fingerprint,
                blob=files.blob(body),
                token=token,
            )
            updated = state.model_copy(update={"reads": (*remaining, item)})
            files.commit(updated)
            files.cleanup(updated)
            return self.read_instruction(files, item)

    def _snapshot_instruction(self, files: StateFiles, snapshot: Snapshot) -> str:
        content = files.content(snapshot.blob).rstrip()
        if hashlib.sha256(content.encode("utf-8")).hexdigest() != snapshot.digest:
            raise SetupError("snapshot journal digest does not match managed content")
        return (
            f"A compaction snapshot is pending at {files.folder / snapshot.blob}. "
            "Read it completely from first line to EOF and incorporate it with the native "
            "compaction summary. Then emit this exact acknowledgement line:\n"
            + self.snapshot_ack(files.session, snapshot)
        )

    def _start(
        self, files: StateFiles, state: ContinuityState, source: str
    ) -> tuple[ContinuityState, list[str]]:
        allowed = (
            *self.policy.identity_sources,
            self.policy.resume_source,
            self.policy.compact_source,
        )
        if source not in allowed:
            raise SetupError("unsupported continuity SessionStart source")
        notices: list[str] = []
        if not state.initialized:
            try:
                identity = self._identity(files)
            except (SetupError, OSError, UnicodeError) as exc:
                LOGGER.warning(
                    "continuity identity read unavailable (%s); no new read gate created",
                    type(exc).__name__,
                )
                notices.append(
                    f"Identity read unavailable ({type(exc).__name__}); repair identity sources "
                    "and retry SessionStart. No new identity read gate was created."
                )
            else:
                state = state.model_copy(
                    update={"initialized": True, "reads": (*state.reads, identity)}
                )
        if source == self.policy.compact_source:
            if state.snapshot is None:
                LOGGER.warning(
                    "continuity compaction snapshot unavailable; no snapshot read gate created"
                )
                notices.append(
                    "Compaction snapshot unavailable: nothing staged. "
                    "No snapshot read gate was created."
                )
            else:
                state = state.model_copy(
                    update={"snapshot": state.snapshot.model_copy(update={"applied": True})}
                )
        if len(state.reads) > self.policy.max_pending:
            raise SetupError("required reads exceed configured bound")
        return state.model_copy(update={"failure": None}), notices

    def start(self, session: str, source: str) -> str:
        """Commit obligations before returning context; preserve outstanding tokens on retries."""
        files = StateFiles(self.config, self.policy, session)
        with files.locked() as state:
            updated, notices = self._start(files, state, source)
            parts = [*notices, *(self.read_instruction(files, item) for item in updated.reads)]
            if updated.snapshot and updated.snapshot.applied:
                parts.append(self._snapshot_instruction(files, updated.snapshot))
            files.commit(updated)
            files.cleanup(updated)
        return "\n\n".join(parts)

    def stop(self, session: str, message: str) -> str:
        """Return an actionable reason while debt remains; settle all proofs atomically."""
        files = StateFiles(self.config, self.policy, session)
        with files.locked() as state:
            lines = set(message.splitlines())
            reads = tuple(
                item
                for item in state.reads
                if f"{self.policy.read_marker} {item.token}" not in lines
            )
            snapshot, last = state.snapshot, state.last_snapshot
            if snapshot and snapshot.applied:
                self._snapshot_instruction(files, snapshot)
                if self.snapshot_ack(session, snapshot) in lines:
                    files.spool_snapshot(snapshot)
                    last = snapshot
                    snapshot = None
            updated = state.model_copy(
                update={
                    "reads": reads,
                    "snapshot": snapshot,
                    "last_snapshot": last,
                    "failure": None,
                }
            )
            files.commit(updated)
            files.cleanup(updated)
            parts = [self.read_instruction(files, item) for item in reads]
            if snapshot and snapshot.applied:
                parts.append(self._snapshot_instruction(files, snapshot))
            return "\n\n".join(parts)

    def stage(self, session: str, content: str) -> Snapshot:
        """Preserve an explicit complete snapshot without applying it or launching a model."""
        content = validate_snapshot(content, self.config, self.policy, session)
        digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
        files = StateFiles(self.config, self.policy, session)
        with files.locked() as state:
            if state.snapshot:
                if state.snapshot.digest == digest:
                    return state.snapshot
                if state.snapshot.applied:
                    raise SetupError(
                        "an applied unread snapshot already exists; refusing replacement"
                    )
            if (
                state.last_snapshot
                and state.last_snapshot.digest == digest
                and state.snapshot is None
            ):
                return state.last_snapshot
            files.reserve_spool()
            snapshot = Snapshot(blob=files.blob(content + "\n"), digest=digest)
            updated = state.model_copy(update={"snapshot": snapshot})
            files.commit(updated)
            files.cleanup(updated)
            return snapshot
