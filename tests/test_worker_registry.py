"""Launch records, the launcher and the sweep, run against real processes."""

import json
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest
from pydantic import ValidationError

from anamnema.continuity_config import continuity_policy
from anamnema.setup_config import SetupError
from anamnema.substrates.claude_hooks import continuity_event
from anamnema.worker_launcher import PARENT_REFUSED, main, parse
from anamnema.worker_registry import (
    RegistryConfig,
    add_worker,
    boot_id,
    launcher_argv,
    register,
    release,
    start_time,
)
from anamnema.worker_sweep import sweep

SLEEP = "/bin/sleep"


@pytest.fixture
def config(tmp_path):
    root = tmp_path / "inputs"
    root.mkdir()
    return RegistryConfig(records_dir=tmp_path / "records", folder_roots=(root,))


def records(config):
    return sorted(p.name for p in config.records_dir.glob("*.json"))


def gone(pid, seconds=5.0):
    """True once the process no longer runs (reaped or a zombie)."""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        try:
            state = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0]
        except OSError:
            return True
        if state == "Z":
            return True
        time.sleep(0.02)
    return False


def marked(marker):
    """Pids whose command line carries a unique marker."""
    found = []
    for entry in Path("/proc").iterdir():
        if entry.name.isdigit():
            try:
                if marker in entry.joinpath("cmdline").read_bytes().decode(errors="replace"):
                    found.append(int(entry.name))
            except OSError:
                continue
    return found


def fake_record(config, folders, **fields):
    config.records_dir.mkdir(mode=0o700, exist_ok=True)
    record = {
        "uid": os.getuid(),
        "boot_id": boot_id(),
        "parent_pid": 1,
        "parent_start": -1,
        "folders": [str(f) for f in folders],
        "pid": None,
        "start": None,
        **fields,
    }
    path = config.records_dir / f"fake-{len(records(config))}.json"
    path.write_text(json.dumps(record))
    path.chmod(0o600)
    return path


def test_config_paths_are_explicit_and_records_stay_out_of_the_roots(tmp_path):
    root = tmp_path / "inputs"
    with pytest.raises(ValidationError, match="absolute"):
        RegistryConfig(records_dir=Path("records"), folder_roots=(root,))
    with pytest.raises(ValidationError, match="inside a swept"):
        RegistryConfig(records_dir=root / "records", folder_roots=(root,))
    with pytest.raises(ValidationError):
        RegistryConfig(records_dir=tmp_path / "r", folder_roots=(root,), max_record_bytes=1 << 20)
    loaded = RegistryConfig.model_validate_json(
        json.dumps({"records_dir": str(tmp_path / "r"), "folder_roots": [str(root)]})
    )
    assert (loaded.max_record_bytes, loaded.max_records_scanned) == (16384, 256)


def test_register_writes_a_private_unique_intent_before_any_worker(config):
    folder = config.folder_roots[0] / "excerpt"
    first, second = register(config, [folder]), register(config, [folder])
    assert first != second and first.name.startswith(f"{os.getpid()}-{start_time(os.getpid())}-")
    assert oct(first.stat().st_mode & 0o777) == "0o600"
    assert oct(config.records_dir.stat().st_mode & 0o777) == "0o700"
    data = json.loads(first.read_text())
    assert data["folders"] == [str(folder)] and data["pid"] is None and data["start"] is None
    release(config, first)
    assert records(config) == [second.name]


def test_register_refuses_folders_outside_the_roots(config, tmp_path):
    with pytest.raises(SetupError, match="strictly inside"):
        register(config, [tmp_path / "elsewhere"])
    with pytest.raises(SetupError, match="strictly inside"):
        register(config, [config.folder_roots[0]])
    assert register(config, []).exists()


def test_the_launcher_names_itself_and_becomes_the_worker(config):
    record = register(config, [])
    process = subprocess.Popen([*launcher_argv(record), SLEEP, "30"], start_new_session=True)  # noqa: S603 - fixed test argv
    try:
        deadline = time.monotonic() + 10
        while json.loads(record.read_text())["pid"] is None and time.monotonic() < deadline:
            time.sleep(0.02)
        data = json.loads(record.read_text())
        assert data["pid"] == process.pid and data["start"] == start_time(process.pid)
        assert Path(f"/proc/{process.pid}/exe").resolve() == Path(SLEEP).resolve()
    finally:
        os.killpg(process.pid, signal.SIGKILL)
        process.wait()
    with pytest.raises(SetupError, match="already names"):
        add_worker(record, 1, 1)


def test_the_launcher_refuses_a_parent_it_was_not_started_by(config):
    record = register(config, [])
    assert main(
        ["--record", str(record), "--parent-pid", "1", "--parent-start", "1", "--", SLEEP]
    ) == (PARENT_REFUSED)
    assert json.loads(record.read_text())["pid"] is None
    with pytest.raises(SystemExit):
        parse(["--record", str(record), "--parent-pid", "1", "--parent-start", "1", "--"])


PARENT = """
import json, os, signal, subprocess, sys
from pathlib import Path
from anamnema.worker_registry import RegistryConfig, launcher_argv, register
config = RegistryConfig.model_validate_json(sys.argv[1])
record = register(config, [Path(sys.argv[2])])
Path(sys.argv[2]).mkdir()
process = subprocess.Popen([*launcher_argv(record), "/bin/sleep", sys.argv[3]],
                           start_new_session=True)
if sys.argv[4] == "registered":
    while json.loads(record.read_text())["pid"] is None:
        pass
os.write(1, str(process.pid).encode())
os.kill(os.getpid(), signal.SIGKILL)
"""


@pytest.mark.parametrize("moment", ["before-registration", "registered"])
def test_a_killed_parent_leaves_no_worker_and_the_sweep_clears_its_folder(config, moment):
    marker = f"{300 + os.getpid() % 97}.{time.time_ns() % 10**6}"
    folder = config.folder_roots[0] / f"excerpt-{moment}"
    parent = subprocess.run(  # noqa: S603 - fixed test argv
        [sys.executable, "-c", PARENT, config.model_dump_json(), str(folder), marker, moment],
        capture_output=True,
        timeout=30,
        check=False,
    )
    assert parent.returncode == -signal.SIGKILL
    launcher = int(parent.stdout)
    assert gone(launcher)
    assert marked(marker) == []
    counts = sweep(config)
    assert counts.cleared == 1 and counts.folders == 1 and counts.killed == 0
    assert not folder.exists() and records(config) == []


def test_two_threads_launch_and_release_their_own_records(config):
    seen = {}

    def launch(name):
        folder = config.folder_roots[0] / name
        folder.mkdir()
        record = register(config, [folder])
        process = subprocess.Popen([*launcher_argv(record), SLEEP, "0.3"], start_new_session=True)  # noqa: S603 - fixed test argv
        process.wait()
        seen[name] = (record, json.loads(record.read_text())["pid"], process.pid)
        folder.rmdir()
        release(config, record)

    threads = [threading.Thread(target=launch, args=(n,)) for n in ("a", "b")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert seen["a"][0] != seen["b"][0]
    assert all(pid == launched for _, pid, launched in seen.values())
    assert records(config) == []


def test_a_live_parent_is_left_alone(config):
    path = register(config, [config.folder_roots[0] / "busy"])
    assert sweep(config).live == 1 and path.exists()


def test_an_orphan_worker_is_killed_but_a_reused_pid_or_another_boot_never(config):
    worker = subprocess.Popen([SLEEP, "30"], start_new_session=True)  # noqa: S603 - fixed test argv
    try:
        fake_record(config, [], pid=worker.pid, start=start_time(worker.pid) + 1)
        fake_record(config, [], pid=worker.pid, start=start_time(worker.pid), boot_id="old")
        assert sweep(config).killed == 0 and worker.poll() is None
        fake_record(config, [], pid=worker.pid, start=start_time(worker.pid))
        counts = sweep(config)
        assert counts.killed == 1 and counts.cleared == 1
        assert worker.wait(timeout=5) == -signal.SIGKILL
    finally:
        if worker.poll() is None:
            worker.kill()
            worker.wait()


def test_only_real_folders_strictly_inside_a_root_are_removed(config, tmp_path):
    root = config.folder_roots[0]
    outside = tmp_path / "outside"
    outside.mkdir()
    (root / "link").symlink_to(outside)
    (root / "real").mkdir()
    fake_record(config, [root / "real", root / "link", outside, root, root / "missing"])
    counts = sweep(config)
    assert counts.folders == 1 and counts.kept_outside == 3
    assert not (root / "real").exists() and outside.exists() and root.exists()


def test_foreign_oversized_or_malformed_records_stay_and_are_counted(config):
    public = fake_record(config, [])
    public.chmod(0o644)
    big = fake_record(config, [], padding="x" * config.max_record_bytes)
    wrong = fake_record(config, [], parent_pid="not-a-pid")
    counts = sweep(config)
    assert counts.unsafe == 3 and counts.cleared == 0
    assert public.exists() and big.exists() and wrong.exists()


def test_a_scan_is_bounded_and_a_missing_directory_is_nothing_to_do(config, tmp_path):
    assert sweep(config) == sweep(config)
    bounded = config.model_copy(update={"max_records_scanned": 1})
    fake_record(bounded, [])
    fake_record(bounded, [])
    counts = sweep(bounded)
    assert counts.cleared == 1 and counts.unscanned == 1


def test_session_start_sweeps_only_with_a_registry(config):
    policy = continuity_policy()
    fields, events = policy.native_fields, policy.native_events
    folder = config.folder_roots[0] / "left"
    folder.mkdir()
    fake_record(config, [folder])
    start = {fields["event"]: events["start"], fields["session"]: "s", fields["source"]: "compact"}
    continuity_event(start, "start", policy)
    assert folder.exists()
    continuity_event(start, "start", policy, registry=config)
    assert not folder.exists() and records(config) == []


def launch_args(record, *command):
    parent = os.getppid()
    return [
        "--record",
        str(record),
        "--parent-pid",
        str(parent),
        "--parent-start",
        str(start_time(parent)),
        "--",
        *command,
    ]


def test_launcher_exits_without_a_worker_at_each_refusal(config, monkeypatch):
    import anamnema.worker_launcher as launcher

    record = register(config, [])
    monkeypatch.setattr(launcher, "die_with_parent", lambda: False)
    assert main(launch_args(record, SLEEP)) == PARENT_REFUSED
    monkeypatch.setattr(launcher, "die_with_parent", lambda: True)
    checks = iter([True, False])
    monkeypatch.setattr(launcher, "expected_parent", lambda pid, start: next(checks))
    assert main(launch_args(record, SLEEP)) == PARENT_REFUSED
    monkeypatch.undo()
    monkeypatch.setattr(launcher, "die_with_parent", lambda: True)
    assert main(launch_args(config.records_dir / "missing.json", SLEEP)) == launcher.RECORD_REFUSED
    monkeypatch.setattr(
        launcher, "start_time", lambda pid: None if pid == os.getpid() else start_time(pid)
    )
    assert main(launch_args(record, SLEEP)) == launcher.RECORD_REFUSED
    monkeypatch.undo()
    monkeypatch.setattr(launcher, "die_with_parent", lambda: True)
    assert main(launch_args(record, str(config.records_dir / "no-such-worker"))) == (
        launcher.EXEC_FAILED
    )
    assert json.loads(record.read_text())["pid"] == os.getpid()


def test_the_death_signal_is_set_on_the_launcher_itself():
    probe = "from anamnema.worker_launcher import die_with_parent; print(die_with_parent())"
    done = subprocess.run([sys.executable, "-I", "-c", probe], capture_output=True, text=True)  # noqa: S603 - fixed test argv
    assert done.stdout.strip() == "True"


def test_a_held_lock_times_out_and_an_oversized_record_is_refused(config, tmp_path):
    from anamnema.private_files import PrivateStateBusy
    from anamnema.worker_registry import locked

    quick = config.model_copy(update={"lock_wait_seconds": 0.05})
    register(quick, [])
    with locked(quick.records_dir, 1.0):
        with pytest.raises(PrivateStateBusy):
            register(quick, [])
        assert sweep(quick).cleared == 0
    many = [quick.folder_roots[0] / ("f" * 200 + str(n)) for n in range(100)]
    with pytest.raises(SetupError, match="exceeds"):
        register(quick, many)


def test_a_record_of_another_user_is_never_acted_on(config):
    folder = config.folder_roots[0] / "theirs"
    folder.mkdir()
    fake_record(config, [folder], uid=os.getuid() + 1)
    assert sweep(config).unsafe == 1 and folder.exists()


def test_a_parent_step_never_reaches_a_sibling_of_the_root(config, tmp_path):
    sibling = tmp_path / "outside"
    sibling.mkdir()
    escape = config.folder_roots[0] / ".." / "outside"
    with pytest.raises(ValidationError, match="parent"):
        RegistryConfig(records_dir=tmp_path / "r", folder_roots=(tmp_path / "a" / ".." / "b",))
    with pytest.raises(SetupError, match="strictly inside"):
        register(config, [escape])
    fake_record(config, [escape])
    counts = sweep(config)
    assert counts.kept_outside == 1 and counts.cleared == 1 and sibling.exists()


def test_a_root_replaced_by_a_link_is_never_followed(tmp_path):
    victim = tmp_path / "elsewhere"
    (victim / "victim").mkdir(parents=True)
    linked = tmp_path / "linked-root"
    linked.symlink_to(victim)
    config = RegistryConfig(records_dir=tmp_path / "records", folder_roots=(linked,))
    fake_record(config, [linked / "victim"])
    counts = sweep(config)
    assert counts.kept_outside == 1 and counts.folders == 0
    assert (victim / "victim").exists()


def test_a_folder_that_cannot_be_removed_keeps_its_record(config, monkeypatch):
    import anamnema.worker_sweep as worker_sweep

    folder = config.folder_roots[0] / "stuck"
    folder.mkdir()
    path = fake_record(config, [folder])
    monkeypatch.setattr(worker_sweep.shutil, "rmtree", lambda *a, **k: None)
    counts = sweep(config)
    assert counts.failed == 1 and counts.cleared == 0 and counts.folders == 0
    assert folder.exists() and path.exists()
    monkeypatch.undo()
    counts = sweep(config)
    assert counts.cleared == 1 and counts.folders == 1 and not folder.exists()
