"""The real foreground hook translator must fit its declared native timeout with margin."""

import json
import tempfile
import time
from pathlib import Path

from anamnema.capture_events import request_from_event
from anamnema.capture_queue_config import CaptureDeployment, capture_queue_policy
from anamnema.setup_config import load_config


def test_large_native_transcript_enqueue_fits_configured_hook_budget():
    with tempfile.TemporaryDirectory(prefix="anamnema-hook-latency-") as folder:
        root = Path(folder)
        config = root / "setup.json"
        config.write_text(load_config().model_dump_json(), encoding="utf-8")
        transcript = root / "synthetic.jsonl"
        historical = {
            "type": "assistant",
            "sessionId": "synthetic",
            "uuid": "historical",
            "timestamp": "2026-01-01T00:00:00Z",
            "message": {
                "content": [
                    {
                        "type": "tool_use",
                        "name": "Read",
                        "id": "synthetic-tool",
                        "input": {"payload": "x" * 8192},
                    }
                ]
            },
        }
        encoded = json.dumps(historical, separators=(",", ":")).encode("utf-8") + b"\n"
        with transcript.open("wb") as stream:
            for _ in range((300 * 1024 * 1024) // len(encoded)):
                stream.write(encoded)
            for role, text in (
                ("user", "Current synthetic turn."),
                ("assistant", "fact: A synthetic latency canary."),
            ):
                stream.write(
                    json.dumps(
                        {
                            "type": role,
                            "sessionId": "synthetic",
                            "uuid": role,
                            "timestamp": "2026-01-01T01:00:00Z",
                            "message": {"content": [{"type": "text", "text": text}]},
                        }
                    ).encode("utf-8")
                    + b"\n"
                )
        assert transcript.stat().st_size > 299 * 1024 * 1024
        deployment = CaptureDeployment(
            instance="synthetic",
            cwd=root,
            state_root=root / "queue",
            transcript_roots=(root,),
            config=config,
        )
        policy = capture_queue_policy()
        payload = {
            "session_id": "synthetic",
            "transcript_path": str(transcript),
            "cwd": str(root),
            "last_assistant_message": "fact: A synthetic latency canary.",
        }
        for event in ("stop", "precompact", "end"):
            start = time.perf_counter()
            job = request_from_event(
                {**payload, "hook_event_name": policy.native_events[event]},
                event,
                deployment,
                policy,
            )
            elapsed = time.perf_counter() - start
            assert job is not None
            assert elapsed < policy.hook_timeout_seconds * policy.hook_budget_fraction, (
                event,
                elapsed,
            )
        assert not deployment.state_root.exists()
    assert not root.exists()
