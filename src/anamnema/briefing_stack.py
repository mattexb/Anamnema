"""Session-held briefing budget and required-read delivery, with crash-safe reservations."""

import hashlib
import math
import re
from collections.abc import Generator
from contextlib import contextmanager
from importlib.resources import files
from pathlib import Path
from typing import Any, Self

from pydantic import Field, ValidationError, model_validator

from anamnema.archive_models import canonical, digest
from anamnema.briefing_files import BriefFiles
from anamnema.briefing_policy import BriefingPolicy
from anamnema.continuity import Continuity
from anamnema.setup_config import Nonempty, SetupError, StrictModel


class StackPolicy(StrictModel):
    state_file: Nonempty
    lock_file: Nonempty
    max_entries: int = Field(gt=0)
    max_state_bytes: int = Field(gt=0)

    @model_validator(mode="after")
    def local_names(self) -> Self:
        if self.state_file == self.lock_file or any(
            Path(name).name != name or name in {".", ".."} or any(c in name for c in "\\\n\r\x00")
            for name in (self.state_file, self.lock_file)
        ):
            raise ValueError("briefing stack names must be distinct local basenames")
        return self


def stack_policy(path: Path | None = None) -> StackPolicy:
    try:
        raw = (
            path.read_text(encoding="utf-8")
            if path
            else files("anamnema")
            .joinpath("config/briefing_stack.json")
            .read_text(encoding="utf-8")
        )
        return StackPolicy.model_validate_json(raw)
    except (OSError, UnicodeError, ValidationError) as exc:
        raise SetupError("briefing stack policy cannot be read or validated") from exc


class HeldBrief(StrictModel):
    briefing: Nonempty
    page: int = Field(gt=0)
    content_bytes: int = Field(gt=0)
    dropped: bool = False


class StackState(StrictModel):
    instance: Nonempty
    session: Nonempty
    held: dict[str, HeldBrief] = Field(default_factory=dict[str, HeldBrief])


class BriefStack:
    """Dropping affects future holding, not archived bundles or already owed read proofs."""

    def __init__(
        self,
        root: Path,
        bundles: BriefFiles,
        continuity: Continuity,
        briefing: BriefingPolicy,
        policy: StackPolicy,
    ) -> None:
        if not root.is_absolute() or root == root.parent or root == bundles.root:
            raise SetupError("briefing stack root must be explicit and separate from bundles")
        self.root, self.bundles, self.continuity, self.briefing, self.policy = (
            root,
            bundles,
            continuity,
            briefing,
            policy,
        )
        self.files = bundles.files

    def _folder(self, session: str) -> Path:
        if re.fullmatch(self.continuity.policy.session_pattern, session) is None:
            raise SetupError("invalid briefing session identity")
        return self.root / digest([self.continuity.config.instance, session])

    @contextmanager
    def _locked(self, session: str) -> Generator[tuple[StackState, Path]]:
        folder = self._folder(session)
        self.files.directory(self.root)
        self.files.directory(folder)
        # Lock order is stack -> continuity; never acquire a stack lock from a read-state lock.
        with self.files.locked(folder / self.policy.lock_file):
            path = folder / self.policy.state_file
            state = self._read(path, session)
            yield state, path

    def _read(self, path: Path, session: str) -> StackState:
        if not path.exists() and not path.is_symlink():
            return StackState(instance=self.continuity.config.instance, session=session)
        try:
            state = StackState.model_validate_json(
                self.files.read(path, self.policy.max_state_bytes)
            )
        except (OSError, ValidationError) as exc:
            raise SetupError("briefing stack is corrupt; no automatic reset") from exc
        if (
            state.instance != self.continuity.config.instance
            or state.session != session
            or len(state.held) > self.policy.max_entries
            or any(key != digest([item.briefing, item.page]) for key, item in state.held.items())
        ):
            raise SetupError("briefing stack identity or bounds differ")
        return state

    def _save(self, path: Path, state: StackState) -> None:
        data = canonical(state.model_dump(mode="json"))
        if len(data) > self.policy.max_state_bytes or len(state.held) > self.policy.max_entries:
            raise SetupError("briefing stack exceeds configured state bounds")
        self.files.save(path, data)

    def _tokens(self, state: StackState) -> int:
        return sum(
            math.ceil(
                item.content_bytes / self.briefing.bytes_per_token * (1 + self.briefing.size_margin)
            )
            for item in state.held.values()
            if not item.dropped
        )

    def status(self, session: str) -> dict[str, Any]:
        """Inspection never creates directories, locks, read debt or state."""
        folder = self._folder(session)
        if folder.exists():
            self.files.existing_directory(self.root)
            self.files.existing_directory(folder)
        state = self._read(folder / self.policy.state_file, session)
        return {
            "session": session,
            "held": state.model_dump(mode="json")["held"],
            "tokens_estimated": self._tokens(state),
        }

    def deliver(self, session: str, identity: str, page: int = 1) -> dict[str, Any]:
        """Reserve budget before registering debt; replay charges no second reservation."""
        manifest, pages = self.bundles.read(self.continuity.config.instance, identity)
        if (
            manifest.metadata.get("status") != "ready"
            or type(page) is not int
            or not 1 <= page <= len(pages)
        ):
            raise SetupError("only a ready briefing and an existing page can be delivered")
        content = self.files.read(pages[page - 1], self.bundles.policy.max_file_bytes).decode(
            "utf-8"
        )
        if not content.strip():
            raise SetupError("briefing page is empty; no read registered")
        expected = manifest.checksums[pages[page - 1].name]
        if hashlib.sha256(content.encode("utf-8")).hexdigest() != expected:
            raise SetupError("briefing page changed after bundle verification")
        item = HeldBrief(briefing=identity, page=page, content_bytes=len(content.encode("utf-8")))
        key = digest([identity, page])
        with self._locked(session) as (state, path):
            previous = state.held.get(key)
            reserved = previous is not None and not previous.dropped
            tokens = self._tokens(state)
            tokens += 0 if reserved else self.briefing.estimate(content)
            if not reserved and tokens > int(
                self.briefing.context_tokens * self.briefing.stack_hard_share
            ):
                raise SetupError("briefing held: session stack ceiling exceeded; drop a held page")
            if not reserved:
                state.held[key] = item
                self._save(path, state)
            # Local bounded file operations only; serialize drop against read registration.
            instruction = self.continuity.require(
                session, "briefings", key, pages[page - 1], expected_sha256=expected
            )
        return {
            "briefing": identity,
            "page": page,
            "required_read": instruction,
            "tokens_estimated": tokens,
            "above_soft_limit": tokens
            > int(self.briefing.context_tokens * self.briefing.stack_soft_share),
            "above_hard_limit": tokens
            > int(self.briefing.context_tokens * self.briefing.stack_hard_share),
        }

    def drop(self, session: str, identity: str, page: int = 1) -> dict[str, Any]:
        """Keep the exact record and files; no drop can acknowledge a managed required read."""
        if type(page) is not int or page < 1:
            raise SetupError("briefing page must be a positive integer")
        with self._locked(session) as (state, path):
            key = digest([identity, page])
            item = state.held.get(key)
            if item is None:
                raise SetupError("briefing page is not held in this session")
            state.held[key] = item.model_copy(update={"dropped": True})
            self._save(path, state)
        return {"briefing": identity, "page": page, "dropped": True}
