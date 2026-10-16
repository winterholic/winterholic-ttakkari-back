#!/usr/bin/env python3
"""llama-server 흉내: /health 와 /v1/chat/completions 만. 시험이 자식 프로세스 수명주기를 확인하는 데 쓴다.

모델 경로 옆의 `<model>.behavior`(JSON)로 동작을 바꾼다: delay(초 뒤에 health 200), exit_after(초 뒤 스스로 종료), fail_start(바로 종료).
"""
import json
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

args = sys.argv[1:]


def arg(name, default=None):
    return args[args.index(name) + 1] if name in args else default


model, port, host = arg("--model"), int(arg("--port")), arg("--host", "127.0.0.1")
behavior = {}
try:
    behavior = json.loads(Path(model + ".behavior").read_text())
except (OSError, ValueError):
    pass
if behavior.get("fail_start"):
    sys.exit(3)
started = time.monotonic()
if behavior.get("exit_after"):
    threading.Timer(float(behavior["exit_after"]), lambda: __import__("os")._exit(7)).start()


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/health":
            if time.monotonic() - started < float(behavior.get("delay", 0)):
                return self._send(503, {"error": "loading"})
            return self._send(200, {"status": "ok"})
        self._send(404, {})

    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length", 0)))
        with open(model + ".posts", "a") as f:
            f.write("x\n")
        self._send(200, {"choices": [{"message": {"content": json.dumps({"kind": "work", "evidence": ""})}}]})


ThreadingHTTPServer((host, port), H).serve_forever()
