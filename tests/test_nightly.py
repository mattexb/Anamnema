"""Actual Neo4j selection: the chain runs each due stage's command and records every outcome."""

import json
import sys
import uuid
from pathlib import Path
from typing import Any

import pytest
from test_graph import note

from anamnema import cli
from anamnema.card_graph import CardGraph
from anamnema.card_policy import card_policy
from anamnema.nightly import (
    STDERR_KEY,
    Nightly,
    NightlyConfig,
    diagnostic,
    lesson_notes,
    run_command,
)
from anamnema.private_files import PrivateStateBusy


def _config(tmp_path: Path, owner: str, **changes: Any) -> NightlyConfig:
    values: dict[str, Any] = {
        "instance": owner,
        "executable": tmp_path / "anamnema",
        "state_directory": tmp_path / "nightly",
        "common": ("--config", str(tmp_path / "setup.json")),
        "model_file": tmp_path / "notes.gguf",
        "card_model_file": tmp_path / "cards.gguf",
        "lesson_model_file": tmp_path / "lessons.gguf",
        "link_limit": 50,
        "naming_rounds": 2,
        "max_cards": 10,
        "lesson_batch": 40,
        "stage_deadline_seconds": 30.0,
        "max_output_bytes": 65536,
        "history": 2,
    }
    return NightlyConfig(**{**values, **changes})


class Recorder:
    def __init__(self, fail: str = "") -> None:
        self.commands: list[list[str]] = []
        self.fail = fail

    def __call__(self, command: list[str], _deadline: float, _limit: int) -> dict[str, Any]:
        self.commands.append(command)
        failed = command[1] == self.fail
        return {"exit": 2 if failed else 0, "seconds": 0.0, "summary": {}}


@pytest.fixture
def owner(graph: Any) -> str:
    name = "nightly-" + uuid.uuid4().hex
    graph.put(name, (note(name, kind="failure"), note(name, "note-two", kind="fact")))
    cards = CardGraph(graph, card_policy())
    cards.create(name, "Andrea", "person", "fixture person")
    cards.create(name, "Lonely", "person", "fixture person without notes")
    cards.link(name, "Andrea", "note-two", "fixture link")
    return name


def test_due_stages_run_in_order_with_their_selected_inputs(
    graph: Any, owner: str, tmp_path: Path
) -> None:
    recorder = Recorder()
    record = Nightly(_config(tmp_path, owner), graph, recorder).run()
    verbs = [command[1] for command in recorder.commands]
    assert verbs == ["link-entities", "build-cards", "name-notes", "name-notes", "build-lessons"]
    cards = recorder.commands[1]
    assert cards[cards.index("--entity") + 1] == "Andrea"
    lessons = recorder.commands[-1]
    assert lessons.count("--note") == 1 and lessons[lessons.index("--note") + 1] == "note-one"
    assert all(command[2:4] == ["--instance", owner] for command in recorder.commands)
    assert record["failed"] == []
    assert lesson_notes(graph, owner, ("failure",), 10) == ["note-one"]


def test_a_failed_stage_is_recorded_and_the_independent_stages_still_run(
    graph: Any, owner: str, tmp_path: Path
) -> None:
    recorder = Recorder(fail="link-entities")
    record = Nightly(_config(tmp_path, owner), graph, recorder).run()
    assert record["failed"] == ["links"]
    assert [c[1] for c in recorder.commands][1:] == [
        "build-cards",
        "name-notes",
        "name-notes",
        "build-lessons",
    ]


def test_naming_stops_at_its_first_failure_and_runs_are_pruned_to_history(
    graph: Any, owner: str, tmp_path: Path
) -> None:
    config = _config(tmp_path, owner)
    for _ in range(3):
        Nightly(config, graph, Recorder(fail="name-notes")).run()
    runs = sorted((config.state_directory / "runs").glob("*.json"))
    assert len(runs) == 2
    assert '"naming"' in runs[-1].read_text(encoding="utf-8")


def test_one_chain_at_a_time(graph: Any, owner: str, tmp_path: Path) -> None:
    config = _config(tmp_path, owner)
    nightly = Nightly(config, graph, Recorder())
    config.state_directory.mkdir(mode=0o700)
    with nightly.files.locked(config.state_directory / ".nightly.lock"):
        with pytest.raises(PrivateStateBusy):
            nightly.run()


def test_the_real_runner_bounds_time_and_output(tmp_path: Path) -> None:
    ok = run_command([sys.executable, "-c", "print('{\"done\": 1}')"], 10.0, 1000)
    assert ok["exit"] == 0 and ok["summary"] == {"done": 1}
    slow = run_command([sys.executable, "-c", "import time; time.sleep(30)"], 0.3, 1000)
    assert slow["exit"] is None and "timed out" in slow["error"] and slow["seconds"] < 5
    # The bound holds while reading: an endless writer is cut off, never buffered whole.
    loud = run_command([sys.executable, "-c", "while True: print('x' * 4096)"], 10.0, 100)
    assert loud["exit"] is None and "output exceeds configured bound" in loud["error"]
    noisy = run_command(
        [sys.executable, "-c", "import sys; sys.stderr.write('e' * 5000)"], 10.0, 100
    )
    assert "output exceeds configured bound" in noisy["error"]
    prose = run_command([sys.executable, "-c", "print('hello')"], 10.0, 100)
    assert prose["error"] == "output is not JSON"


def test_a_changed_held_briefing_is_rebuilt_and_recorded_as_its_replacement(
    graph: Any, owner: str, tmp_path: Path
) -> None:
    from anamnema.briefing_refresh import load_replacements
    from anamnema.nightly import NightlyBriefings

    stacks = tmp_path / "stacks"
    folder = stacks / "session-digest"
    folder.mkdir(parents=True, mode=0o700)
    stacks.chmod(0o700)
    held = {"k1": {"briefing": "old-bundle", "page": 1, "content_bytes": 10, "dropped": False}}
    state = folder / "stack.json"
    state.write_text(
        json.dumps({"instance": owner, "session": "s1", "held": held}),
        encoding="utf-8",
    )
    state.chmod(0o600)
    briefings = NightlyBriefings(
        briefing_root=tmp_path / "bundles",
        stack_root=stacks,
        judge_config=tmp_path / "judge.json",
        max_rebuilds=5,
    )
    config = _config(tmp_path, owner, briefings=briefings)

    def runner(command: list[str], _deadline: float, _limit: int) -> dict[str, Any]:
        if command[1] == "brief":
            assert command[command.index("--rebuild") + 1] == "old-bundle"
            summary = {"briefing": "new-bundle", "status": "ready", "unchanged": False}
            return {"exit": 0, "seconds": 0.0, "summary": summary}
        return {"exit": 0, "seconds": 0.0, "summary": {}}

    record = Nightly(config, graph, runner).run()
    assert record["stages"][-1]["stage"] == "briefing:old-bundle"
    replacements = load_replacements(config.state_directory, owner, 4096)
    assert replacements.current("old-bundle") == "new-bundle"


def test_execution_and_search_profiles_reach_only_the_commands_that_take_them(
    graph: Any, owner: str, tmp_path: Path
) -> None:
    extraction, search = tmp_path / "execution.json", tmp_path / "search.json"
    config = _config(tmp_path, owner, extraction_config=extraction, search_config=search)
    recorder = Recorder()
    chain = Nightly(config, graph, recorder)
    parser = cli._parser()  # pyright: ignore[reportPrivateUsage]
    accepted = {
        verb: set(sub._option_string_actions)  # pyright: ignore[reportPrivateUsage]
        for action in parser._subparsers._group_actions  # pyright: ignore
        for verb, sub in getattr(action, "choices", {}).items()
    }
    for verb in ("link-entities", "build-cards", "name-notes", "build-lessons", "brief"):
        argv = chain._base(verb)  # pyright: ignore[reportPrivateUsage]
        assert ("--extraction-config" in argv) == (verb != "brief"), verb
        assert ("--search-config" in argv) == (verb != "link-entities"), verb
        # Every option the chain passes is one the real subcommand parser declares.
        assert {a for a in argv[2:] if a.startswith("--")} <= accepted[verb], verb
    chain.run()
    for command in recorder.commands:
        parsed = parser.parse_args(command[1:])  # a full stage command parses strictly
        assert parsed.extraction_config == extraction, command[1]


def test_without_the_profiles_no_option_is_added(graph: Any, owner: str, tmp_path: Path) -> None:
    chain = Nightly(_config(tmp_path, owner), graph, Recorder())
    argv = chain._base("build-cards")  # pyright: ignore[reportPrivateUsage]
    assert "--extraction-config" not in argv and "--search-config" not in argv


def test_relative_profile_paths_are_refused(owner: str, tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="absolute"):
        _config(tmp_path, owner, extraction_config=Path("execution.json"))


def test_the_real_runner_hands_stderr_only_for_a_failed_stage() -> None:
    failed = run_command(
        [sys.executable, "-c", "import sys; sys.stderr.write('why'); sys.exit(2)"], 10.0, 1000
    )
    assert failed["exit"] == 2 and failed[STDERR_KEY] == b"why"
    quiet = run_command(
        [sys.executable, "-c", "import sys; sys.stderr.write('note'); print('{}')"], 10.0, 1000
    )
    assert quiet["exit"] == 0 and STDERR_KEY not in quiet


def test_a_structured_error_is_kept_cut_to_the_tail() -> None:
    reason = json.dumps({"error": "lesson answer names an unknown lesson label"}).encode()
    assert diagnostic(reason, 4096) == {"error": "lesson answer names an unknown lesson label"}
    assert diagnostic(reason, 6) == {"error": "lesson"}
    assert diagnostic(b"x" * 10 + b"Traceback: end", 8) == {"stderr_tail": "ack: end"}
    assert diagnostic(b'{"error": 3}', 50) == {"stderr_tail": '{"error": 3}'}


class Failing(Recorder):
    """Lessons fail with the CLI's SetupError JSON on stderr, as the guest run did."""

    def __call__(self, command: list[str], deadline: float, limit: int) -> dict[str, Any]:
        result = super().__call__(command, deadline, limit)
        if command[1] == "build-lessons":
            reason = json.dumps({"error": "lesson answer names an unknown lesson label"})
            result.update(exit=2, **{STDERR_KEY: reason.encode()})
        return result


def test_a_failed_stage_reason_stays_in_the_private_run_file(
    graph: Any, owner: str, tmp_path: Path
) -> None:
    config = _config(tmp_path, owner)
    record = Nightly(config, graph, Failing()).run()
    lessons = record["stages"][-1]
    assert record["failed"] == ["lessons"]
    assert lessons["diagnostic_kept"] is True and "diagnostic" not in lessons
    assert "unknown lesson label" not in json.dumps(record)
    (run,) = (config.state_directory / "runs").glob("*.json")
    assert run.stat().st_mode & 0o777 == 0o600
    kept = json.loads(run.read_text(encoding="utf-8"))["stages"]
    assert kept[-1]["diagnostic"] == {"error": "lesson answer names an unknown lesson label"}
    assert all(STDERR_KEY not in stage for stage in kept)


def test_the_tail_is_bounded_by_the_output_bound_and_defaults_for_old_configs(
    owner: str, tmp_path: Path
) -> None:
    assert _config(tmp_path, owner).diagnostic_tail_bytes == 4096
    with pytest.raises(ValueError, match="diagnostic_tail_bytes"):
        _config(tmp_path, owner, max_output_bytes=100, diagnostic_tail_bytes=101)
