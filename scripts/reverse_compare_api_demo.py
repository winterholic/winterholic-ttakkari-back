"""Deliberately local-only sample API used by reverse_compare_api.py."""
from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import ClassVar


class DemoHandler(BaseHTTPRequestHandler):
    state: ClassVar[dict] = {"count": 0}

    def do_GET(self):
        if self.path == "/state":
            self._send(200, self.state)
        elif self.path == "/large":
            self._send(200, {"payload": "x" * 8192})
        else:
            self._send(404, {"error": "not_found"})

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length)
        try:
            value = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError):
            self._send(400, {"error": "invalid_json"})
            return
        if not isinstance(value, dict) or "value" not in value:
            self._send(422, {"error": "value_required"})
            return
        self.state["count"] += 1
        self._send(201, {"accepted": True, "value": value["value"], "count": self.state["count"]})

    def _send(self, status: int, value: dict):
        body = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args):
        pass


def serve(port: int = 0) -> ThreadingHTTPServer:
    handler = type("InstanceDemoHandler", (DemoHandler,), {"state": {"count": 0}})
    return ThreadingHTTPServer(("127.0.0.1", port), handler)
