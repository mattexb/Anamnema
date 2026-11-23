"""Retry only an explicitly unavailable service, never invalid credentials or configuration."""

import time
from collections.abc import Callable

from anamnema.setup_config import SetupError


class ServiceNotReady(SetupError):
    """A refused/timed-out connection or an explicit loading response may heal by waiting."""


def wait_ready[T](operation: Callable[[], T], seconds: float, poll: float, context: str) -> T:
    """Bound the retry period; each operation retains its configured per-request deadline."""
    deadline = time.monotonic() + seconds
    while True:
        try:
            return operation()
        except ServiceNotReady as exc:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise SetupError(f"{context} readiness retry budget exhausted: {exc}") from exc
            time.sleep(min(poll, remaining))
