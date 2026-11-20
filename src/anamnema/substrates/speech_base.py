"""Reusable speech boundary: native readers translate; shared code filters."""

from abc import ABC, abstractmethod
from collections import Counter
from dataclasses import dataclass

from anamnema.archive_models import Record
from anamnema.processing import Window
from anamnema.speech import Row, model_row
from anamnema.speech_policy import SpeechPolicy


@dataclass(frozen=True)
class SpeechResult:
    """Eligible rows plus visible rejection and masking counts."""

    rows: tuple[Row, ...]
    dropped: dict[str, int]
    masked: int


class SpeechReader(ABC):
    """Substrate extenders translate one record, not memory-engine behavior."""

    def __init__(self, policy: SpeechPolicy) -> None:
        self.policy = policy

    @abstractmethod
    def translate(self, record: Record, number: int) -> Row | str:
        """Return attributed words or an explicit exclusion reason."""

    def read(self, window: Window) -> SpeechResult:
        """Apply common filtering while retaining stable source-sequence row numbers."""
        rows: list[Row] = []
        dropped: Counter[str] = Counter()
        masked = 0
        for number, source in enumerate(window.records):
            translated = self.translate(source, number)
            if isinstance(translated, str):
                dropped[translated] += 1
                continue
            row, count = model_row(translated, self.policy)
            masked += count
            if row is None:
                dropped["filtered"] += 1
            else:
                rows.append(row)
        return SpeechResult(tuple(rows), dict(dropped), masked)
