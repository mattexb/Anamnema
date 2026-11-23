"""Local resource assembly for the concrete window-batch engines; context owns every connection."""

from collections.abc import Generator
from contextlib import ExitStack, contextmanager

from anamnema.archive_access import connect, keyring_secret, keyring_uri
from anamnema.archive_mongo import MongoArchive
from anamnema.batch_archive import ArchiveWindow
from anamnema.batch_config import WindowBatchConfig
from anamnema.batch_prepare import PreparedInputs, preparation_policy
from anamnema.batch_window import WindowStages
from anamnema.extraction_config import extraction_config
from anamnema.extraction_store import ExtractionStore
from anamnema.graph_store import connect_graph
from anamnema.ingest_config import archive_settings
from anamnema.local_model import local_model
from anamnema.memory_config import load_memory
from anamnema.memory_loader import MemoryLoader
from anamnema.memory_sources import SourceLookup
from anamnema.merge_judge import MergeJudge
from anamnema.merge_policy import merge_policy
from anamnema.notes_policy import notes_policy
from anamnema.repeat_merge import RepeatMerger
from anamnema.search_services import CPUEncoder, LocalJSON, load_search_services
from anamnema.setup_config import SetupError, load_config
from anamnema.source_display import SourceDisplay
from anamnema.source_policy import source_policy
from anamnema.speech_policy import speech_policy
from anamnema.substrates.claude_speech import ClaudeSpeechReader
from anamnema.window_store import WindowStore


@contextmanager
def window_runtime(config: WindowBatchConfig, identity: str) -> Generator[WindowStages]:
    """No service starts, hooks, downloads, credentials files or remote runner are used."""
    setup, archive, memory = (
        load_config(config.setup_config),
        archive_settings(config.archive_config),
        load_memory(config.memory_config),
    )
    execution = extraction_config(config.extraction_config)
    speech, notes = speech_policy(config.speech_config), notes_policy(config.notes_config)
    merge = merge_policy(config.merge_config)
    if merge.enabled and config.merge_model_file is None:
        raise SetupError("enabled batch repeat merge requires an explicit merge model file")
    inputs = PreparedInputs(config.inputs_root, preparation_policy(config.preparation_policy))
    inputs.manifest(config.instance, identity)
    with ExitStack() as resources:
        graph = connect_graph(setup, archive, memory)
        resources.callback(graph.close)
        mongo = connect(keyring_uri(setup, archive), archive, setup.services.archive)
        resources.callback(mongo.close)
        original = MongoArchive(mongo[archive.database], archive)
        windows = WindowStore(original, source_policy(config.source_config))
        sources = SourceLookup(
            original,
            memory.max_items,
            memory.max_source_bytes,
            SourceDisplay(windows, ClaudeSpeechReader(speech)),
        )
        policy = load_search_services(config.search_config)
        token = keyring_secret(setup, archive, policy.encoder_keyring_entry, policy.token_field)
        encoder = CPUEncoder(
            LocalJSON(setup.services.encoder, policy, token, policy.timeout_seconds)
        )
        saved = ExtractionStore(original.database, execution, notes, speech)
        provider = local_model(setup, archive, execution, config.model_file)
        merger = None
        if merge.enabled and config.merge_model_file is not None:
            judge = MergeJudge(
                local_model(setup, archive, execution, config.merge_model_file), merge
            )
            merger = RepeatMerger(graph, merge, judge)
        loader = MemoryLoader(graph, sources, saved, encoder, merger)
        graph.ensure_schema()
        yield WindowStages(
            ArchiveWindow(inputs, config.instance, identity, original, windows),
            saved,
            provider,
            loader,
            setup,
        )
