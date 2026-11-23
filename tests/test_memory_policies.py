"""Resource/typing/failure policies, without private services or real model artifacts."""

import socket
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from neo4j import GraphDatabase

from anamnema.graph_store import GraphStore
from anamnema.memory_config import load_memory
from anamnema.memory_rating import NoteRating, effective_trust, priority
from anamnema.service_config import load_service
from anamnema.service_http import JsonService
from anamnema.setup_config import SetupError, StrictModel


class Probe(StrictModel):
    value: bool


def test_http_limits_rate_auth_and_redacted_validation():
    calls = []
    settings = load_service().model_copy(update={"max_request_bytes": 32, "requests_per_period": 3})
    service = JsonService(settings, {"fixture": "fixture-token"}, "Synthetic")
    service.post(
        "/probe",
        Probe,
        lambda owner, body: calls.append((owner, body.value)) or {"ok": True},
        "Synthetic boundary probe",
    )
    client = TestClient(service.app)
    headers = {"Authorization": "Bearer fixture-token"}
    assert client.post("/probe", json={"value": True}).status_code == 401
    invalid = client.post("/probe", headers=headers, json={"value": "secret-value"})
    assert invalid.status_code == 422
    assert "secret-value" not in invalid.text
    assert client.post("/probe", headers=headers, content=b"x" * 33).status_code == 413
    assert client.post("/probe", headers=headers, json={"value": False}).status_code == 200
    assert calls == [("fixture", False)]
    assert client.post("/probe", headers=headers, json={"value": True}).status_code == 429


def test_graph_outage_is_not_empty_search():
    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        port = reservation.getsockname()[1]
        driver = GraphDatabase.driver(
            f"bolt://127.0.0.1:{port}", connection_timeout=0.1, max_transaction_retry_time=0
        )
        store = GraphStore(driver, load_memory())
        try:
            with pytest.raises(SetupError, match="graph read failed"):
                store.read("RETURN 1 AS value")
        finally:
            store.close()


def test_rating_age_is_not_trust_and_history_gets_no_freshness_boost():
    settings = load_memory().rating
    now = datetime.now(UTC)
    fresh = NoteRating(3.0, 1.0, False, "active", 0, 0, now)
    older = NoteRating(3.0, 1.0, False, "active", 0, 0, now - timedelta(days=365))
    assert effective_trust(fresh, settings) == effective_trust(older, settings)
    assert priority(fresh, now, True, settings) > priority(older, now, True, settings)
    assert priority(fresh, now, False, settings) == priority(older, now, False, settings)
    wrong = NoteRating(100.0, 1.0, True, "active", 50, 0, now)
    assert effective_trust(wrong, settings) < settings.floor
    assert priority(wrong, now, True, settings) < 100 * settings.floor
