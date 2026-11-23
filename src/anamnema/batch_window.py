"""Concrete archive/extraction/storage adapters over the existing window engines."""

from collections.abc import Callable

from pydantic import JsonValue

from anamnema.archive_models import digest
from anamnema.batch_archive import ArchiveWindow
from anamnema.batch_models import BatchPlan, Metrics
from anamnema.binding import bindings
from anamnema.chunks import Chunk, chunks
from anamnema.extraction_store import ExtractionStore
from anamnema.extractor import Extractor
from anamnema.llama_provider import LlamaProvider
from anamnema.memory_loader import MemoryLoader
from anamnema.notes_request import run_identity
from anamnema.processing import Window
from anamnema.setup_config import SetupConfig, SetupError
from anamnema.substrates.claude_speech import ClaudeSpeechReader


class WindowStages:
    """The core three stages are not a completed nightly/finishing workflow."""

    def __init__(
        self,
        archive: ArchiveWindow,
        extraction: ExtractionStore,
        provider: LlamaProvider,
        loader: MemoryLoader,
        deployment: SetupConfig,
    ) -> None:
        if (
            extraction.database.client is not archive.archive.database.client
            or extraction.database.name != archive.archive.database.name
            or loader.extraction is not extraction
            or loader.sources.archive is not archive.archive
            or loader.sources.windows is not archive.windows
        ):
            raise SetupError(
                "batch stages must share the same verified archive and extraction store"
            )
        self.archive, self.extraction, self.provider, self.loader = (
            archive,
            extraction,
            provider,
            loader,
        )
        self.run_key = run_identity(provider.identity, extraction.notes, extraction.speech)
        self.profile = digest(
            {
                "model": provider.identity,
                "deployment": deployment.model_dump(mode="json"),
                "archive_database": archive.archive.database.name,
                "notes": extraction.notes.model_dump(mode="json"),
                "speech": extraction.speech.model_dump(mode="json"),
                "extraction": extraction.config.model_dump(mode="json"),
                "memory": loader.store.settings.model_dump(mode="json"),
                "encoder": loader.encoder.policy.model_dump(mode="json"),
                "archive": archive.archive.settings.model_dump(mode="json"),
                "source": archive.windows.policy.model_dump(mode="json"),
                "merge": loader.merger.judge.identity if loader.merger else None,
            }
        )

    def operations(self) -> dict[str, Callable[[], Metrics]]:
        return {"archive": self.archive.archive_input, "extract": self.extract, "store": self.store}

    def plan(self) -> BatchPlan:
        return BatchPlan(
            instance=self.archive.instance,
            session=self.archive.manifest.session,
            input_path=self.archive.inputs.path(self.archive.instance, self.archive.identity),
            input_digest=self.archive.manifest.identity,
            profile_digest=self.profile,
            stages=tuple(self.operations()),
        )

    def _input(self) -> tuple[Window, tuple[Chunk, ...]]:
        window = self.archive.load()
        rows = ClaudeSpeechReader(self.extraction.speech).read(window).rows
        parts = chunks(rows, bindings(rows, self.extraction.notes), self.extraction.notes)
        return window, parts

    def extract(self) -> Metrics:
        window, parts = self._input()
        result = Extractor(self.provider, self.extraction).run([(window, parts)])
        keys = ("created_chunks", "reused_chunks", "model_calls", "notes", "flagged_notes")
        return self._counters(result, keys)

    def store(self) -> Metrics:
        window, parts = self._input()
        return {
            key: value for key, value in self.loader.window(window, parts, self.run_key).items()
        }

    @staticmethod
    def _counters(result: dict[str, JsonValue], keys: tuple[str, ...]) -> Metrics:
        counts: Metrics = {}
        for key in keys:
            value = result.get(key)
            if type(value) is not int:
                raise SetupError("batch stage returned invalid verified progress counters")
            counts[key] = value
        return counts
