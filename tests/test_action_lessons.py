"""Real-store lessons by fit: every admitted owned lesson near the action, nothing else."""

from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from test_lesson_graph import document, tokens
from test_lesson_graph import lessons as lessons
from test_lesson_read import nominate

from anamnema.action_lessons import ActionLessons, action_policy
from anamnema.lesson_policy import lesson_policy
from anamnema.memory_models import MemoryInputError
from anamnema.memory_service import create_memory_app
from anamnema.service_config import load_service

NEAR = (1.0, 0.0, 0.0, 0.0)
FAR = (0.0, 1.0, 0.0, 0.0)


class Encoder:
    """Query vectors by subject word, as the fixture encoder does for passages."""

    def __init__(self) -> None:
        self.calls: list[tuple[list[str], bool]] = []

    def encode(self, texts: list[str], passage: bool) -> list[tuple[float, ...]]:
        self.calls.append((texts, passage))
        return [NEAR if "retention" in text else FAR for text in texts]


def save(owner, graph, identity, embedding=NEAR):
    proposed = document(id=identity, statement=f"{identity} statement", embedding=embedding)
    proposed = proposed.model_copy(
        update={"episodes": (proposed.episodes[0].model_copy(update={"evidence": "ruling"}),)}
    )
    graph.write(owner, proposed, 0, tokens(owner, graph))


def test_every_near_admitted_lesson_comes_with_its_routes_not_a_fixed_count(lessons):
    owner, graph = lessons
    for identity in ("lesson-one", "lesson-two", "lesson-three"):
        save(owner, graph, identity)
    save(owner, graph, "lesson-far", FAR)
    encoder = Encoder()
    found = ActionLessons(graph.store, encoder, lesson_policy()).fitting(
        owner, "Bash\ncommand: purge retention"
    )["lessons"]
    assert sorted(item["id"] for item in found) == ["lesson-one", "lesson-three", "lesson-two"]
    assert all(item["similarity"] >= action_policy().min_similarity for item in found)
    assert found[0]["routes"] == [
        {"condition": "Before using retention", "correction": "Read the source", "exception": False}
    ]
    assert encoder.calls == [(["Bash\ncommand: purge retention"], False)]


def test_unrelated_stopped_and_foreign_lessons_stay_away(lessons):
    owner, store = lessons
    save(owner, store, "lesson-one")
    finder = ActionLessons(store.store, Encoder(), lesson_policy())
    assert finder.fitting(owner, "Bash\ncommand: ls")["lessons"] == []
    assert finder.fitting("someone-else", "purge retention")["lessons"] == []
    nominate(owner, store)
    assert finder.fitting(owner, "purge retention")["lessons"] == []


def test_action_bounds_are_explicit(lessons):
    owner, graph = lessons
    finder = ActionLessons(graph.store, Encoder(), lesson_policy())
    with pytest.raises(MemoryInputError):
        finder.fitting(owner, " ")
    with pytest.raises(MemoryInputError):
        finder.fitting(owner, "x" * (action_policy().max_action_characters + 1))


def test_http_advice_route_uses_the_bearer_owner(lessons):
    owner, graph = lessons
    save(owner, graph, "lesson-one")
    engine = SimpleNamespace(
        store=graph.store, encoder=Encoder(), lesson_settings=lesson_policy(), check=None
    )
    settings = load_service()
    client = TestClient(
        create_memory_app(engine, settings, {owner: "own-token", "other": "other-token"})  # type: ignore[arg-type]
    )
    body = {"action": "Bash\ncommand: purge retention", "cwd": "/work"}
    assert client.post(settings.advice_path, json=body).status_code == 401
    other = client.post(
        settings.advice_path, json=body, headers={"Authorization": "Bearer other-token"}
    )
    assert other.status_code == 200 and other.json() == {"lessons": []}
    own = client.post(
        settings.advice_path, json=body, headers={"Authorization": "Bearer own-token"}
    )
    assert own.status_code == 200, own.text
    assert [item["id"] for item in own.json()["lessons"]] == ["lesson-one"]
    extra = {**body, "count": 2}
    assert (
        client.post(
            settings.advice_path, json=extra, headers={"Authorization": "Bearer own-token"}
        ).status_code
        == 422
    )
