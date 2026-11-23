"""Synthetic native HTTP inference contracts, not real model quality/performance."""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class SearchHTTP:
    def __init__(self):
        self.calls = []
        self.override = {}
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                if self.headers.get("Authorization") != "Bearer fixture-search-token":
                    self.send_error(401)
                    return
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                owner.calls.append((self.path, body))
                if self.path in owner.override:
                    result = owner.override[self.path]
                elif self.path == "/tokenize":
                    tokens = [ord(c) for c in body["content"]]
                    result = {"tokens": [1, *tokens, 2] if body["add_special"] else tokens}
                elif self.path == "/detokenize":
                    result = {"content": "".join(chr(i) for i in body["tokens"])}
                elif self.path == "/v1/embeddings":
                    result = {
                        "data": [
                            {"index": i, "embedding": owner.vector(text)}
                            for i, text in reversed(list(enumerate(body["input"])))
                        ]
                    }
                elif self.path == "/v1/rerank":
                    result = {
                        "results": [
                            {"index": i, "relevance_score": float(i)}
                            for i in reversed(range(len(body["documents"])))
                        ]
                    }
                else:
                    self.send_error(404)
                    return
                data = json.dumps(result).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *args):
                return

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @staticmethod
    def vector(text):
        return [1.0, 0.0, 0.0, 0.0] if "fourteen" in text else [0.8, 0.6, 0.0, 0.0]

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *_):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
