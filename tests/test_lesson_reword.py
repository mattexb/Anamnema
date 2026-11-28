"""A lesson's routes reworded by hand: a new version, the old kept, evidence still checked."""

import json
from argparse import Namespace

import pytest
from test_lesson_graph import document, tokens
from test_lesson_graph import lessons as lessons

from anamnema.graph_store import MemoryDenied
from anamnema.lesson_cli import run_reword
from anamnema.lesson_models import LessonRoute
from anamnema.lesson_reword import HAND, reword_routes
from anamnema.setup_config import SetupError

WIDER = LessonRoute(
    condition="Before any gate run, whole or on changed files",
    correction="Read the source",
    notes=("note-one",),
    exception=False,
)


class Encoder:
    def __init__(self) -> None:
        self.texts: list[str] = []

    def encode(self, texts: list[str], passage: bool) -> list[tuple[float, ...]]:
        assert passage
        self.texts += texts
        return [(0.0, 1.0, 0.0, 0.0) for _ in texts]


def test_a_reword_is_a_new_version_with_the_old_routes_in_history(lessons):
    owner, graph = lessons
    graph.write(owner, document(), 0, tokens(owner, graph))
    encoder = Encoder()
    answer = reword_routes(graph, encoder, owner, "lesson-one", (WIDER,), "condition too narrow")  # type: ignore[arg-type]
    assert answer == {"lesson": "lesson-one", "version": 2, "routes": 1}
    stored = graph.get(owner, "lesson-one")
    assert stored["routes"][0]["condition"] == WIDER.condition
    assert stored["model"] == f"{HAND}: condition too narrow"
    assert json.loads(stored["history"][0]["routes"])[0]["condition"] == "Before using retention"
    assert "Before any gate run" in encoder.texts[0]  # the passage is embedded from the new text


def test_a_reword_needs_routes_a_reason_and_cited_episode_notes(lessons):
    owner, graph = lessons
    graph.write(owner, document(), 0, tokens(owner, graph))
    with pytest.raises(SetupError, match="route and a reason"):
        reword_routes(graph, Encoder(), owner, "lesson-one", (), "r")  # type: ignore[arg-type]
    with pytest.raises(SetupError, match="route and a reason"):
        reword_routes(graph, Encoder(), owner, "lesson-one", (WIDER,), " ")  # type: ignore[arg-type]
    foreign = WIDER.model_copy(update={"notes": ("note-elsewhere",)})
    with pytest.raises(ValueError, match="cite supplied episode notes"):
        reword_routes(graph, Encoder(), owner, "lesson-one", (foreign,), "r")  # type: ignore[arg-type]
    with pytest.raises(MemoryDenied):
        reword_routes(graph, Encoder(), "someone-else", "lesson-one", (WIDER,), "r")  # type: ignore[arg-type]


def test_the_command_reads_a_checked_routes_file(lessons, tmp_path):
    owner, graph = lessons
    graph.write(owner, document(), 0, tokens(owner, graph))
    path = tmp_path / "routes.json"
    path.write_text(json.dumps([WIDER.model_dump(mode="json")]), encoding="utf-8")
    args = Namespace(instance=owner, lesson="lesson-one", routes_file=path, reason="too narrow")
    assert run_reword(args, graph.store, Encoder())["version"] == 2  # type: ignore[arg-type]
    path.write_text('[{"condition": "c"}]', encoding="utf-8")
    with pytest.raises(SetupError, match="routes file is invalid"):
        run_reword(args, graph.store, Encoder())  # type: ignore[arg-type]
