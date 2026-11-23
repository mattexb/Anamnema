"""Pure bounded briefing evidence preparation; neither note data nor source rows are mutated."""

from dataclasses import dataclass
from typing import Any

from anamnema.archive_models import canonical
from anamnema.briefing_policy import BriefingPolicy
from anamnema.setup_config import SetupError
from anamnema.speech import scrub
from anamnema.speech_policy import SpeechPolicy


@dataclass(frozen=True)
class BriefBatch:
    """Short labels and their owned identities remain tied to the exact rendered evidence."""

    labels: dict[str, str]
    text: str


@dataclass(frozen=True)
class BriefInputs:
    """Every selected note remains accounted for even beyond the configured read budget."""

    batches: tuple[BriefBatch, ...]
    unread: tuple[str, ...]
    source_unavailable: tuple[str, ...]
    broad: bool


def prepare_briefing(
    notes: list[dict[str, Any]],
    sources: dict[str, list[str]],
    policy: BriefingPolicy,
    speech: SpeechPolicy,
) -> BriefInputs:
    """Pack whole sanitized note blocks; a read limit leaves an explicit never-read tail."""
    identities = [str(note["id"]) for note in notes]
    if len(identities) != len(set(identities)) or any(not i for i in identities):
        raise SetupError("briefing inputs contain repeated or empty note identities")
    batches: list[BriefBatch] = []
    unavailable: list[str] = []
    unread: list[str] = []
    size = 0
    exhausted = False
    for index, (identity, note) in enumerate(zip(identities, notes, strict=True), 1):
        label = f"n{index}"
        rows = sources.get(identity, [])
        if not rows:
            unavailable.append(identity)
        block = note_block(note, label, rows, policy, speech)
        weight = len(block.encode("utf-8"))
        if weight > policy.batch_bytes:
            raise SetupError("one briefing note exceeds the configured batch byte limit")
        separator = len(b"\n\n") if size else 0
        if exhausted or size + separator + weight > policy.read_bytes:
            unread.append(identity)
            exhausted = True
            continue
        size += separator + weight
        if (
            batches
            and len((batches[-1].text + "\n\n" + block).encode("utf-8")) <= policy.batch_bytes
        ):
            previous = batches.pop()
            batches.append(
                BriefBatch({**previous.labels, label: identity}, previous.text + "\n\n" + block)
            )
        else:
            batches.append(BriefBatch({label: identity}, block))
    return BriefInputs(
        tuple(batches),
        tuple(unread),
        tuple(unavailable),
        size > policy.read_bytes * policy.broad_share or bool(unread),
    )


def note_block(
    note: dict[str, Any], label: str, rows: list[str], policy: BriefingPolicy, speech: SpeechPolicy
) -> str:
    """Preserve dated trust/status/wrongness and mark clipped dialogue rather than inventing it."""
    metadata = {
        "kind": note.get("kind"),
        "said_at": note.get("said_at"),
        "status": note.get("status", "active"),
        "checked": "WRONG" if note.get("wrong_open") else "not marked wrong",
        "trust_positive": note.get("trust_positive", 1),
        "trust_negative": note.get("trust_negative", 1),
        "author": note.get("author", "extracted"),
    }
    block = [f"[{label}] " + canonical(metadata).decode("utf-8"), str(note["text"])]
    for row in rows[: policy.source_rows]:
        clipped = row[: policy.row_characters]
        block.append("> " + clipped + (" [clipped]" if len(row) > policy.row_characters else ""))
    return scrub("\n".join(block), speech)[0]
