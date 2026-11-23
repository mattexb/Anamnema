"""Owned private atomic files shared by independent lifecycle journals."""

import os
import stat
import tempfile
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path

from anamnema.setup_config import SetupError


class PrivateStateBusy(SetupError):
    """A bounded caller may retry lock contention, never other state errors."""


class PrivateFiles:
    """Callers own schema/namespaces; this boundary owns safe filesystem operations."""

    def __init__(self, directory_mode: int, file_mode: int, temporary_prefix: str) -> None:
        self.directory_mode = directory_mode
        self.file_mode = file_mode
        self.temporary_prefix = temporary_prefix

    def directory(self, path: Path) -> None:
        """Create only nonsymlink private directories owned by the current user."""
        if any(p.is_symlink() for p in (path, *path.parents)):
            raise SetupError("private directories cannot contain symlinks")
        path.mkdir(parents=True, exist_ok=True, mode=self.directory_mode)
        self.existing_directory(path)

    def existing_directory(self, path: Path) -> None:
        """Never repair permissions or follow an unexpected directory redirect."""
        if any(p.is_symlink() for p in (path, *path.parents)):
            raise SetupError("private directories cannot contain symlinks")
        info = path.stat()
        if (
            not stat.S_ISDIR(info.st_mode)
            or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != self.directory_mode
        ):
            raise SetupError("private directory is not private and owned by this user")

    def check_file(self, descriptor: int) -> None:
        """Refuse public, linked, foreign or nonregular managed files before use."""
        info = os.fstat(descriptor)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or info.st_nlink != 1
            or stat.S_IMODE(info.st_mode) != self.file_mode
        ):
            raise SetupError("private file is not a private owned regular file")

    def read(self, path: Path, limit: int) -> bytes:
        """Bound reads before decoding; a fifo or redirected file never becomes state."""
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(descriptor, "rb") as stream:
            self.check_file(stream.fileno())
            data = stream.read(limit + 1)
        if len(data) > limit:
            raise SetupError("private file exceeds configured bound")
        return data

    def save(self, path: Path, content: bytes, *, written_ns: int | None = None) -> None:
        """Flush content and directory changes around atomic replacement."""
        descriptor, temporary = tempfile.mkstemp(dir=path.parent, prefix=self.temporary_prefix)
        temporary_path = Path(temporary)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                os.fchmod(stream.fileno(), self.file_mode)
                stream.write(content)
                stream.flush()
                if written_ns is not None:
                    os.utime(stream.fileno(), ns=(written_ns, written_ns))
                os.fsync(stream.fileno())
            os.replace(temporary_path, path)
            directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            temporary_path.unlink(missing_ok=True)

    @contextmanager
    def locked(self, path: Path) -> Generator[None]:
        """Process death releases the advisory lock, never durable pending work."""
        try:
            import fcntl
        except ImportError as exc:
            raise SetupError("private locking requires a POSIX host") from exc

        descriptor = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, self.file_mode)
        try:
            self.check_file(descriptor)
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise PrivateStateBusy("private state is busy; retry") from exc
            yield
        finally:
            os.close(descriptor)

    def cleanup_temporary(self, directory: Path, maximum: int) -> None:
        """Recover reserved atomic-write leftovers only while the caller holds its journal lock."""
        count = 0
        with os.scandir(directory) as entries:
            for entry in entries:
                if not entry.name.startswith(self.temporary_prefix):
                    continue
                count += 1
                if count > maximum:
                    raise SetupError("private temporary-file recovery bound exceeded; retry")
                descriptor = os.open(entry.path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
                try:
                    self.check_file(descriptor)
                finally:
                    os.close(descriptor)
                Path(entry.path).unlink()
