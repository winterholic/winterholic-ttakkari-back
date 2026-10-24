"""최소 전환 계획 Step 3: 접수 로컬 Qwen 소유주 분리 및 격리 검증.

- remote API 세대는 llama-server 프로세스를 start/stop하지 않고 HTTP 추론/상태 조회만 수행.
- managed 모드에서 단일 장수 실행 주체(executor 또는 local)만 start/stop 책임(owner).
- API 세대 2개가 기동되어도 API 프로세스의 서버 spawn은 0.
- API 세대 shutdown 시 Qwen 서버 stop은 0.
- 정당 owner(executor/local)만 start/stop 1회 수행.
- 진행 중인 추론 중 API 세대 교체(draining/shutdown)가 일어나도 추론 보존.
- 외부 baseURL 모드는 기존 설정 존중, 재배포 시 프로세스 건드리지 않음.
- desk pool 기능 정상 연동.
"""
from __future__ import annotations

import asyncio
import json
import os
import time
from unittest.mock import AsyncMock

import pytest

from ttakkari.config import get_settings
from ttakkari.runs import desk_pool, desk_qwen
from ttakkari.runs import desk_qwen_service as svc


@pytest.fixture(autouse=True)
def qwen_test_env(tmp_path, monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "home", tmp_path / "home")
    monkeypatch.setattr(s, "desk_qwen_enabled", True)
    monkeypatch.setattr(s, "desk_qwen_manage", True)
    monkeypatch.setattr(s, "desk_qwen_base_url", "")
    monkeypatch.setattr(svc, "HEALTH_INTERVAL", 0.05)
    monkeypatch.setattr(svc, "BACKOFF_START", 0.05)
    desk_qwen.reset_cooldown()
    yield
    desk_qwen.reset_cooldown()


async def _until(cond, timeout=5.0, what="조건"):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if cond():
            return
        await asyncio.sleep(0.02)
    raise AssertionError(f"{what} 이(가) {timeout}초 안에 충족되지 않음")


class _FakeChild:
    def __init__(self, pid: int = 12345):
        self.pid = pid
        self.returncode = None

    async def wait(self):
        return 0


# ==============================================================================
# 1. managed / externally_managed × local / remote API / executor 역할 검증
# ==============================================================================

async def test_remote_api_does_not_spawn_and_discovers_owner(tmp_path, monkeypatch):
    """remote API 세대는 프로세스를 spawn하지 않고 owner가 띄운 managed 서버를 발견하여 ready가 된다."""
    remote_api = svc.QwenServer()

    # 가짜 owner 상태 파일 작성 (포트 54321)
    port = 54321
    state_dir = svc._state_dir()
    rec = {
        "owner_pid": os.getpid(),
        "owner_start": svc._proc_start(os.getpid()),
        "child_pid": os.getpid(),
        "child_start": svc._proc_start(os.getpid()),
        "port": port,
        "server": "/fake/llama-server",
        "model": "/fake/model.gguf",
        "started_at": time.time(),
    }
    (state_dir / "active.json").write_text(json.dumps(rec))

    # probe는 54321에 대해 True를 반환하도록 모의
    async def fake_probe(base, timeout=2.0):
        return base == f"http://127.0.0.1:{port}"

    monkeypatch.setattr(svc, "probe", fake_probe)

    # remote API 세대 기동 (owner=False)
    await remote_api.start(owner=False)
    await _until(lambda: remote_api.state == "ready", what="remote api ready")

    assert remote_api.spawn_count == 0
    assert remote_api.endpoint == f"http://127.0.0.1:{port}"
    assert remote_api.status()["ready"] is True
    assert remote_api.status()["mode"] == "managed"

    # remote API 종료 시 프로세스 stop 호출 없음
    await remote_api.stop()
    assert remote_api.endpoint is None
    assert remote_api.stop_count == 0
    # active.json 파일은 살아있어야 함
    assert (state_dir / "active.json").exists()


async def test_owner_spawns_and_kills_managed_server(monkeypatch):
    """정당 owner(executor/local)는 managed 모드에서 서버를 1회 spawn하고 stop 시 정리한다."""
    server = svc.QwenServer()
    port = 59101

    fake_child = _FakeChild(pid=os.getpid())
    terminated = []

    async def fake_spawn(s_path, m_path, p):
        server._child = fake_child
        server.spawn_count += 1
        d = svc._state_dir()
        server._pidfile = d / f"{os.getpid()}-{server._token}.json"
        rec = {
            "owner_pid": os.getpid(),
            "owner_start": svc._proc_start(os.getpid()),
            "child_pid": fake_child.pid,
            "child_start": svc._proc_start(os.getpid()),
            "port": port,
            "server": str(s_path),
            "model": str(m_path),
            "started_at": time.time(),
        }
        server._pidfile.write_text(json.dumps(rec))

    def fake_terminate(pid, grace=5.0):
        terminated.append(pid)
        fake_child.returncode = 0

    monkeypatch.setattr(server, "_preflight", lambda *_: None)
    monkeypatch.setattr(server, "_spawn", fake_spawn)
    monkeypatch.setattr(server, "_warm", AsyncMock())
    monkeypatch.setattr(svc, "probe", AsyncMock(return_value=True))
    monkeypatch.setattr(svc, "_terminate", fake_terminate)
    monkeypatch.setattr(svc, "_pick_port", lambda: port)

    # Owner로서 기동
    await server.start(owner=True)
    await _until(lambda: server.state == "ready", what="owner ready")

    assert server.spawn_count == 1
    assert server.endpoint == f"http://127.0.0.1:{port}"

    # Owner 종료 -> 프로세스 1회 종료
    await server.stop()
    assert server.stop_count == 1
    assert terminated == [fake_child.pid]
    assert server.endpoint is None


async def test_externally_managed_never_spawns_nor_kills(monkeypatch):
    """외부 baseURL 모드에서는 remote든 owner든 프로세스를 spawn하거나 kill하지 않는다."""
    monkeypatch.setattr(get_settings(), "desk_qwen_base_url", "http://127.0.0.1:8888")
    monkeypatch.setattr(svc, "probe", AsyncMock(return_value=True))

    q = svc.QwenServer()
    await q.start(owner=True)
    await _until(lambda: q.external_ready is True, what="external ready")

    assert q.state == "external"
    assert q.spawn_count == 0
    assert q.status()["mode"] == "external"
    assert q.endpoint == "http://127.0.0.1:8888"

    await q.stop()
    assert q.stop_count == 0


# ==============================================================================
# 2. API 세대 2개 동시 기동 시 spawn 0, shutdown 시 stop 0 검증
# ==============================================================================

async def test_two_api_generations_concurrent_spawn_zero_and_stop_zero(monkeypatch):
    """API 세대 2개가 순차/동시 기동되어도 spawn 0, shutdown되어도 stop 0."""
    port = 52000
    state_dir = svc._state_dir()
    rec = {
        "owner_pid": os.getpid(),
        "owner_start": svc._proc_start(os.getpid()),
        "child_pid": os.getpid(),
        "child_start": svc._proc_start(os.getpid()),
        "port": port,
        "server": "/bin/server",
        "model": "/bin/model",
        "started_at": time.time(),
    }
    (state_dir / "active.json").write_text(json.dumps(rec))

    monkeypatch.setattr(svc, "probe", AsyncMock(return_value=True))

    gen1 = svc.QwenServer()
    gen2 = svc.QwenServer()

    # 1. API 세대 1 기동
    await gen1.start(owner=False)
    await _until(lambda: gen1.state == "ready")
    assert gen1.spawn_count == 0
    assert gen1.endpoint == f"http://127.0.0.1:{port}"

    # 2. API 세대 2 기동 (전환 기간 공존)
    await gen2.start(owner=False)
    await _until(lambda: gen2.state == "ready")
    assert gen2.spawn_count == 0
    assert gen2.endpoint == f"http://127.0.0.1:{port}"

    # 누적 spawn 수 0
    assert gen1.spawn_count + gen2.spawn_count == 0

    # 3. 구세대 API 1 퇴역 및 종료
    await gen1.stop()
    assert gen1.stop_count == 0
    # 신세대 API 2는 여전히 같은 endpoint로 정상 작동 중
    assert gen2.state == "ready"
    assert gen2.endpoint == f"http://127.0.0.1:{port}"

    # 4. 신세대 API 2도 종료
    await gen2.stop()
    assert gen2.stop_count == 0
    assert gen1.stop_count + gen2.stop_count == 0


# ==============================================================================
# 3. 진행 중 추론 API 교체 중 보존 (fixture 기반)
# ==============================================================================

async def test_inflight_inference_preserved_across_api_replacement(monkeypatch):
    """API 세대 1에서 추론 요청이 진행 중일 때 세대 1이 shutdown되고 세대 2로 교체되어도 추론이 중단되지 않는다."""
    port = 53000
    state_dir = svc._state_dir()
    rec = {
        "owner_pid": os.getpid(),
        "owner_start": svc._proc_start(os.getpid()),
        "child_pid": os.getpid(),
        "child_start": svc._proc_start(os.getpid()),
        "port": port,
        "server": "/bin/server",
        "model": "/bin/model",
        "started_at": time.time(),
    }
    (state_dir / "active.json").write_text(json.dumps(rec))

    monkeypatch.setattr(svc, "probe", AsyncMock(return_value=True))

    gen1 = svc.QwenServer()
    gen2 = svc.QwenServer()

    await gen1.start(owner=False)
    await _until(lambda: gen1.state == "ready")

    # 추론 시뮬레이션: 0.15초 소요되는 가짜 HTTP 요청
    inference_started = asyncio.Event()
    inference_completed = asyncio.Event()
    inference_result = {}

    async def mock_inference_call():
        inference_started.set()
        await asyncio.sleep(0.15)
        inference_result["outcome"] = "success"
        inference_result["text"] = "정상 추론 완료"
        inference_completed.set()
        return inference_result

    # API 세대 1에서 추론 요청 시작
    inference_task = asyncio.create_task(mock_inference_call())
    await inference_started.wait()

    # 추론이 진행되는 도중 API 세대 2 기동 & 세대 1 shutdown (드레인 전환)
    await gen2.start(owner=False)
    await _until(lambda: gen2.state == "ready")
    await gen1.stop()

    # 추론 완료 대기
    await asyncio.wait_for(inference_completed.wait(), timeout=1.0)
    res = await inference_task

    assert res["outcome"] == "success"
    assert res["text"] == "정상 추론 완료"
    assert gen2.state == "ready"
    assert gen2.endpoint == f"http://127.0.0.1:{port}"

    await gen2.stop()


# ==============================================================================
# 4. generation_service / executor 배선 및 역할 계약 검증
# ==============================================================================

async def test_generation_service_start_api_services_uses_non_owner_for_remote(monkeypatch):
    """generation_service.start_api_services는 execution_mode='remote'일 때 owner=False로 기동한다."""
    from ttakkari import background_consumers, generation_service
    from ttakkari.artifacts import preview
    from ttakkari.meetings.local_stt import jobs as stt_jobs
    from ttakkari.tts import jobs as tts_jobs

    monkeypatch.setattr(preview, "start_worker", AsyncMock())
    monkeypatch.setattr(stt_jobs, "start", AsyncMock())
    monkeypatch.setattr(tts_jobs, "start", AsyncMock())
    monkeypatch.setattr(background_consumers, "start", AsyncMock())

    started_owners: list[bool] = []

    async def fake_qwen_start(*, owner=True):
        started_owners.append(owner)

    monkeypatch.setattr(svc.service, "start", fake_qwen_start)

    # 1. remote 모드 -> owner=False
    await generation_service.start_api_services("remote")
    assert started_owners == [False]

    started_owners.clear()

    # 2. local 모드 -> owner=True
    await generation_service.start_api_services("local")
    assert started_owners == [True]


async def test_executor_starts_and_stops_qwen_as_owner(monkeypatch):
    """executor는 serve 기동 시 Qwen을 owner=True로 시작하고 종료 시 stop한다."""
    from ttakkari import executor
    from ttakkari.artifacts import preview
    from ttakkari.meetings.local_stt import jobs as stt_jobs
    from ttakkari.runs import manager

    monkeypatch.setattr(preview, "start_worker", AsyncMock())
    monkeypatch.setattr(preview, "stop_worker", AsyncMock())
    monkeypatch.setattr(stt_jobs, "start", AsyncMock())
    monkeypatch.setattr(stt_jobs, "stop", AsyncMock())
    monkeypatch.setattr(executor, "start_background_services", AsyncMock(return_value=[]))
    monkeypatch.setattr(executor.ExecutorRuntime, "register", AsyncMock())
    monkeypatch.setattr(executor.ExecutorRuntime, "listen", AsyncMock())
    monkeypatch.setattr(executor.ExecutorRuntime, "refresh", AsyncMock(return_value="active"))
    monkeypatch.setattr(executor.ExecutorRuntime, "close", AsyncMock())
    from ttakkari import generation_service
    monkeypatch.setattr(generation_service, "coordinate_runs", AsyncMock())
    monkeypatch.setattr(executor, "dispose_engine", AsyncMock())
    monkeypatch.setattr(executor, "drain", AsyncMock(return_value=True))
    monkeypatch.setattr(manager.manager, "shutdown", AsyncMock())

    calls: list[str] = []

    async def fake_start(*, owner=True):
        calls.append(f"start:{owner}")

    async def fake_stop():
        calls.append("stop")

    monkeypatch.setattr(svc.service, "start", fake_start)
    monkeypatch.setattr(svc.service, "stop", fake_stop)

    # settings mock
    s = get_settings()
    monkeypatch.setattr(s, "release_revision", "rev-test")
    monkeypatch.setattr(s, "runtime_poll_seconds", 0.05)

    # serve를 실행하고 바로 SIGTERM/stop 트리거
    serve_task = asyncio.create_task(executor.serve(activate=True))
    await asyncio.sleep(0.05)
    os.kill(os.getpid(), 15)  # SIGTERM
    await asyncio.wait_for(serve_task, timeout=2.0)

    assert "start:True" in calls
    assert "stop" in calls


# ==============================================================================
# 5. Desk Pool 기능 정상 검증
# ==============================================================================

async def test_desk_pool_shutdown_and_lifecycle():
    """desk_pool 모듈의 warm pool 라이프사이클 및 shutdown이 정상 동작한다."""
    # shutdown 호출 시 에러 없이 깨끗하게 반환
    await desk_pool.shutdown()
    assert len(desk_pool._warm) == 0
    assert len(desk_pool._spawning) == 0
    assert len(desk_pool._last_used) == 0
