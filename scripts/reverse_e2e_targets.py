#!/usr/bin/env python3
"""Start disposable, loopback-only API and web originals for platform E2E."""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import secrets
import signal
import socket
import socketserver
import subprocess
import sys
import threading
import time
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import ClassVar

SCRIPT = Path(__file__).resolve()
API_DEMO = SCRIPT.with_name("reverse_compare_api_demo.py")
WEB_HTML = r'''<!doctype html>
<html lang="ko">
<head>
  <meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
  <title>주문 준비 | Reverse E2E 원본</title><link rel="icon" href="data:,">
  <style>
    :root{color-scheme:light;--ink:#172b3a;--muted:#627482;--line:#d6e0e6;--blue:#145b73;--pale:#eaf4f6;--green:#216b50}
    *{box-sizing:border-box}body{margin:0;background:#f3f6f7;color:var(--ink);font:16px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}
    main{width:min(100% - 32px,720px);margin:40px auto}.eyebrow{color:var(--blue);font-size:.78rem;font-weight:750;letter-spacing:.1em;text-transform:uppercase}
    h1{font-size:clamp(1.8rem,6vw,2.5rem);line-height:1.15;margin:.55rem 0}.intro{color:var(--muted);margin:0 0 24px}
    .panel{background:white;border:1px solid var(--line);border-radius:16px;padding:24px;box-shadow:0 8px 24px #172b3a0c}
    label{display:block;font-weight:700;margin:0 0 8px}input{width:100%;min-height:48px;border:1px solid #9aabb5;border-radius:9px;padding:10px 12px;font:inherit;color:inherit}
    input:focus{outline:3px solid #145b7330;border-color:var(--blue)}.hint{font-size:.88rem;color:var(--muted);margin:7px 0 18px}
    button{min-height:46px;border:0;border-radius:9px;padding:10px 18px;background:var(--blue);color:#fff;font:700 1rem inherit;cursor:pointer}
    button:hover{background:#0d465c}button:focus-visible{outline:3px solid #145b7350;outline-offset:3px}
    .status{margin-top:18px;padding:13px 14px;background:var(--pale);border-radius:9px;color:var(--blue);font-weight:700}
    .status[data-kind="success"]{background:#edf7f1;color:var(--green)}.status[data-kind="error"]{background:#fff0ec;color:#91391f}
    .log-title{margin:23px 0 6px;font-size:.95rem}.events{margin:0;padding-left:20px;color:var(--muted);font-size:.9rem;min-height:28px}.events li+li{margin-top:3px}
    @media (max-width:600px){main{width:calc(100% - 28px);margin:20px auto}.panel{padding:18px;border-radius:13px}button{width:100%}}
    /* The 390px reference viewport is an explicit E2E observation size. */
    @media (max-width:390px){main{width:calc(100% - 24px);margin:16px auto}.intro{font-size:.94rem}.panel{padding:16px}}
  </style>
</head>
<body><main>
  <div class="eyebrow">LOCAL REVERSE E2E · ORIGINAL</div>
  <h1>주문 준비</h1><p class="intro">수량을 확인하고 준비 요청을 보내세요. 요청 수와 최근 사용자 이벤트가 아래에 표시됩니다.</p>
  <section class="panel" aria-labelledby="form-title">
    <h2 id="form-title" style="font-size:1.1rem;margin:0 0 18px">준비 요청</h2>
    <form id="request-form">
      <label for="quantity">수량</label><input id="quantity" name="quantity" type="number" min="1" max="5" step="1" value="1" required aria-describedby="quantity-hint">
      <p class="hint" id="quantity-hint">1개부터 5개까지 입력할 수 있습니다.</p>
      <button type="submit">준비 요청</button>
    </form>
    <div class="status" id="status" data-kind="idle" role="status" aria-live="polite">대기 중 · 요청 0건</div>
    <h3 class="log-title">사용자 이벤트</h3><ol class="events" id="events" aria-live="polite"></ol>
  </section>
</main>
<script>
(() => {
  let count = 0;
  const form = document.querySelector('#request-form');
  const quantity = document.querySelector('#quantity');
  const status = document.querySelector('#status');
  const events = document.querySelector('#events');
  function record(message) {
    const item = document.createElement('li');
    item.textContent = message;
    events.prepend(item);
  }
  quantity.addEventListener('change', () => record(`수량 변경: ${quantity.value || '비움'}`));
  form.addEventListener('submit', event => {
    event.preventDefault();
    if (!form.reportValidity()) {
      status.dataset.kind = 'error';
      status.textContent = `입력 오류 · 요청 ${count}건`;
      record('잘못된 수량을 제출함');
      return;
    }
    count += 1;
    status.dataset.kind = 'success';
    status.textContent = `준비 완료 · ${quantity.value}개 · 요청 ${count}건`;
    record(`요청 성공: ${quantity.value}개`);
  });
})();
</script></body></html>
'''


def _load_demo_handler():
    spec = importlib.util.spec_from_file_location("reverse_compare_api_demo", API_DEMO)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load existing API demo: {API_DEMO}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.DemoHandler


def _api_server(port: int) -> ThreadingHTTPServer:
    DemoHandler = _load_demo_handler()

    class TargetHandler(DemoHandler):
        state: ClassVar[dict] = {"count": 0}
        items: ClassVar[list[dict]] = []

        def do_GET(self):
            if self.path == "/items":
                self._send(200, self.items)
                return
            super().do_GET()

        def do_POST(self):
            if self.path != "/items":
                self._send(404, {"error": "not_found"})
                return
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
            self.items.append({"value": value["value"]})
            self.rfile = __import__("io").BytesIO(raw)
            super().do_POST()

    return ThreadingHTTPServer(("127.0.0.1", port), TargetHandler)


def _web_server(port: int) -> ThreadingHTTPServer:
    class WebHandler(SimpleHTTPRequestHandler):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, directory="/", **kwargs)

        def do_GET(self):
            if self.path not in ("/", "/index.html"):
                self.send_error(404)
                return
            body = WEB_HTML.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args):
            pass

    return ThreadingHTTPServer(("127.0.0.1", port), WebHandler)


def _serve(kind: str, port: int, owner: str, control_port: int) -> int:
    server = _api_server(port) if kind == "api" else _web_server(port)
    serving = threading.Thread(target=server.serve_forever, name="reverse-e2e-http", daemon=True)
    serving.start()
    actual_port = server.server_address[1]
    path = "/items" if kind == "api" else "/"
    stopped = threading.Event()

    class ControlHandler(socketserver.StreamRequestHandler):
        def handle(self):
            try:
                request = json.loads(self.rfile.readline(1024))
            except (json.JSONDecodeError, UnicodeDecodeError):
                request = {}
            accepted = request.get("owner") == owner
            response = {"accepted": accepted, "pid": os.getpid(), "owner": owner if accepted else None}
            self.wfile.write((json.dumps(response, separators=(",", ":")) + "\n").encode())
            if accepted:
                stopped.set()

    control = socketserver.ThreadingTCPServer(("127.0.0.1", control_port), ControlHandler)
    control.daemon_threads = True
    control_serving = threading.Thread(target=control.serve_forever, name="reverse-e2e-control", daemon=True)
    control_serving.start()
    actual_control_port = control.server_address[1]
    print(json.dumps({"ready": True, "kind": kind, "environment": "local-loopback", "host": "127.0.0.1", "port": actual_port, "control_port": actual_control_port, "url": f"http://127.0.0.1:{actual_port}{path}", "owner": owner}, separators=(",", ":")), flush=True)

    def request_stop(_signum, _frame):
        stopped.set()

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    try:
        stopped.wait()
    finally:
        server.shutdown()
        server.server_close()
        serving.join(timeout=3)
        control.shutdown()
        control.server_close()
        control_serving.join(timeout=3)
    return 0


def _start(kind: str, host: str, port: int, state_dir: Path) -> dict:
    if host != "127.0.0.1":
        raise ValueError("this target binds only to 127.0.0.1")
    state_dir.mkdir(parents=True, exist_ok=True)
    owner = secrets.token_hex(16)
    pid_file = state_dir / f"reverse-e2e-{kind}-{owner[:8]}.pid.json"
    child = subprocess.Popen(
        [sys.executable, str(SCRIPT), "_serve", kind, "--port", str(port), "--control-port", "0", "--owner", owner],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        start_new_session=True,
    )
    assert child.stdout is not None
    line = child.stdout.readline()
    if not line:
        code = child.poll()
        raise RuntimeError(f"target did not become ready (exit={code})")
    ready = json.loads(line)
    if not ready.get("ready") or ready.get("owner") != owner:
        child.terminate()
        raise RuntimeError("target readiness did not match owner")
    pid_file.write_text(json.dumps({"pid": child.pid, "owner": owner, "kind": kind, "script": str(SCRIPT), "control_port": ready["control_port"]}), encoding="utf-8")
    ready.update({"pid": child.pid, "pid_file": str(pid_file), "host": "127.0.0.1"})
    ready.pop("owner", None)
    return ready


def _stop(pid_file: Path) -> dict:
    try:
        record = json.loads(pid_file.read_text(encoding="utf-8"))
        pid, owner, script = int(record["pid"]), record["owner"], record["script"]
        control_port = int(record["control_port"])
        kind = record["kind"]
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise ValueError(f"invalid ownership metadata: {exc}") from exc
    if script != str(SCRIPT):
        raise ValueError("ownership check failed: target script does not match")
    with socket.create_connection(("127.0.0.1", control_port), timeout=3) as connection:
        connection.sendall((json.dumps({"owner": owner}) + "\n").encode())
        response = json.loads(connection.makefile("rb").readline(1024))
    if not response.get("accepted") or response.get("pid") != pid or response.get("owner") != owner:
        raise ValueError("ownership check failed: target did not confirm PID and owner token")
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", control_port), timeout=.2):
                pass
        except OSError:
            break
        time.sleep(.05)
    else:
        raise RuntimeError(f"owned target PID {pid} did not stop after authenticated shutdown request")
    pid_file.unlink(missing_ok=True)
    return {"stopped": True, "pid": pid, "kind": kind}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subs = parser.add_subparsers(dest="command", required=True)
    start = subs.add_parser("start", help="start a target in a separate process")
    start.add_argument("kind", choices=("api", "web"))
    start.add_argument("--host", default="127.0.0.1")
    start.add_argument("--port", "--api-port", "--web-port", type=int, default=0)
    start.add_argument("--state-dir", type=Path, default=Path("/tmp/reverse-e2e-targets"))
    stop = subs.add_parser("stop", help="stop only the process named by owned PID metadata")
    stop.add_argument("--pid-file", type=Path, required=True)
    serve = subs.add_parser("_serve", help=argparse.SUPPRESS)
    serve.add_argument("kind", choices=("api", "web"))
    serve.add_argument("--port", type=int, required=True)
    serve.add_argument("--control-port", type=int, required=True)
    serve.add_argument("--owner", required=True)
    args = parser.parse_args()
    try:
        if args.command == "start":
            result = _start(args.kind, args.host, args.port, args.state_dir)
        elif args.command == "stop":
            result = _stop(args.pid_file)
        else:
            return _serve(args.kind, args.port, args.owner, args.control_port)
        print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
        return 0
    except (OSError, RuntimeError, ValueError, json.JSONDecodeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
