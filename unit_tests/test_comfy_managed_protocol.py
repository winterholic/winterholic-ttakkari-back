"""Managed Comfy 경로를 Lab 실제 adapter(ComfyClient/Runner/Registry)와 로컬 HTTP stub 서버로 검증한다.

GPU와 실제 ComfyUI 프로세스는 쓰지 않는다. stub은 ComfyUI의 /prompt, /queue, /history, /view, /interrupt,
/object_info 계약만 흉내 내므로 이 파일은 "실제 adapter 코드 + HTTP 프로토콜" 증거이고 실 GPU 증거가 아니다.
websocket-client가 backend 환경에 없으면 adapter는 /history 폴링으로 관측하므로 WS 경로는 여기서 검증하지 않는다.
"""
from __future__ import annotations

import hashlib
import json
import socket
import subprocess
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

import pytest

from ttakkari import managed_comfy
from ttakkari.host_resource import LeaseBusy, ResourceLeaseManager
from ttakkari.managed_comfy import PROFILES, ComfyManagedController
from ttakkari.managed_comfy_receipt import canonical_sha256

pytestmark = pytest.mark.skipif(
    not (managed_comfy.REGISTRY_ROOT / "t2v-wan22-5b").exists() or not managed_comfy.ADAPTER_ROOT.exists(),
    reason="Lab Comfy adapter/registry is not available on this host",
)

PROFILE = PROFILES["wan22-5b-draft"]


class StubComfy:
    def __init__(self, graph: dict) -> None:
        self.graph = graph
        self.lock = threading.Lock()
        self.entries: list[dict] = []
        self.interrupts: list[dict] = []
        self.submit_count = 0
        self.hold = False
        self.view_status = 200
        self.drop_submit_response = False
        self.hide_queue = False
        self.http: ThreadingHTTPServer | None = None

    def object_info(self) -> dict:
        info: dict = {}
        for node in self.graph.values():
            required = info.setdefault(node["class_type"], {"input": {"required": {}}})["input"]["required"]
            for key, value in node["inputs"].items():
                if key.endswith("_name") and isinstance(value, str):
                    required.setdefault(key, [[]])[0].append(value)
        return info

    def running(self) -> list[dict]:
        return [e for e in self.entries if e["state"] == "running"]

    def finish(self, entry: dict, *, interrupted: bool = False) -> None:
        entry["state"] = "done"
        if interrupted:
            entry["status"] = {"status_str": "error", "completed": True,
                               "messages": [["execution_interrupted", {"prompt_id": entry["id"]}]]}
            entry["outputs"] = {}
        else:
            entry["status"] = {"status_str": "success", "completed": True, "messages": []}
            entry["outputs"] = {"20": {"images": [{"filename": "clip.webp", "subfolder": "", "type": "output"}]}}

    def queue_view(self, state: str) -> list:
        if self.hide_queue:
            return []
        return [[e["number"], e["id"], e["prompt"], e["extra"], []] for e in self.entries if e["state"] == state]

    def history_view(self, prompt_id: str | None) -> dict:
        if self.hide_queue:
            return {}
        out = {e["id"]: {"prompt": [e["number"], e["id"], e["prompt"], e["extra"], []],
                         "outputs": e["outputs"], "status": e["status"]}
               for e in self.entries if e["state"] == "done"}
        return {prompt_id: out[prompt_id]} if prompt_id and prompt_id in out else ({} if prompt_id else out)

    def handler(self):
        stub = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                return

            def _send(self, payload, status=200, raw=False):
                body = payload if raw else json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                path = urlparse(self.path).path
                with stub.lock:
                    if path == "/system_stats":
                        return self._send({"system": {}})
                    if path == "/object_info":
                        return self._send(stub.object_info())
                    if path == "/queue":
                        return self._send({"queue_running": stub.queue_view("running"),
                                           "queue_pending": stub.queue_view("pending")})
                    if path.startswith("/history"):
                        return self._send(stub.history_view(path.removeprefix("/history").strip("/") or None))
                    if path == "/view":
                        if stub.view_status != 200:
                            return self._send({"error": "boom"}, stub.view_status)
                        return self._send(b"RIFF-fake-webp", raw=True)
                self._send({}, 404)

            def do_POST(self):
                path = urlparse(self.path).path
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
                with stub.lock:
                    if path == "/prompt":
                        stub.submit_count += 1
                        entry = {"id": uuid.uuid4().hex, "number": stub.submit_count, "prompt": body["prompt"],
                                 "extra": body.get("extra_data", {}), "outputs": {}, "status": None,
                                 "state": "running" if not stub.running() else "pending"}
                        stub.entries.append(entry)
                        if not stub.hold:
                            stub.finish(entry)
                        if stub.drop_submit_response:
                            self.close_connection = True
                            return None
                        return self._send({"prompt_id": entry["id"], "number": entry["number"], "node_errors": {}})
                    if path == "/interrupt":
                        stub.interrupts.append(body)
                        for entry in stub.running():
                            if entry["id"] == body.get("prompt_id"):
                                stub.finish(entry, interrupted=True)
                        return self._send({})
                    if path == "/queue":
                        stub.entries[:] = [e for e in stub.entries
                                           if not (e["state"] == "pending" and e["id"] in body.get("delete", []))]
                        return self._send({})
                self._send({}, 404)

        return Handler

    def start(self, port: int) -> None:
        self.http = ThreadingHTTPServer(("127.0.0.1", port), self.handler())
        threading.Thread(target=self.http.serve_forever, daemon=True, name="stub-comfy").start()

    def stop(self) -> None:
        if self.http:
            self.http.shutdown()
            self.http.server_close()
            self.http = None


class StubServer:
    """ComfyServer 대역: 실제 프로세스 대신 `sleep` 자식과 stub HTTP 서버를 소유한다(GPU 모델 로드 없음)."""

    def __init__(self, real, stub: StubComfy, port: int) -> None:
        self.real, self.stub, self.port = real, stub, port
        self.client = real.client
        self.proc: subprocess.Popen | None = None
        self.owned = False
        self.start_kwargs: dict = {}

    def start(self, **kwargs):
        self.start_kwargs = kwargs
        self.proc = subprocess.Popen(["sleep", "120"])
        self.owned = True
        self.stub.start(self.port)
        return self

    def stop(self, grace: float = 20) -> dict:
        proc = self.proc
        if not (self.owned and proc):
            return {"stopped": False, "reason": "not owned"}
        proc.terminate()
        proc.wait(timeout=10)
        self.stub.stop()
        self.proc, self.owned = None, False
        return {"stopped": True, "pid": proc.pid}


@pytest.fixture
def harness(tmp_path):
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    stubs: list[StubComfy] = []
    servers: list[StubServer] = []

    def factory(profile, runtime_instance, data_dir, lease, controller):
        # 실제 factory로 adapter 모듈·registry·graph hash 검증을 통과시킨 뒤 프로세스만 대역으로 바꾼다.
        runtime = controller._real_factory(profile, runtime_instance, data_dir, lease, controller)
        api, _meta, _hash = runtime["registry_cls"](root=managed_comfy.REGISTRY_ROOT).load(
            profile.workflow, profile.revision)
        stub = StubComfy(api)
        stub.hold = harness_state["hold"]
        stubs.append(stub)
        server = StubServer(runtime["server"], stub, port)
        servers.append(server)
        runtime["server"] = server
        return runtime

    harness_state = {"hold": False}
    service = ComfyManagedController(data_dir=tmp_path, runtime_factory=factory, spawn=lambda fn, *a: fn(*a),
                                     port=port)
    service._real_factory = lambda *args: ComfyManagedController._runtime_factory(service, *args)
    yield service, stubs, servers, harness_state
    for stub in stubs:
        stub.hold = False
        stub.stop()
    for server in servers:
        if server.proc and server.proc.poll() is None:
            server.proc.kill()
    for active in list(service._active.values()):
        active["heartbeat_stop"].set()
        active["cancel_stop"].set()


def lease_of(service):
    return ResourceLeaseManager(data_dir=service.data_dir).current()


def wait_for(predicate, timeout=20.0):
    end = time.time() + timeout
    while time.time() < end:
        if predicate():
            return True
        time.sleep(0.05)
    return False


def test_real_adapter_runs_registered_profile_over_http_and_releases_after_runtime_stop(harness):
    service, stubs, servers, _state = harness
    task = service.prepare("wan22-5b-draft", prompt="A fox", seed=7)["task_id"]

    job = service.execute(task)

    assert job["status"] == "succeeded"
    stub = stubs[0]
    assert stub.submit_count == 1 and stub.interrupts == []
    submitted = stub.entries[0]
    assert submitted["extra"]["ttk_job"] == job["runner_job_id"]
    assert PROFILE.models[0] in json.dumps(submitted["prompt"])
    assert servers[0].start_kwargs == {"required_gb": PROFILE.required_gb}
    files = job["result"]["files"]
    assert files and Path(files[0]["path"]).read_bytes() == b"RIFF-fake-webp"
    assert lease_of(service) is None
    assert job["lease_release_proof"]["evidence"]["server_health_after_stop"] is False
    receipt = job["receipt"]
    assert "receipt_error" not in job
    # stub 이 실제로 받은 graph 와 receipt 가 같은 해시를 말한다(adapter 가 만든 값이 아니라 받은 쪽에서 독립 계산).
    assert receipt["resolved_graph_sha256"] == canonical_sha256(submitted["prompt"])
    assert receipt["graph_hash_consistent"] and receipt["seed_consistent"] and receipt["seed"] == 7
    assert receipt["external_prompt_id"] == submitted["id"]
    assert receipt["outputs"][0]["sha256"] == hashlib.sha256(b"RIFF-fake-webp").hexdigest()
    assert receipt["profile_id"] == PROFILE.profile_id and receipt["workflow_revision"] == PROFILE.revision
    assert receipt["lease"]["owner_task"] == task and receipt["lease"]["runtime_instance"] == job["runtime_instance"]
    assert [m["name"] for m in receipt["models"]] == list(PROFILE.models)


def test_running_cancel_interrupts_only_after_exclusive_lease_and_rejects_foreign_owner(harness):
    service, stubs, servers, state = harness
    state["hold"] = True
    task = service.prepare("wan22-5b-draft", prompt="A fox", seed=7)["task_id"]
    with pytest.raises(LeaseBusy):
        service.prepare("wan22-5b-draft", prompt="Another", seed=8)
    runner = threading.Thread(target=service.execute, args=(task,), daemon=True)
    runner.start()
    assert wait_for(lambda: stubs and stubs[0].running() and service.get(task)["status"] == "running")
    stub = stubs[0]

    with pytest.raises(PermissionError):
        service.cancel(task, owner_task="someone-else")
    assert stub.interrupts == [] and lease_of(service)["phase"] == "running"

    requested = service.cancel(task, owner_task=task)
    assert requested["status"] in {"cancel_requested", "cancelled"}
    runner.join(timeout=30)

    assert not runner.is_alive()
    saved = service.get(task)
    assert saved["status"] == "cancelled"
    assert "receipt" not in saved
    assert [item["prompt_id"] for item in stub.interrupts] == [stub.entries[0]["id"]]
    assert stub.submit_count == 1 and servers[0].proc is None
    assert lease_of(service) is None


def test_partial_download_failure_keeps_lease_and_reconcile_collects_without_resubmit(harness, monkeypatch):
    service, stubs, servers, _state = harness
    task = service.prepare("wan22-5b-draft", prompt="A fox", seed=7)["task_id"]
    original_start = StubServer.start

    def start_with_broken_view(self, **kwargs):
        result = original_start(self, **kwargs)
        self.stub.view_status = 500
        return result

    monkeypatch.setattr(StubServer, "start", start_with_broken_view)
    job = service.execute(task)

    assert job["status"] == "needs_reconcile"
    assert lease_of(service) and servers[0].proc is not None and servers[0].proc.poll() is None
    stub = stubs[0]
    assert stub.submit_count == 1

    stub.view_status = 200
    result = service.reconcile(task, owner_task=task)

    assert result["status"] == "succeeded"
    assert Path(result["result"]["files"][0]["path"]).read_bytes() == b"RIFF-fake-webp"
    assert stub.submit_count == 1
    assert lease_of(service) is None
    assert "receipt" not in job  # 불완전 수집 상태에서는 receipt 를 만들지 않는다
    assert result["receipt"]["outputs"][0]["sha256"] == hashlib.sha256(b"RIFF-fake-webp").hexdigest()


def test_lost_submit_response_is_reconciled_without_a_second_submit_and_keeps_lease(harness, monkeypatch):
    service, stubs, servers, _state = harness
    task = service.prepare("wan22-5b-draft", prompt="A fox", seed=7)["task_id"]
    original_start = StubServer.start

    def start_lossy(self, **kwargs):
        result = original_start(self, **kwargs)
        self.stub.drop_submit_response = True
        self.stub.hide_queue = True
        return result

    monkeypatch.setattr(StubServer, "start", start_lossy)
    job = service.execute(task)

    assert job["status"] == "needs_reconcile"
    assert lease_of(service) and servers[0].proc is not None
    stub = stubs[0]
    assert stub.submit_count == 1

    stub.drop_submit_response = False
    stub.hide_queue = False
    result = service.reconcile(task, owner_task=task)

    assert result["status"] == "succeeded"
    assert stub.submit_count == 1
    assert lease_of(service) is None
