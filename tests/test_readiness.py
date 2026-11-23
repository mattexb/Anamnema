"""Only explicit transient readiness errors retry; permanent errors remain specific."""

import pytest

from anamnema.readiness import ServiceNotReady, wait_ready
from anamnema.setup_config import SetupError


def test_transient_refusal_retries_and_then_returns():
    attempts = []

    def operation():
        attempts.append(True)
        if len(attempts) == 1:
            raise ServiceNotReady("synthetic connection refused")
        return "ready"

    assert wait_ready(operation, 1.0, 0.001, "fixture") == "ready"
    assert len(attempts) == 2


def test_permanent_error_is_not_delayed_or_replaced():
    attempts = []

    def operation():
        attempts.append(True)
        raise SetupError("synthetic missing keyring entry")

    with pytest.raises(SetupError, match="synthetic missing keyring entry"):
        wait_ready(operation, 1.0, 0.1, "fixture")
    assert len(attempts) == 1


def test_exhaustion_keeps_last_safe_transport_error():
    def operation():
        raise ServiceNotReady("synthetic refusal detail")

    with pytest.raises(
        SetupError, match="readiness retry budget exhausted: synthetic refusal detail"
    ):
        wait_ready(operation, 0.01, 0.001, "fixture")
