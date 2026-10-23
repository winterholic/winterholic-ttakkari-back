"""자동 소재 생성에서 엔진 사이 GPU lease 인계와 receipt 의 task 조회를 고정하는 회귀 테스트.

운영 2026-10-23: 장면 Comfy 가 succeeded 로 기록된 직후(70ms) 다음 장면 prepare 가
`LeaseBusy: host GPU resource is owned by another runtime` 로 실패했다. 성공 기록은 런타임 정지·lease 해제보다 먼저 저장된다.
"""
from __future__ import annotations

import hashlib
import json
import threading
import time
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from ttakkari.api import comfy_managed as api
from ttakkari.creative.v2 import asset_generation_managed as managed
from ttakkari.host_resource import LeaseBusy, ResourceLeaseManager
from ttakkari.managed_comfy import PROFILES, ComfyManagedController


class SlowStopServer:
    """stop() 이 gate 가 열릴 때까지 끝나지 않는 Comfy 런타임. 성공 기록 뒤 lease 해제 전 구간을 재현한다."""

    def __init__(self, gate: threading.Event):
        self.gate = gate
        self.owned = True
        self.started = False
        self.stopped = False
        self.proc = type("Proc", (), {"pid": 2317, "returncode": 0,
                                      "poll": lambda _self: 0 if self.stopped else None})()
        self.client = type("Client", (), {
            "alive": lambda _self: self.started and not self.stopped,
            "queue": lambda _self: {"queue_running": [], "queue_pending": []},
        })()

    def start(self, **_kwargs):
        self.started = True
        return self

    def stop(self):
        self.gate.wait(10)
        self.stopped = True
        return {"stopped": True, "pid": self.proc.pid}


class QuickRunner:
    def __init__(self, jobs_dir: Path):
        self.jobs_dir = jobs_dir

    def check(self, _workflow, version):
        assert version == PROFILES["wan22-5b-draft"].revision
        return {"ok": True}

    def run(self, **kwargs):
        job_dir = self.jobs_dir / f"adapter-{kwargs['tag']}"
        job_dir.mkdir(parents=True)
        job = {"id": f"adapter-{kwargs['tag']}", "tag": kwargs["tag"], "status": "succeeded",
               "attempts": [{"attempt_id": "attempt-1", "external_prompt_id": "prompt-1"}],
               "files": [], "failure": None}
        (job_dir / "job.json").write_text(json.dumps(job))
        return job


def _controller(tmp_path: Path, gate: threading.Event) -> ComfyManagedController:
    def runtime_factory(_profile, _instance, data_dir, _lease, _controller):
        server = SlowStopServer(gate)
        return server, QuickRunner(data_dir / "managed-comfy" / "jobs")

    return ComfyManagedController(
        data_dir=tmp_path, runtime_factory=runtime_factory,
        spawn=lambda fn, *args: threading.Thread(target=fn, args=args, daemon=True).start(),
    )


def _wait_for(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("condition was not reached in time")


def test_next_scene_prepare_waits_for_previous_scene_lease_handoff(tmp_path):
    gate = threading.Event()
    service = _controller(tmp_path, gate)
    first = service.prepare("wan22-5b-draft", prompt="scene 1", seed=1, owner="creative-v2:p:1:s1")
    service.start(first["task_id"])
    # 생성기가 보는 것과 같은 신호: 성공은 기록됐지만 런타임 정지·lease 해제는 아직이다.
    _wait_for(lambda: service.get(first["task_id"])["status"] == "succeeded")

    with pytest.raises(LeaseBusy, match="owned by another runtime") as busy:
        service.prepare("wan22-5b-draft", prompt="scene 2", seed=2, owner="creative-v2:p:1:s2")
    assert busy.value.retryable
    assert first["task_id"] in str(busy.value)  # 점유자(자기 앞 장면)를 사유에 밝힌다

    threading.Timer(0.3, gate.set).start()
    second = service.prepare("wan22-5b-draft", prompt="scene 2", seed=2,
                             owner="creative-v2:p:1:s2", lease_wait_seconds=10)

    lease = ResourceLeaseManager(data_dir=tmp_path).current()
    assert lease and lease["owner_task"] == second["task_id"]


def test_acquire_reports_external_holder_after_bounded_wait(tmp_path):
    manager = ResourceLeaseManager(data_dir=tmp_path)
    held = manager.acquire(owner_task="aip-eval", owner_attempt="a1", runtime_instance="qwen:1",
                           resource_family="llm", claim={"gpu": 1}, ttl_seconds=60)

    started = time.monotonic()
    with pytest.raises(LeaseBusy) as busy:
        ResourceLeaseManager(data_dir=tmp_path).acquire(
            owner_task="next", owner_attempt="a2", runtime_instance="comfy:2", resource_family="comfy",
            claim={"gpu": 1}, ttl_seconds=60, wait_seconds=0.6, poll_seconds=0.1,
        )

    assert 0.5 <= time.monotonic() - started < 3
    message = str(busy.value)
    assert "owner_task=aip-eval" in message and "family=llm" in message and "waited 0.6s" in message
    held.release(proof={"runtime_status": "stopped", "evidence": {"runtime_instance": "qwen:1", "exit_code": 0}})


def test_acquire_does_not_wait_for_expired_lease_that_needs_reconcile(tmp_path):
    manager = ResourceLeaseManager(data_dir=tmp_path)
    held = manager.acquire(owner_task="dead", owner_attempt="a1", runtime_instance="r1",
                           resource_family="comfy", claim={"gpu": 1}, ttl_seconds=1, now=100.0)
    held._lock_fd.close()  # 소유 프로세스가 해제 없이 사라진 상태

    started = time.monotonic()
    with pytest.raises(LeaseBusy, match="reconciled") as busy:
        ResourceLeaseManager(data_dir=tmp_path).acquire(
            owner_task="next", owner_attempt="a2", runtime_instance="r2", resource_family="comfy",
            claim={"gpu": 1}, wait_seconds=30,
        )

    assert not busy.value.retryable
    assert time.monotonic() - started < 2


def test_acquire_rejects_invalid_wait(tmp_path):
    manager = ResourceLeaseManager(data_dir=tmp_path)
    with pytest.raises(ValueError, match="wait_seconds"):
        manager.acquire(owner_task="t", owner_attempt="a", runtime_instance="r", resource_family="comfy",
                        claim={}, wait_seconds=-1)


def test_creative_owned_task_is_readable_but_foreign_owner_is_not(tmp_path):
    service = _controller(tmp_path, threading.Event())
    creative = service.prepare("wan22-5b-draft", prompt="scene", seed=1, owner="creative-v2:p:1:s1")

    with pytest.raises(FileNotFoundError):
        service.get(creative["task_id"], owner="agent:session-1")
    assert service.get(creative["task_id"], owner="agent:session-1",
                       readable_owner_prefixes=("creative-v2:",))["task_id"] == creative["task_id"]


def test_agent_job_status_exposes_creative_tasks_read_only(monkeypatch):
    calls = {}

    class Service:
        def get(self, task_id, *, owner=None, readable_owner_prefixes=()):
            calls["get"] = (task_id, owner, readable_owner_prefixes)
            return {"task_id": task_id, "profile": "wan22-5b-draft", "status": "succeeded"}

        def cancel(self, task_id, *, owner_task=None, owner=None):
            calls["cancel"] = (task_id, owner)
            raise FileNotFoundError("managed Comfy task not found")

    class Settings:
        data_dir = None

    monkeypatch.setattr(api, "get_settings", lambda: Settings())
    monkeypatch.setattr(api, "default_controller", lambda _data_dir: Service())
    app = FastAPI()
    app.include_router(api.agent_router)
    app.dependency_overrides[api.agent_access] = lambda: "agent:session-1"
    client = TestClient(app)
    prefix = "/api/agent/comfy/jobs"

    task_id = "a" * 32
    assert client.get(f"{prefix}/{task_id}").json()["status"] == "succeeded"
    assert calls["get"] == (task_id, "agent:session-1", ("creative-v2:",))
    assert client.post(f"{prefix}/{task_id}/cancel").status_code == 404
    assert calls["cancel"] == (task_id, "agent:session-1")  # 취소는 읽기 접두사 없이 소유자 일치를 요구한다


def test_scene_generation_requests_bounded_lease_wait_from_controller(tmp_path):
    seen = {}

    class Controller:
        def prepare(self, profile, *, prompt, seed, owner, lease_wait_seconds=0):
            seen["wait"] = lease_wait_seconds
            return {"task_id": "b" * 32, "status": "starting"}

        def start(self, _task_id):
            pass

        def get(self, task_id):
            return {"task_id": task_id, "profile": "wan22-5b-draft", "status": "succeeded",
                    "runner_job_id": "j1", "receipt": {
                        "prompt_sha256": hashlib.sha256(b"prompt").hexdigest(),
                        "outputs": [{"sha256": "c" * 64}],
                    }}

    generator = managed.ManagedAssetGenerator(controller=Controller())
    plan = {"id": "plan-1", "revision": 1}
    events = []

    job = generator._run_comfy("prompt", 1, plan, "s1", tmp_path, events.append)

    assert job["status"] == "succeeded"
    assert seen["wait"] == managed.LEASE_HANDOFF_WAIT_SECONDS > 0
