"""Process stub for llama-server used by lifecycle tests (no model, no Metal).

Implements /health, /tokenize and /v1/chat/completions on 127.0.0.1.
Appends ``load``/``unload`` events with timestamps to $STUB_TIMELINE so tests
can prove that heavy-model intervals never overlap.  Behaviour knobs:
STUB_LOAD_SECONDS (startup delay), STUB_REPLY (fixed reply text),
STUB_FINISH (finish_reason), STUB_EXIT_AFTER_LOAD (crash after load).
"""

from __future__ import annotations

import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
import signal
import sys
import time


def record(event: str) -> None:
    path = os.environ.get("STUB_TIMELINE")
    if path:
        with open(path, "a", encoding="utf-8") as stream:
            stream.write(json.dumps({"event": event, "pid": os.getpid(), "t": time.time(), "who": "llm"}) + "\n")


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_args) -> None:
        return

    def _send(self, status: int, payload: dict) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        self._send(200, {"status": "ok"})

    def do_POST(self) -> None:
        if self.headers.get("Authorization") != f"Bearer {os.environ.get('LLAMA_API_KEY')}":
            self._send(401, {"error": "unauthorized"})
            return
        request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        if self.path == "/tokenize":
            self._send(200, {"tokens": list(range(len(request["content"]) // 3))})
            return
        prompt = request["messages"][-1]["content"]
        reply = os.environ.get("STUB_REPLY")
        if reply is None:
            # Echo UNIT markers so semantic contract checks can run.
            reply = prompt.rsplit("\n\n", 1)[-1]
        self._send(200, {
            "choices": [{"finish_reason": os.environ.get("STUB_FINISH", "stop"),
                         "message": {"role": "assistant", "content": reply}}],
            "usage": {"prompt_tokens": len(prompt) // 3, "completion_tokens": len(reply) // 3},
            "timings": {"predicted_ms": 10.0, "predicted_per_second": 100.0},
        })


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, required=True)
    args, _unknown = parser.parse_known_args()
    time.sleep(float(os.environ.get("STUB_LOAD_SECONDS", "0")))
    record("load")
    if os.environ.get("STUB_EXIT_AFTER_LOAD"):
        return 3

    def stop(_signum, _frame) -> None:
        record("unload")
        sys.exit(0)

    signal.signal(signal.SIGTERM, stop)
    ThreadingHTTPServer(("127.0.0.1", args.port), Handler).serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
