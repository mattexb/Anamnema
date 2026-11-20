"""Save an explicit nonsecret setup document without replacing existing files."""

from __future__ import annotations

import json
import os
import tempfile
from importlib.resources import files
from pathlib import Path

from anamnema.setup_config import SetupConfig, SetupError


def save_answers(path: Path, config: SetupConfig) -> None:
    """Publish a complete restricted-mode file atomically and without overwrite."""
    policy = json.loads(
        files("anamnema").joinpath("config/wizard.json").read_text(encoding="utf-8")
    )
    path = path.expanduser()
    temporary: Path | None = None
    try:
        if path.exists() or path.is_symlink():
            raise SetupError("answer file already exists; choose a new filename")
        descriptor, name = tempfile.mkstemp(dir=path.parent, prefix=policy["temporary_prefix"])
        temporary = Path(name)
        with os.fdopen(descriptor, "wb") as stream:
            os.fchmod(stream.fileno(), policy["answer_file_mode"])
            stream.write((config.model_dump_json(indent=2) + "\n").encode("utf-8"))
            stream.flush()
            os.fsync(stream.fileno())
        # Atomic no-clobber publication also refuses a concurrently created symlink.
        os.link(temporary, path, follow_symlinks=False)
    except OSError as exc:
        raise SetupError(f"answers could not be saved ({type(exc).__name__})") from exc
    finally:
        if temporary is not None:
            temporary.unlink()
