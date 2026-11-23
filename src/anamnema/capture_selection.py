"""Event-time source anchors prevent delayed capture from drifting to a newer turn."""

from dataclasses import replace
from pathlib import Path
from typing import Annotated, Literal, Self

from pydantic import Field, model_validator

from anamnema.archive_models import Record, Snapshot
from anamnema.marked_capture import CapturePolicy, is_capture_boundary
from anamnema.setup_config import Nonempty, SetupError, StrictModel
from anamnema.speech_policy import SpeechPolicy


class RecordAnchor(StrictModel):
    """Physical position distinguishes exact duplicate source objects without minting IDs."""

    id: Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
    line: Annotated[int, Field(gt=0)]


class CaptureSelection(StrictModel):
    """Turn end may still flush; a compaction/end backstop has a fixed observed upper bound."""

    instance: Nonempty
    session: Nonempty
    transcript: Path
    mode: Literal["turn", "stretch"]
    start: RecordAnchor | None
    end: RecordAnchor | None
    empty: bool = False

    @model_validator(mode="after")
    def bounded_coordinates(self) -> Self:
        if not self.transcript.is_absolute():
            raise ValueError("capture selection transcript must be absolute")
        if self.mode == "stretch" and not self.empty and self.end is None:
            raise ValueError("closed capture selection needs an observed end record")
        if self.mode == "turn" and (self.empty or self.end is not None):
            raise ValueError("turn capture keeps its upper bound open until the next boundary")
        return self


def _scope(snapshot: Snapshot, instance: str, session: str, policy: CapturePolicy) -> None:
    for record in snapshot.records:
        if (
            record.instance != instance
            or record.session != session
            or record.source.get(policy.session_field, record.session) != session
        ):
            raise SetupError("capture selection source ownership/session differs")


def _anchor(record: Record) -> RecordAnchor:
    return RecordAnchor(id=record.identity, line=record.source_line)


def pin_capture(
    snapshot: Snapshot,
    instance: str,
    session: str,
    policy: CapturePolicy,
    speech: SpeechPolicy,
    *,
    turn: bool,
) -> CaptureSelection:
    """Record only actual event-time source coordinates, never a guessed future note ID."""
    _scope(snapshot, instance, session, policy)
    start = next(
        (
            _anchor(row)
            for row in reversed(snapshot.records)
            if is_capture_boundary(row, policy, speech, last_turn=turn)
        ),
        None,
    )
    return CaptureSelection(
        instance=instance,
        session=session,
        transcript=snapshot.path,
        mode="turn" if turn else "stretch",
        start=start,
        end=_anchor(snapshot.records[-1]) if snapshot.records and not turn else None,
        empty=not snapshot.records and not turn,
    )


def _position(records: tuple[Record, ...], anchor: RecordAnchor) -> int:
    positions = [
        index
        for index, row in enumerate(records)
        if row.source_line == anchor.line and row.identity == anchor.id
    ]
    if len(positions) != 1:
        raise SetupError(
            "pinned capture boundary is unavailable or changed; restore or rebind explicitly"
        )
    return positions[0]


def select_capture(
    snapshot: Snapshot, selection: CaptureSelection, policy: CapturePolicy, speech: SpeechPolicy
) -> Snapshot:
    """Appends are safe; trimming/replacing a referenced anchor is a visible refusal."""
    if snapshot.path != selection.transcript:
        raise SetupError("capture selection names another transcript")
    _scope(snapshot, selection.instance, selection.session, policy)
    if selection.empty:
        return replace(snapshot, records=())
    start = _position(snapshot.records, selection.start) if selection.start else -1
    if selection.mode == "stretch":
        if selection.end is None:
            raise SetupError("closed capture selection has no end")
        last = _position(snapshot.records, selection.end)
        if last < start:
            raise SetupError("capture selection boundaries are reversed")
        end = last + 1
    else:
        end = next(
            (
                index
                for index in range(start + 1, len(snapshot.records))
                if is_capture_boundary(snapshot.records[index], policy, speech, last_turn=True)
            ),
            len(snapshot.records),
        )
    return replace(snapshot, records=snapshot.records[start + 1 : end])
