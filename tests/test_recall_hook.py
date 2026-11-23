"""The prompt hook adds bounded source-linked notes, skips what asks for no recall, never blocks."""

import argparse
import io
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any

import pytest

from anamnema import recall_hook
from anamnema.recall_hook import (
    RecallDeployment,
    recall_binding,
    recall_policy,
    render,
    run_recall,
)
from anamnema.setup_config import SetupError

NOTES = {
    "notes": [
        {
            "id": "n1",
            "text": "Andrea  set up\nthe cluster.",
            "kind": "fact",
            "said_at": "2026-11-01",
        },
        {"id": "n2", "text": "Old port.", "kind": "fact", "status": "outdated", "said_at": None},
    ]
}


def _deployment(tmp_path: Path) -> RecallDeployment:
    return RecallDeployment(instance="aura", cwd=tmp_path, config=tmp_path / "setup.json")


def _payload(tmp_path: Path, prompt: str, **extra: Any) -> io.StringIO:
    data = {"hook_event_name": "UserPromptSubmit", "prompt": prompt, "cwd": str(tmp_path), **extra}
    return io.StringIO(json.dumps(data))


@pytest.fixture
def bearer(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(recall_hook, "_bearer", lambda _d, _s: "fixture-bearer")


def test_notes_become_bounded_context_with_kind_date_and_id(tmp_path: Path, bearer: None) -> None:
    asked: list[dict[str, Any]] = []

    def ask(url: str, body: dict[str, Any], token: str, _policy: Any) -> dict[str, Any]:
        asked.append({"url": url, "body": body, "token": token})
        return NOTES

    out = run_recall(
        _payload(tmp_path, "Who set up the cluster?"), _deployment(tmp_path), recall_policy(), ask
    )
    context = out["hookSpecificOutput"]["additionalContext"]
    assert out["hookSpecificOutput"]["hookEventName"] == "UserPromptSubmit"
    assert "- [fact] Andrea set up the cluster. (2026-11-01) [n1]" in context
    assert "- [fact, outdated] Old port. (date unknown) [n2]" in context
    assert asked == [
        {
            "url": "http://127.0.0.1:8095/v1/search",
            "body": {"question": "Who set up the cluster?"},
            "token": "fixture-bearer",
        }
    ]


@pytest.mark.parametrize(
    ("prompt", "cwd_ok"),
    [("/compact", True), ("ok", True), ("A real question about the cluster?", False)],
)
def test_commands_short_prompts_and_other_projects_ask_nothing(
    tmp_path: Path, bearer: None, prompt: str, cwd_ok: bool
) -> None:
    def ask(*_args: Any) -> dict[str, Any]:
        raise AssertionError("no search expected")

    stream = _payload(tmp_path, prompt) if cwd_ok else _payload(tmp_path, prompt, cwd="/elsewhere")
    assert run_recall(stream, _deployment(tmp_path), recall_policy(), ask) == {}


def test_an_outage_is_a_visible_notice_never_a_block(tmp_path: Path, bearer: None) -> None:
    def ask(*_args: Any) -> dict[str, Any]:
        raise SetupError("memory service unreachable (URLError)")

    out = run_recall(
        _payload(tmp_path, "Who set up the cluster?"), _deployment(tmp_path), recall_policy(), ask
    )
    assert out == {"systemMessage": "Memory recall skipped: memory service unreachable (URLError)"}
    wrong_event = io.StringIO(json.dumps({"hook_event_name": "Stop", "prompt": "x" * 40}))
    assert "decision" not in run_recall(wrong_event, _deployment(tmp_path), recall_policy())


def test_context_stops_at_its_byte_bound(tmp_path: Path) -> None:
    policy = recall_policy().model_copy(
        update={"max_context_bytes": 300, "max_note_characters": 50}
    )
    many = {"notes": [{"id": f"n{i}", "text": "word " * 40, "kind": "fact"} for i in range(20)]}
    context = render(many, policy)
    assert len(context.encode("utf-8")) <= 300 and context.count("\n- [fact]") >= 1
    assert render({"notes": []}, policy) == ""


def test_the_real_http_path_sends_the_bearer_and_reads_the_answer(tmp_path: Path) -> None:
    seen: dict[str, Any] = {}

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802 - http.server protocol name
            seen["auth"] = self.headers["Authorization"]
            seen["body"] = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            data = json.dumps(NOTES).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *_args: Any) -> None:
            return

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        url = f"http://127.0.0.1:{server.server_port}/v1/search"
        answer = recall_hook.fetch(url, {"question": "q"}, "secret-x", recall_policy())
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
    assert answer == NOTES
    assert seen == {"auth": "Bearer secret-x", "body": {"question": "q"}}
    with pytest.raises(SetupError, match="unreachable"):
        recall_hook.fetch(url, {"question": "q"}, "secret-x", recall_policy())


def test_the_command_reports_a_bad_deployment_without_raising(tmp_path: Path) -> None:
    parser = argparse.ArgumentParser()
    recall_hook.add_recall_hook(parser)
    args = parser.parse_args(["--deployment", str(tmp_path / "missing.json")])
    out = recall_hook.run_recall_hook(args, io.StringIO("{}"))
    assert out["systemMessage"].startswith("Memory recall skipped: invalid recall deployment")


def test_binding_uses_the_policy_timeout_and_absolute_paths(tmp_path: Path) -> None:
    binding = recall_binding(tmp_path / "anamnema", tmp_path / "recall.json", recall_policy())
    assert binding.event == "UserPromptSubmit"
    assert binding.timeout_seconds == recall_policy().hook_timeout_seconds
    assert binding.command[1] == "recall-hook"
    with pytest.raises(SetupError, match="absolute"):
        recall_binding(Path("anamnema"), tmp_path / "recall.json", recall_policy())
    with pytest.raises(ValueError, match="before the hook timeout"):
        recall_policy().model_validate(
            {**recall_policy().model_dump(), "request_timeout_seconds": 20.0}
        )
