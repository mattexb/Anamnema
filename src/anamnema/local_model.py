"""One explicit local model selection shared by semantic commands, never service discovery."""

from pathlib import Path

from anamnema.archive_access import keyring_secret
from anamnema.extraction_config import ExtractionConfig
from anamnema.ingest_config import ArchiveSettings
from anamnema.llama_provider import LlamaProvider
from anamnema.setup_config import SetupConfig, SetupError


def local_model(
    setup: SetupConfig, archive: ArchiveSettings, execution: ExtractionConfig, model_file: Path
) -> LlamaProvider:
    """Local CPU/Vulkan transport; files and resident model identity must agree."""
    if setup.backend not in {"vulkan", "cpu"}:
        raise SetupError("semantic generation supports local CPU/Vulkan llama.cpp only")
    token = None
    if execution.model_keyring_entry and execution.model_keyring_field:
        token = keyring_secret(
            setup, archive, execution.model_keyring_entry, execution.model_keyring_field
        )
    return LlamaProvider(setup.services.notes, execution, model_file, token)
