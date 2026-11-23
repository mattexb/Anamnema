"""Actual fixture child processes prove producer boundaries, not native Claude acceptance."""

import json
import os
import subprocess
import sys

import pytest
from pydantic import ValidationError
from test_continuity import continuity as continuity
from test_continuity import snapshot_text, state

from anamnema.continuity_state import StateFiles
from anamnema.setup_config import SetupError
from anamnema.snapshot_excerpt import chunk_entries, cut_mark, fit_entry
from anamnema.snapshot_policy import SnapshotWorkerConfig, producer_policy
from anamnema.snapshot_producer import SnapshotProducer
from anamnema.snapshot_worker import run_worker
from anamnema.speech_policy import speech_policy
from anamnema.worker_registry import RegistryConfig

WORKER = """import json, os, pathlib, stat, sys, time
root = pathlib.Path.cwd()
mode = json.loads((root / "behavior.json").read_text(encoding="utf-8"))
position = sys.argv.index("--add-dir") + 1
directories = []
while position < len(sys.argv) and not sys.argv[position].startswith("--"):
    directories.append(sys.argv[position])
    position += 1
if len(directories) != 1:
    print("variadic add-dir consumed the prompt", file=sys.stderr)
    sys.exit(64)
folder = pathlib.Path(directories[0])
paths = sorted(folder.glob("*.txt"))
report = {"pid": os.getpid(), "arguments": sys.argv[1:],
          "directory_mode": stat.S_IMODE(folder.stat().st_mode),
          "inputs": [{"mode": stat.S_IMODE(p.stat().st_mode),
                      "text": p.read_text(encoding="utf-8")} for p in paths]}
if mode["action"] == "hook":
    from anamnema.continuity import Continuity
    from anamnema.continuity_config import continuity_config, continuity_policy
    core = Continuity(continuity_config(root / "core-config.json"), continuity_policy())
    report["concurrent_stop"] = core.stop("session-one", "worker is running")
(root / "report.json").write_text(json.dumps(report), encoding="utf-8")
if mode["action"] == "sleep":
    time.sleep(60)
elif mode["action"] == "overflow":
    os.write(2, b"x" * 100000)
elif mode["action"] == "error":
    print("fixture-sensitive-output", file=sys.stderr)
    sys.exit(7)
elif mode["action"] == "invalid_utf8":
    os.write(1, b"\\xff")
else:
    default = "## 1. State\\nFixture evidence preserved.\\n## 2. Active thread\\nContinue."
    print(mode.get("body", default))
"""


def transcript(tmp_path, rows=None):
    path = tmp_path / "synthetic.jsonl"
    if rows is None:
        rows = [
            {
                "type": "user",
                "sessionId": "session-one",
                "timestamp": "2026-01-01",
                "message": {"content": "Preserve this unfinished task."},
            }
        ]
    path.write_text(
        "\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n", encoding="utf-8"
    )
    return path


@pytest.fixture
def producer(continuity, tmp_path):
    executable = tmp_path / "fixture-worker"
    executable.write_text(f"#!{sys.executable}\n" + WORKER, encoding="utf-8")
    executable.chmod(0o700)
    (tmp_path / "behavior.json").write_text('{"action":"good"}', encoding="utf-8")
    (tmp_path / "core-config.json").write_text(
        continuity.config.model_dump_json(), encoding="utf-8"
    )
    worker = SnapshotWorkerConfig(
        executable=executable,
        cwd=tmp_path,
        input_root=tmp_path / "private-inputs",
        model="explicit-model",
    )
    policy = producer_policy().model_copy(update={"timeout_seconds": 2.0})
    return SnapshotProducer(continuity, policy, worker, speech_policy())


def behavior(producer, action, **extra):
    (producer.worker.cwd / "behavior.json").write_text(
        json.dumps({"action": action, **extra}), encoding="utf-8"
    )


@pytest.mark.parametrize("action", ["good", "error"])
def test_guard_record_outlives_worker_until_temporary_inputs_are_removed(
    producer, monkeypatch, action
):
    registry = RegistryConfig(
        records_dir=producer.worker.cwd / "worker-records",
        folder_roots=(producer.worker.input_root,),
    )
    producer.worker = producer.worker.model_copy(update={"registry": registry})
    producer.policy = producer.policy.model_copy(update={"timeout_seconds": 5.0})
    behavior(producer, action)
    actual = run_worker
    observed = []

    def inspect(*args, **kwargs):
        record = kwargs["record"]
        assert record.is_file()
        folder = producer.worker.input_root / json.loads(record.read_bytes())["folders"][0]
        try:
            return actual(*args, **kwargs)
        finally:
            assert record.is_file() and folder.is_dir()
            observed.append(record)

    monkeypatch.setattr("anamnema.snapshot_producer.run_worker", inspect)
    source = transcript(producer.worker.cwd)
    if action == "error":
        with pytest.raises(SetupError, match="output withheld"):
            producer.produce("session-one", source, "manual")
    else:
        producer.produce("session-one", source, "manual")
    assert len(observed) == 1 and not observed[0].exists()
    assert not list(producer.worker.input_root.iterdir())


def report(producer):
    return json.loads((producer.worker.cwd / "report.json").read_text(encoding="utf-8"))


def cleaned(producer):
    assert not producer.worker.input_root.exists() or not list(producer.worker.input_root.iterdir())


def test_actual_worker_stages_then_existing_start_stop_flow_applies_and_spools(producer, tmp_path):
    core = producer.core
    path = transcript(tmp_path)
    original = path.read_bytes()
    result = producer.produce("session-one", path, "manual")
    files, held = state(core)
    assert result["applied"] is False and held.snapshot and not held.snapshot.applied
    assert "Fixture evidence preserved" in files.content(held.snapshot.blob)
    assert core.stop("session-one", "not applied yet") == ""
    context = core.start("session-one", "compact")
    _, applied = state(core)
    assert applied.snapshot and core.snapshot_ack("session-one", applied.snapshot) in context
    proof = "\n".join(f"{core.policy.read_marker} {r.token}" for r in applied.reads)
    proof += "\n" + core.snapshot_ack("session-one", applied.snapshot)
    assert core.stop("session-one", proof) == ""
    assert state(core)[1].snapshot is None
    assert (files.folder / core.policy.spool_directory / held.snapshot.blob).is_file()
    seen = report(producer)
    assert seen["directory_mode"] == 0o700 and seen["inputs"][0]["mode"] == 0o600
    arguments = seen["arguments"]
    offset = arguments.index("--print")
    assert arguments[offset : offset + len(producer.policy.worker_arguments)] == list(
        producer.policy.worker_arguments
    )
    assert arguments[arguments.index("--add-dir") + 2] == "--print"
    assert arguments[-3] == "--no-chrome"
    assert arguments[-2] == "--"
    assert arguments[-1].startswith("# Compaction snapshot")
    assert arguments[arguments.index("--model") + 1] == "explicit-model"
    assert json.loads(arguments[arguments.index("--settings") + 1]) == {"disableAllHooks": True}
    assert path.read_bytes() == original
    cleaned(producer)


def test_real_child_can_run_stop_during_generation_without_session_lock(producer, tmp_path):
    behavior(producer, "hook")
    producer.produce("session-one", transcript(tmp_path), "auto")
    assert report(producer)["concurrent_stop"] == ""
    cleaned(producer)


def test_raw_unicode_separators_are_json_content_not_record_boundaries(producer, tmp_path):
    body = "First\u2028second\u2029third\u0085fourth"
    path = transcript(tmp_path, [{"type": "user", "message": {"content": body}}])
    assert "\u2028" in path.read_text(encoding="utf-8")
    assert "\u0085" in path.read_text(encoding="utf-8")
    producer.produce("session-one", path, "manual")
    copied = "\n".join(item["text"] for item in report(producer)["inputs"])
    assert all(word in copied for word in ("First", "second", "third", "fourth"))
    assert path.read_text(encoding="utf-8").count("\n") == 1
    cleaned(producer)


def test_old_argument_order_is_a_negative_control_for_variadic_parser(producer):
    fileset = StateFiles(producer.core.config, producer.core.policy, "session-one")
    with fileset.temporary_inputs(producer.worker.input_root) as folder:
        old_command = [
            str(producer.worker.executable),
            *producer.policy.worker_arguments,
            producer.policy.directory_option,
            str(folder),
            "# A synthetic prompt.",
        ]
        with pytest.raises(SetupError, match="exited 64"):
            run_worker(old_command, producer.worker, producer.policy)
    cleaned(producer)


def test_no_recorded_or_explicit_model_leaves_native_default_without_settings_lookup(
    producer, tmp_path
):
    producer.worker = producer.worker.model_copy(update={"model": None})
    settings = tmp_path / ".claude"
    settings.mkdir()
    (settings / "settings.json").write_text(
        '{"model":"project-model-not-to-load"}', encoding="utf-8"
    )
    result = producer.produce("session-one", transcript(tmp_path), "manual")
    assert result["model"] is None and "--model" not in report(producer)["arguments"]
    cleaned(producer)


@pytest.mark.parametrize(
    "action,body,error",
    [
        ("good", "", "empty"),
        ("good", "```bad```", "fenced"),
        ("good", "## 1. State\n![image]", "image"),
        ("good", "Only some prose", "headings"),
        ("error", None, "exited 7"),
        ("invalid_utf8", None, "UnicodeDecodeError"),
        ("overflow", None, "bound"),
        ("sleep", None, "timed out"),
    ],
)
def test_worker_failure_preserves_prior_snapshot_and_cleans_process_inputs(
    producer, tmp_path, action, body, error
):
    producer.core.stage("session-one", snapshot_text(producer.core, body="Prior preserved state"))
    before = state(producer.core)[1]
    behavior(producer, action, **({"body": body} if body is not None else {}))
    producer.policy = producer.policy.model_copy(update={"max_output_bytes": 4096})
    with pytest.raises(SetupError, match=error) as failure:
        producer.produce("session-one", transcript(tmp_path), "manual")
    assert "fixture-sensitive-output" not in str(failure.value)
    assert state(producer.core)[1] == before
    with pytest.raises(ProcessLookupError):
        os.kill(report(producer)["pid"], 0)
    cleaned(producer)


def test_applied_unread_snapshot_refuses_before_starting_worker(producer, tmp_path):
    producer.core.stage("session-one", snapshot_text(producer.core))
    producer.core.start("session-one", "compact")
    with pytest.raises(SetupError, match="no worker"):
        producer.produce("session-one", transcript(tmp_path), "manual")
    assert not (tmp_path / "report.json").exists()


def test_shared_filter_boundary_model_and_marked_correction(producer, tmp_path):
    rows = [
        {"type": "user", "message": {"content": "Old window"}},
        {"type": "system", "subtype": "compact_boundary"},
        {"type": "user", "isCompactSummary": True, "message": {"content": "Native summary"}},
        {"type": "user", "isMeta": True, "message": {"content": "Injected"}},
        {
            "type": "user",
            "message": {"content": [{"type": "tool_result", "content": "Secret tool"}]},
        },
        {"type": "user", "message": {"content": "Fresh question"}},
        {
            "type": "assistant",
            "message": {
                "model": "latest-model",
                "content": [
                    {"type": "thinking", "thinking": "Hidden thinking"},
                    {"type": "tool_use", "name": "DangerousTool"},
                    {
                        "type": "text",
                        "text": "mistake: my earlier claim was wrong; check before claiming.",
                    },
                ],
            },
        },
        {
            "type": "assistant",
            "isSidechain": True,
            "message": {
                "model": "sidechain-model",
                "content": [{"type": "text", "text": "Sidechain"}],
            },
        },
    ]
    result = producer.produce("session-one", transcript(tmp_path, rows), "auto")
    text = "\n".join(item["text"] for item in report(producer)["inputs"])
    assert "Fresh question" in text and "mistake:" in text
    assert all(
        excluded not in text
        for excluded in (
            "Old window",
            "Native summary",
            "Injected",
            "Secret tool",
            "Hidden thinking",
            "Sidechain",
        )
    )
    assert result["model"] == "latest-model" and result["dialogue_entries"] == 2


@pytest.mark.parametrize("kind", ["foreign", "invalid", "empty", "oversize", "symlink"])
def test_bad_transcript_never_starts_worker_or_creates_debt(producer, tmp_path, kind):
    path = transcript(tmp_path)
    if kind == "foreign":
        path = transcript(tmp_path, [{"sessionId": "foreign", "type": "user"}])
    elif kind == "invalid":
        path.write_text('{"broken":', encoding="utf-8")
    elif kind == "empty":
        path.write_text("", encoding="utf-8")
    elif kind == "oversize":
        producer.policy = producer.policy.model_copy(update={"max_transcript_bytes": 1})
    else:
        linked = tmp_path / "linked.jsonl"
        linked.symlink_to(path)
        path = linked
    with pytest.raises((SetupError, OSError)):
        producer.produce("session-one", path, "manual")
    assert not (tmp_path / "report.json").exists() and not producer.core.config.state_root.exists()


def test_actual_excerpt_writer_obeys_byte_bound_for_scientific_marker(producer, tmp_path):
    producer.policy = producer.policy.model_copy(
        update={"max_dialogue_bytes": 1000, "entry_cut_mark": "\n[cut {left:g} bytes]"}
    )
    path = transcript(tmp_path, [{"type": "user", "message": {"content": "x" * 999_968}}])
    producer.produce("session-one", path, "manual")
    payloads = [item["text"].split("\n\n", 1)[1] for item in report(producer)["inputs"]]
    assert all(0 < len(body.encode("utf-8")) <= 1000 for body in payloads)
    cleaned(producer)


@pytest.mark.parametrize(
    "mark", ["{left:g}", "{left:,}", "{left!r}", "{left:>9}", "{{left}} {left}"]
)
def test_utf8_format_grid(mark):
    cut_mark(mark, 1000)
    for character in ("x", "\u00e9", "\U0001f600"):
        for size in (999, 1000, 1001, 1_000_000):
            body = fit_entry(character * size, 1000, mark)
            assert len(body.encode("utf-8")) <= 1000 and "\ufffd" not in body
    chunks = chunk_entries(["old" + "a" * 600, "new" + "b" * 600], 1000, 1, mark)
    assert len(chunks) == 1 and chunks[0].startswith("new")


@pytest.mark.parametrize(
    "mark", ["{{left}}", "{left} {}", "{left:q}", "{left.real}", "{left}" * 1000]
)
def test_unusable_mark_refused(mark):
    with pytest.raises(SetupError, match="entry_cut_mark"):
        cut_mark(mark, 1000)


def test_cli_precompute_is_actual_child_command_without_native_hooks(producer, tmp_path):
    config = tmp_path / "worker.json"
    config.write_text(producer.worker.model_dump_json(), encoding="utf-8")
    command = [
        sys.executable,
        "-m",
        "anamnema",
        "continuity",
        "precompute",
        "--continuity-config",
        str(tmp_path / "core-config.json"),
        "--session",
        "session-one",
        "--transcript",
        str(transcript(tmp_path)),
        "--trigger",
        "manual",
        "--worker-config",
        str(config),
    ]
    result = subprocess.run(  # noqa: S603 - own Python entry point and synthetic fixture paths
        command,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=15,
        cwd=tmp_path,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["snapshot"]["applied"] is False
    assert "continuity SUCCESS" in result.stderr
    assert state(producer.core)[1].snapshot is not None
    cleaned(producer)


@pytest.mark.parametrize(
    "extra",
    [
        ["--resume", "some-session"],
        ["--tools", "Bash"],
        ["--effort"],
        ["--settings", '{"disableAllHooks":false}'],
    ],
)
def test_policy_refuses_unsafe_duplicate_or_incomplete_worker_arguments(extra):
    policy = producer_policy()
    raw = policy.model_dump(mode="json")
    if extra == ["--effort"]:
        index = raw["worker_arguments"].index("--effort")
        del raw["worker_arguments"][index : index + 2]
    raw["worker_arguments"] += extra
    with pytest.raises(ValidationError):
        type(policy).model_validate_json(json.dumps(raw))


def test_production_json_policy_roundtrip_is_a_positive_validation_control():
    policy = producer_policy()
    assert type(policy).model_validate_json(policy.model_dump_json()) == policy


@pytest.mark.parametrize(
    "flag,value",
    [
        ("--permission-mode", "bypassPermissions"),
        ("--tools", "Bash"),
        ("--setting-sources", "project"),
        ("--settings", '{"disableAllHooks":false}'),
    ],
)
def test_security_values_are_checked_through_actual_json_policy_loader(flag, value):
    policy = producer_policy()
    raw = policy.model_dump(mode="json")
    raw["worker_arguments"][raw["worker_arguments"].index(flag) + 1] = value
    with pytest.raises(ValidationError):
        type(policy).model_validate_json(json.dumps(raw))


def test_worker_input_root_cannot_overlap_managed_state(producer, tmp_path):
    producer.worker = producer.worker.model_copy(
        update={"input_root": producer.core.config.state_root}
    )
    with pytest.raises(SetupError, match="outside managed state"):
        producer.produce("session-one", transcript(tmp_path), "manual")
    assert not (tmp_path / "report.json").exists()


def test_worker_start_race_and_header_bound_preserve_previous_state(producer, tmp_path):
    producer.core.stage("session-one", snapshot_text(producer.core))
    before = state(producer.core)[1]
    path = transcript(tmp_path)
    producer.worker.executable.unlink()
    with pytest.raises(SetupError, match="cannot start"):
        producer.produce("session-one", path, "manual")
    assert state(producer.core)[1] == before
    cleaned(producer)
    producer.policy = producer.policy.model_copy(update={"max_header_bytes": 1})
    with pytest.raises(SetupError, match="header"):
        producer.produce("session-one", path, "manual")
    assert state(producer.core)[1] == before
    cleaned(producer)


@pytest.mark.parametrize("change", [None, "event", "cwd", "session", "trigger", "transcript"])
def test_actual_precompact_cli_translates_only_valid_stdin(producer, tmp_path, change):
    config = tmp_path / "worker.json"
    config.write_text(producer.worker.model_dump_json(), encoding="utf-8")
    payload = {
        "hook_event_name": "PreCompact",
        "session_id": "session-one",
        "transcript_path": str(transcript(tmp_path)),
        "trigger": "auto",
        "cwd": str(tmp_path),
    }
    if change == "event":
        payload["hook_event_name"] = "Stop"
    elif change == "cwd":
        payload["cwd"] = str(tmp_path.parent)
    elif change == "session":
        payload["session_id"] = "../escape"
    elif change == "trigger":
        payload["trigger"] = "unsupported"
    elif change == "transcript":
        payload["transcript_path"] = "relative.jsonl"
    command = [
        sys.executable,
        "-m",
        "anamnema",
        "continuity",
        "precompact",
        "--continuity-config",
        str(tmp_path / "core-config.json"),
        "--worker-config",
        str(config),
    ]
    result = subprocess.run(  # noqa: S603 - own Python entry point and synthetic fixture paths
        command,
        input=json.dumps(payload),
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=15,
        cwd=tmp_path,
    )
    if change is None:
        assert result.returncode == 0 and json.loads(result.stdout) == {}, result.stderr
        assert state(producer.core)[1].snapshot is not None
    else:
        assert result.returncode == 2 and "continuity ERROR" in result.stderr
        assert not (tmp_path / "report.json").exists()
        assert not producer.core.config.state_root.exists()
    cleaned(producer)
