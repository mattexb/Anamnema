"""Streaming immutable prepared-input verification, never native dialogue parsing or rewriting."""

import hashlib
import os
import stat
from pathlib import Path

from anamnema.batch_models import BatchPolicy
from anamnema.setup_config import SetupError


def input_digest(path: Path, policy: BatchPolicy) -> str:
    """Bound bytes and reject changed, linked-path or nonregular input before a stage starts."""
    if not path.is_absolute() or any(part.is_symlink() for part in (path, *path.parents)):
        raise SetupError("batch prepared input must be absolute without symlink ancestors")
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(descriptor, "rb") as stream:
            before = os.fstat(stream.fileno())
            if not stat.S_ISREG(before.st_mode) or before.st_size > policy.max_input_bytes:
                raise SetupError("batch prepared input is not regular or exceeds its byte limit")
            hashed, total = hashlib.sha256(), 0
            while data := stream.read(
                min(policy.read_chunk_bytes, policy.max_input_bytes - total + 1)
            ):
                total += len(data)
                if total > policy.max_input_bytes:
                    raise SetupError("batch prepared input exceeds configured byte limit")
                hashed.update(data)
            after = os.fstat(stream.fileno())
        current = path.stat(follow_symlinks=False)
    except OSError as exc:
        raise SetupError(f"batch prepared input unavailable ({type(exc).__name__})") from exc
    signatures = [
        (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)
        for info in (before, after, current)
    ]
    if signatures[0] != signatures[1] or signatures[0] != signatures[2]:
        raise SetupError("batch prepared input changed during verification")
    return hashed.hexdigest()
