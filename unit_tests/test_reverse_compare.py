import base64
import hashlib
import json
import sys

from ttakkari.reverse_compare import (
    run_api_comparison,
    run_comparison,
    run_web_comparison,
)


def test_cli_records_exact_bytes_shared_argv_context_and_recomputable_capture(tmp_path):
    script = r"import sys; sys.stdout.buffer.write(('|'.join(sys.argv[0:])+' 한글\n').encode()); sys.stderr.buffer.write(b'\xff')"
    cmd = [sys.executable, "-c", script]
    result = run_comparison(
        tmp_path, comparison_id="utf8-boundary", behavior="UTF-8 경계와 argv", target_type="cli",
        input_data={"argv": ["공백 포함", ""], "vector_id": "v-1"},
        original_command=cmd, replica_command=cmd, pre_state={"cwd": str(tmp_path)},
        run_id="run-1", timeout_seconds=3,
    )
    assert result["result"] == "match"
    assert result["original"]["stdout_bytes_b64"] == base64.b64encode("-c|공백 포함| 한글\n".encode()).decode()
    assert result["original"]["stderr_bytes_b64"] == "/w=="
    assert result["original"]["argv"] == cmd + ["공백 포함", ""]
    assert result["replica"]["argv"] == result["original"]["argv"]
    raw_bytes = (tmp_path / result["evidence"][0]["path"]).read_bytes()
    raw = json.loads(raw_bytes)
    assert hashlib.sha256(raw_bytes).hexdigest() == result["evidence"][0]["sha256"]
    assert raw["original"] == result["original"] and raw["replica"] == result["replica"]


def test_cli_timeout_and_missing_original_are_not_match(tmp_path):
    replica = [sys.executable, "-c", "print('replica')"]
    result = run_comparison(
        tmp_path, comparison_id="timeout", behavior="timeout", target_type="cli", input_data={"argv": []},
        original_command=[sys.executable, "-c", "import time; time.sleep(2)"], replica_command=replica,
        run_id="run-2", timeout_seconds=0.05,
    )
    assert result["result"] == "unverified"
    assert result["original"] is None
    assert result["difference"]["reason"]
    raw = json.loads((tmp_path / result["evidence"][0]["path"]).read_text("utf-8"))
    assert raw["original"] is None
    assert "TimeoutExpired" in raw["execution_log"][0]["error"]
    assert result["replica"]["exit_code"] == 0
    missing = run_comparison(
        tmp_path, comparison_id="original-missing", behavior="missing", target_type="cli", input_data={"argv": []},
        original_command=[str(tmp_path / "does-not-exist")], replica_command=replica, run_id="run-3", timeout_seconds=1,
    )
    assert missing["original"] is None and missing["result"] == "unverified"


def test_api_compares_response_and_state_without_persisting_secret_headers(tmp_path):
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    states = {}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(states[self.server.server_port]).encode())

        def do_POST(self):
            body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
            states[self.server.server_port]["value"] += 1
            self.send_response(201)
            self.send_header("Content-Type", "application/json")
            self.send_header("Set-Cookie", "session=fixture")
            self.end_headers()
            self.wfile.write(b'{"accepted":true,"echo":' + body + b'}')

        def log_message(self, *_args):
            pass

    servers = [ThreadingHTTPServer(("127.0.0.1", 0), Handler) for _ in range(2)]
    states.update({s.server_port: {"value": 0} for s in servers})
    threads = [threading.Thread(target=s.serve_forever, daemon=True) for s in servers]
    for t in threads:
        t.start()
    try:
        result = run_api_comparison(
            tmp_path, comparison_id="api-json", behavior="POST 직렬화", input_data={"json": {"text": "한글"}, "state_path": "/state"},
            original_url=f"http://127.0.0.1:{servers[0].server_port}/item",
            replica_url=f"http://127.0.0.1:{servers[1].server_port}/item", method="POST",
            headers={"Authorization": "Bearer secret"}, run_id="run-api", timeout_seconds=2,
        )
    finally:
        for s in servers:
            s.shutdown()
            s.server_close()
    assert result["result"] == "match"
    raw = json.loads((tmp_path / result["evidence"][0]["path"]).read_text())
    assert "secret" not in json.dumps(raw)
    assert result["original"]["status"] == 201
    assert result["original"]["body_bytes_b64"]
    assert result["original"]["state_before"]["body"] == '{"value": 0}'
    assert result["original"]["state_after"]["body"] == '{"value": 1}'
    from ttakkari.reverse_tasks import verify_comparisons
    checked = verify_comparisons(tmp_path, [result], expected_run_id="run-api")[0]
    assert checked["result"] == "match" and checked["evidence_level"] == "direct", checked["difference"]


def test_api_comparison_rejects_unallowlisted_hosts(tmp_path):
    import pytest

    with pytest.raises(ValueError, match="allowlisted"):
        run_api_comparison(
            tmp_path, comparison_id="api-blocked", behavior="외부 호스트 차단", input_data={},
            original_url="https://example.test/api", replica_url="http://127.0.0.1:8000/api", run_id="run-api",
        )


def test_api_comparison_blocks_redirect_escape_and_redacts_query_secrets(tmp_path):
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    hits = {"redirect_target": 0}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path.startswith("/redirect-target"):
                hits["redirect_target"] += 1
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"followed")
                return
            self.send_response(302)
            self.send_header("Location", f"http://127.0.0.1:{self.server.server_port}/redirect-target?token=demo-secret")
            self.end_headers()

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f"http://localhost:{server.server_port}/redirect?access_token=demo-secret"
    try:
        result = run_api_comparison(
            tmp_path, comparison_id="api-redirect", behavior="리다이렉트 차단", input_data={},
            original_url=url, replica_url=url, run_id="run-redirect", allowed_hosts=("localhost",),
        )
    finally:
        server.shutdown()
        server.server_close()
    raw_text = (tmp_path / result["evidence"][0]["path"]).read_text("utf-8")
    assert result["original"]["status"] == 302
    assert hits["redirect_target"] == 0
    assert "demo-secret" not in raw_text


def test_web_functional_match_does_not_claim_visual_match(tmp_path):
    observation = {"events": [{"type": "click", "selector": "button#go"}], "dom": {"text": "완료"},
                   "state": {"done": True}, "network": [{"status": 200}]}
    result = run_web_comparison(
        tmp_path, comparison_id="web-click", behavior="버튼 완료", input_data={"url": "http://localhost"},
        original_observation=observation, replica_observation=observation, run_id="run-web",
        screenshots={"original": "original.png", "replica": "replica.png"},
    )
    assert result["result"] == "match"
    raw = json.loads((tmp_path / result["evidence"][0]["path"]).read_text())
    assert raw["complete"] is True
    assert any(e["kind"] == "visual_assessment" and e["result"] == "unverified" for e in result["evidence"])
    assert result["evidence_level"] == "observed"
    checked = __import__("ttakkari.reverse_tasks", fromlist=["verify_comparisons"]).verify_comparisons(
        tmp_path, [result], expected_run_id="run-web")[0]
    assert checked["result"] == "unverified"


def test_cli_compares_invalid_utf8_by_exact_bytes_and_rejects_cwd_escape(tmp_path):
    left = [sys.executable, "-c", r"import sys; sys.stdout.buffer.write(b'\xfe')"]
    right = [sys.executable, "-c", r"import sys; sys.stdout.buffer.write(b'\xff')"]
    result = run_comparison(tmp_path, comparison_id="raw-byte", behavior="invalid UTF-8 bytes",
        target_type="cli", input_data={"argv": []}, original_command=left, replica_command=right, run_id="bytes")
    assert result["result"] == "mismatch"
    assert result["original"]["stdout"] == result["replica"]["stdout"]
    assert result["original"]["stdout_bytes_b64"] != result["replica"]["stdout_bytes_b64"]
    assert result["difference"]["field"] == "stdout_bytes_b64"

    import pytest
    with pytest.raises(ValueError, match="inside the workspace"):
        run_comparison(tmp_path, comparison_id="cwd-escape", behavior="cwd escape", target_type="cli",
            input_data={"argv": []}, original_command=left, replica_command=right, run_id="escape",
            pre_state={"cwd": "/private/tmp"})
