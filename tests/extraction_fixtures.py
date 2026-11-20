"""Synthetic HTTP llama.cpp protocol fixture; never a real inference endpoint."""

import json
import re
import threading
import time
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


class ModelService:
    def __init__(self, path: Path):
        self.model_file = path
        self.context = 8192
        self.token_count = 64
        self.requests = []
        self.headers = []
        self.chat_replies = deque()
        self.overrides = {}
        self.chat_delay = 0
        self.after_chat = None
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                return

            def do_GET(self):
                self.answer(None)

            def do_POST(self):
                self.answer(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))

            def answer(self, body):
                owner.headers.append(dict(self.headers))
                owner.requests.append((self.command, self.path, body))
                status, response = owner.response(self.path, body)
                raw = response if isinstance(response, bytes) else json.dumps(response).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                try:
                    self.wfile.write(raw)
                except BrokenPipeError:
                    # A timeout test intentionally closes its client before this response.
                    owner.client_closed = True

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever)

    def response(self, path, body):
        if path in self.overrides:
            return self.overrides[path]
        if path == "/props":
            return 200, {
                "model_path": str(self.model_file),
                "model_alias": "fixture-model",
                "build_info": "fixture-build",
                "default_generation_settings": {"n_ctx": self.context},
            }
        if path == "/apply-template":
            return 200, {"prompt": body["messages"][0]["content"]}
        if path == "/tokenize":
            return 200, {"tokens": [1] * self.token_count}
        if path == "/v1/chat/completions":
            time.sleep(self.chat_delay)
            if self.after_chat:
                self.after_chat()
            if self.chat_replies:
                return self.chat_replies.popleft()
            number = int(re.findall(r"\[#(\d+) ", body["messages"][0]["content"])[0])
            answer = {
                "concepts": [
                    {
                        "text": "The project keeps original conversation sources.",
                        "rows": [number],
                        "kind": "fact",
                        "ruled_by": 0,
                    }
                ]
            }
            return completion(json.dumps(answer))
        return 404, {"error": "fixture has no route"}

    @property
    def port(self):
        return self.server.server_address[1]

    @property
    def calls(self):
        return [body for _, path, body in self.requests if path == "/v1/chat/completions"]

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *args):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        assert not self.thread.is_alive()


def completion(content, finish="stop"):
    return 200, {"choices": [{"finish_reason": finish, "message": {"content": content}}]}
