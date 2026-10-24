"""Tests for remote API residual execution paths (ArtCraft and Google Account Login).

Platform V2 integration:
- During API drain, new execution starts are rejected with 503 + Retry-After.
- In-flight jobs prevent drain_ready until finished.
- In-flight jobs drain within bounded timeout or get cleaned up safely.
- Normal execution (active generation or standalone local/no runtime) remains unaffected.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI

from ttakkari import artcraft, generation, generation_service
from ttakkari.api import artcraft as artcraft_api
from ttakkari.api import google_accounts as google_api
from ttakkari.api.deps import Db
from ttakkari.google_account import login
from ttakkari.security.auth import require_user


class _DrainingRuntime:
    def __init__(self) -> None:
        self.state = "draining"
        self.http_inflight = 0
        self._work: set[str] = set()
        self._accepted_work: dict = {}
        self._listener = SimpleNamespace(is_closed=lambda: False)

    async def enter_admission(self, kind: str = "http") -> bool:
        return False

    async def refresh(self) -> str:
        return self.state

    async def snapshot(self) -> dict:
        return {"state": self.state}

    async def can_retire(self) -> bool:
        return True


class _ActiveRuntime:
    def __init__(self) -> None:
        self.state = "active"
        self.http_inflight = 0
        self._work: set[str] = set()
        self._accepted_work: dict = {}
        self._listener = SimpleNamespace(is_closed=lambda: False)

    async def enter_admission(self, kind: str = "http") -> bool:
        return True

    async def refresh(self) -> str:
        return self.state

    async def snapshot(self) -> dict:
        return {"state": self.state}

    async def can_retire(self) -> bool:
        return False


@pytest.fixture(autouse=True)
def _clean_state():
    orig_runtime = generation.current
    orig_artcraft_jobs = dict(artcraft.JOBS)
    artcraft.JOBS.clear()
    login.manager._states.clear()
    login.manager._tasks.clear()
    login.manager._cancel.clear()
    login.manager._complete.clear()
    yield
    artcraft.JOBS.clear()
    artcraft.JOBS.update(orig_artcraft_jobs)
    login.manager._states.clear()
    login.manager._tasks.clear()
    login.manager._cancel.clear()
    login.manager._complete.clear()
    generation.current = orig_runtime


@pytest.fixture
def artcraft_client():
    app = FastAPI()
    app.include_router(artcraft_api.router)
    app.dependency_overrides[require_user] = lambda: "tester"
    app.dependency_overrides[Db] = lambda: AsyncMock()
    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(transport=transport, base_url="http://test")


@pytest.fixture
def google_client():
    app = FastAPI()
    google_api.install(app)
    app.include_router(google_api.router)
    app.dependency_overrides[google_api.user_access] = lambda: None
    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(transport=transport, base_url="http://test")


# ==============================================================================
# 1. Drain 중 새 시작 요청 명시적 거절 (503 + Retry-After)
# ==============================================================================

async def test_artcraft_remote_check_rejected_with_503_during_drain(artcraft_client):
    generation.current = _DrainingRuntime()
    resp = await artcraft_client.post("/api/artcraft/remote-check")
    assert resp.status_code == 503
    assert resp.headers.get("Retry-After") == "5"
    assert resp.json()["detail"]["code"] == "draining"


async def test_artcraft_smoke_rejected_with_503_during_drain(artcraft_client):
    generation.current = _DrainingRuntime()
    resp = await artcraft_client.post("/api/artcraft/smoke", json={"apps": ["app1"], "mode": "cli"})
    assert resp.status_code == 503
    assert resp.headers.get("Retry-After") == "5"
    assert resp.json()["detail"]["code"] == "draining"


async def test_artcraft_gui_rejected_with_503_during_drain(artcraft_client):
    generation.current = _DrainingRuntime()
    resp = await artcraft_client.post("/api/artcraft/apps/app1/gui", json={"action": "start"})
    assert resp.status_code == 503
    assert resp.headers.get("Retry-After") == "5"
    assert resp.json()["detail"]["code"] == "draining"


async def test_google_login_start_rejected_with_503_during_drain(google_client):
    generation.current = _DrainingRuntime()
    resp = await google_client.post("/api/google-accounts/test-acc/login", json={})
    assert resp.status_code == 503
    assert resp.headers.get("Retry-After") == "5"
    assert resp.json()["detail"]["code"] == "draining"


async def test_google_login_complete_and_cancel_not_503_during_drain(google_client, monkeypatch):
    generation.current = _DrainingRuntime()
    # complete & cancel 은 진행 중인 작업 정리 경로이므로 503으로 차단하지 않는다.
    monkeypatch.setattr(google_api.service, "complete_login", AsyncMock(return_value={"status": "completed"}))
    monkeypatch.setattr(google_api.service, "cancel_login", AsyncMock(return_value={"status": "cancelled"}))

    r_complete = await google_client.post("/api/google-accounts/test-acc/login/complete")
    assert r_complete.status_code == 200

    r_cancel = await google_client.post("/api/google-accounts/test-acc/login/cancel")
    assert r_cancel.status_code == 200


async def test_normal_requests_allowed_in_active_mode_and_no_runtime(artcraft_client, google_client, monkeypatch):
    # active runtime
    generation.current = _ActiveRuntime()
    fake_job = artcraft.Job(id="job1", kind="remote", action="check", apps=[])
    monkeypatch.setattr(artcraft, "start_remote_check", lambda **_kw: fake_job)
    resp = await artcraft_client.post("/api/artcraft/remote-check")
    assert resp.status_code == 202

    # standalone / no runtime
    generation.current = None
    monkeypatch.setattr(google_api.service, "start_login", AsyncMock(return_value={"state": "starting"}))
    resp = await google_client.post("/api/google-accounts/test-acc/login", json={})
    assert resp.status_code == 202


# ==============================================================================
# 2. 진행 중 작업의 drain_ready 차단
# ==============================================================================

async def test_ready_drain_ready_is_blocked_by_inflight_artcraft_job():
    runtime = _DrainingRuntime()
    # 초기에 아무 작업 없을 때 drain_ready 는 True
    ready_val = await generation_service.ready(runtime)
    assert ready_val["drain_ready"] is True

    # active ArtCraft job 추가
    job = artcraft.Job(id="art1", kind="smoke", action="smoke-cli", apps=["test"], state="running")
    artcraft.JOBS[job.id] = job
    assert artcraft.inflight() == 1

    ready_val = await generation_service.ready(runtime)
    assert ready_val["drain_ready"] is False
    assert ready_val.get("api_residual", 0) >= 1

    # 작업 완료
    job.state = "succeeded"
    assert artcraft.inflight() == 0

    ready_val = await generation_service.ready(runtime)
    assert ready_val["drain_ready"] is True
    assert ready_val.get("api_residual", 0) == 0


async def test_ready_drain_ready_is_blocked_by_inflight_google_login():
    runtime = _DrainingRuntime()
    ready_val = await generation_service.ready(runtime)
    assert ready_val["drain_ready"] is True

    # 진행 중 로그인 태스크 등록
    release = asyncio.Event()

    async def _mock_task():
        await release.wait()

    t = asyncio.create_task(_mock_task())
    login.manager._tasks["acc1"] = t
    assert login.manager.inflight() == 1

    try:
        ready_val = await generation_service.ready(runtime)
        assert ready_val["drain_ready"] is False
        assert ready_val.get("api_residual", 0) >= 1
    finally:
        release.set()
        await t

    assert login.manager.inflight() == 0
    ready_val = await generation_service.ready(runtime)
    assert ready_val["drain_ready"] is True


# ==============================================================================
# 3. 진행 중 작업의 drain 상한 및 안전 정리
# ==============================================================================

async def test_artcraft_drain_waits_for_completion_and_respects_timeout():
    release = asyncio.Event()

    async def _runner():
        await release.wait()

    t = asyncio.create_task(_runner())
    job = artcraft.Job(id="art2", kind="smoke", action="smoke-cli", apps=["test"], state="running", task=t)
    artcraft.JOBS[job.id] = job

    # 1. timeout 내 미종료 시 cancel 후 False 반환
    drained = await artcraft.drain(timeout=0.02)
    assert drained is False
    assert t.cancelled() or t.done()

    # 2. 자연 완료 시 True 반환
    t2 = asyncio.create_task(asyncio.sleep(0.01))
    job2 = artcraft.Job(id="art3", kind="remote", action="check", apps=[], state="running", task=t2)
    artcraft.JOBS[job2.id] = job2
    drained2 = await artcraft.drain(timeout=1.0)
    assert drained2 is True


async def test_google_login_drain_waits_for_completion_and_respects_timeout():
    release = asyncio.Event()

    async def _login_runner():
        await release.wait()

    t = asyncio.create_task(_login_runner())
    login.manager._tasks["acc2"] = t

    # 1. timeout 내 미종료 시 shutdown 후 False 반환
    drained = await login.manager.drain(timeout=0.02)
    assert drained is False
    assert t.cancelled() or t.done()

    # 2. 자연 완료 시 True 반환
    login.manager._tasks.clear()
    t2 = asyncio.create_task(asyncio.sleep(0.01))
    login.manager._tasks["acc3"] = t2
    drained2 = await login.manager.drain(timeout=1.0)
    assert drained2 is True
