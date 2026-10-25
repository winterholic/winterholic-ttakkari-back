"""local→remote 최초 전환의 TTS accepted work 경계(FND1).

accepted work 는 구세대가 tts_jobs 행을 커밋한 접수다. 커밋 전 503(앱의 generation_not_active)이나 앱에
도달하지 못한 요청은 accepted work 가 아니다. 구세대가 tts_admission 계약을 스스로 보고하면 접수 경계는
프록시 전송이 아니라 구세대 앱의 DB 상태 확인과 커밋이다. activate 가 그 경계를 닫고, 이미 커밋된 일은
구세대가 draining 중에 끝내며 drain_ready 가 그때까지 퇴역을 막는다. 계약을 보고하지 않는 구세대(23e0cbe 이하)는
기존 stock 거절을 그대로 받는다.

실제 Caddy 시험은 운영과 다른 격리 fixture다. request_buffers=16 으로 upstream 카운터 이전 본문 대기를
제어하고, 실제 main.app(InflightMiddleware·/api/ready·TTS route)을 uvicorn 으로 띄워 격리 PostgreSQL 을 쓴다.
운영 포트·프록시·launchctl·GPU 는 쓰지 않는다.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import os
import shutil
import socket
import uuid
from pathlib import Path

import httpx
import pytest
import pytest_asyncio
import uvicorn
from sqlalchemy import func, select
from tts_helpers import job_row, plan_for

from ttakkari import generation, generation_service
from ttakkari.config import get_settings
from ttakkari.db import sessionmaker
from ttakkari.deployment import JobStore
from ttakkari.generation import GenerationRuntime, activate
from ttakkari.generation_deployment import (
    GenerationController,
    Slots,
    StageError,
    proxy_config,
)
from ttakkari.tts import cache, jobs
from ttakkari.tts_models import TtsJob

pytest_plugins = ("conftest_tts",)

FORBIDDEN_PORTS = {8787, 8791, 8792, 8793, 20191}


@pytest_asyncio.fixture(loop_scope="session")
async def two_generations(tmp_path, tts_env, monkeypatch):
    monkeypatch.setattr(get_settings(), "runtime_poll_seconds", 0.05)
    a = GenerationRuntime(tmp_path, revision="a" * 40, api_url="http://127.0.0.1:18821")
    b = GenerationRuntime(tmp_path, revision="b" * 40, api_url="http://127.0.0.1:18822")
    await a.register()
    await b.register()
    await activate(a.id, expected=None)
    generation.current = a
    try:
        yield a, b
    finally:
        generation.current = None
        await jobs.stop()
        await a.close()
        await b.close()


class _FakeSlots:
    def __init__(self, snapshot):
        self.value, self.events = snapshot, []

    async def snapshot(self, slot):
        self.events.append("snapshot")
        return None if self.value is None else dict(self.value)

    async def close_tts_ingress(self, slot):
        self.events.append("closed")

    async def wait_tts_transport_drained(self, slot):
        return await Slots.wait_tts_transport_drained(self, slot)  # stock 거절 그대로

    async def switch(self, slot):
        self.events.append(f"switch:{slot}")


def _job(a, *, revision=None):
    return {"previous_slot": 1, "previous_generation": str(a.id),
            "previous_revision": revision or a.revision}


async def _capable_snapshot(a):
    return await generation_service.ready(a) | {"ready": True}


async def _eventually_drain_ready(runtime, seconds: float = 5.0) -> bool:
    """guard task 는 다음 poll 에 끝난다. 조건 관측 대기일 뿐 시간으로 안전을 판정하지 않는다."""
    for _ in range(int(seconds / 0.05)):
        if (await generation_service.ready(runtime))["drain_ready"]:
            return True
        await asyncio.sleep(0.05)
    return False


async def _count_jobs() -> int:
    async with sessionmaker()() as db:
        return int(await db.scalar(select(func.count(TtsJob.id))))


# ---------------------------------------------------------------- 컨트롤러 판정


async def test_ready_reports_the_admission_contract_of_this_process(two_generations):
    a, _ = two_generations
    ready = await generation_service.ready(a)
    assert ready["tts_admission"] == jobs.ADMISSION_CONTRACT
    assert ready["generation"] == str(a.id) and ready["revision"] == a.revision


async def test_capable_local_predecessor_needs_no_proxy_transport_ack(two_generations, tmp_path):
    a, _ = two_generations
    slots = _FakeSlots(await _capable_snapshot(a))
    await GenerationController(JobStore(tmp_path), slots=slots).drain_previous_tts(_job(a), timeout=1, poll=0.01)
    assert slots.events == ["snapshot"]  # 프록시를 닫지도, 전송 ack 를 기다리지도 않는다
    assert await a.refresh() == "active"  # activate 는 호출자 몫


@pytest.mark.parametrize("case", ["missing", "unknown-contract", "revision-mismatch", "no-recorded-revision"])
async def test_unproven_admission_contract_keeps_the_stock_refusal(two_generations, tmp_path, case):
    a, _ = two_generations
    snapshot = await _capable_snapshot(a)
    job = _job(a)
    if case == "missing":
        snapshot.pop("tts_admission")  # 23e0cbe 와 같은 구세대
    elif case == "unknown-contract":
        snapshot["tts_admission"] = "generation-state-v0"
    elif case == "revision-mismatch":
        job = _job(a, revision="c" * 40)
    else:
        job.pop("previous_revision")
    slots = _FakeSlots(snapshot)
    with pytest.raises(StageError) as raised:
        await GenerationController(JobStore(tmp_path), slots=slots).drain_previous_tts(job, timeout=1, poll=0.01)
    assert raised.value.stage == "tts-drain"
    assert "transport completion unavailable" in str(raised.value)
    assert slots.events == ["snapshot", "closed", "switch:1"]  # 닫았으면 반드시 원래 route 로 복원


@pytest.mark.parametrize("case", ["unreachable", "other-generation"])
async def test_identity_change_refuses_before_touching_the_proxy(two_generations, tmp_path, case):
    a, b = two_generations
    snapshot = None if case == "unreachable" else await _capable_snapshot(b)
    slots = _FakeSlots(snapshot)
    with pytest.raises(StageError, match="unreachable or changed"):
        await GenerationController(JobStore(tmp_path), slots=slots).drain_previous_tts(_job(a), timeout=1, poll=0.01)
    assert slots.events == ["snapshot"]


async def test_cancelled_decision_writes_nothing(two_generations, tmp_path):
    a, _ = two_generations
    started, release = asyncio.Event(), asyncio.Event()

    class _Slow(_FakeSlots):
        async def snapshot(self, slot):
            started.set()
            await release.wait()
            return await super().snapshot(slot)

    slots = _Slow(await _capable_snapshot(a))
    task = asyncio.create_task(
        GenerationController(JobStore(tmp_path), slots=slots).drain_previous_tts(_job(a), timeout=1, poll=0.01))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert slots.events == [] and await a.refresh() == "active"


# ---------------------------------------------------------------- 구세대가 보장하는 계약(실제 accept)


async def test_commit_racing_activation_stays_owned_and_completes_before_retirement(two_generations, tts_env,
                                                                                     monkeypatch):
    """active 확인 → (조회 중) activate → 커밋. 커밋된 일은 draining 구세대 소유로 남아 퇴역 전에 끝난다."""
    a, b = two_generations
    checked, gate = asyncio.Event(), asyncio.Event()
    lookup = cache.lookup_many

    async def slow_lookup(db, keys, **kwargs):
        checked.set()
        await gate.wait()
        return await lookup(db, keys, **kwargs)

    monkeypatch.setattr(cache, "lookup_many", slow_lookup)
    post = asyncio.create_task(jobs.accept(plan_for(tts_env, "활성 확인 뒤 커밋되는 문장입니다.")))
    await checked.wait()
    await activate(b.id, expected=a.id)
    gate.set()
    created = await post
    row = await job_row(uuid.UUID(created["job_id"]))
    assert (row.status, row.owner_id, row.chunks_json) == ("queued", a.id, ["활성 확인 뒤 커밋되는 문장입니다."])
    assert await a.can_retire() is False
    assert await jobs.tick() is True
    assert (await job_row(row.id)).status == "succeeded"
    assert await a.can_retire() is True


async def test_draining_predecessor_rejects_before_any_commit(two_generations, tts_env):
    a, b = two_generations
    await activate(b.id, expected=a.id)
    with pytest.raises(Exception) as raised:
        await jobs.accept(plan_for(tts_env, "전환 뒤 도착한 문장입니다."))
    assert getattr(raised.value, "code", None) == "generation_not_active"
    assert await _count_jobs() == 0


# ---------------------------------------------------------------- 실제 Caddy + 실제 앱


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class _Rig:
    """격리 Caddy(자식 프로세스) → uvicorn(main.app) → 격리 PostgreSQL."""

    def __init__(self, root: Path, upstream: int, admin: int, public: int):
        self.root, self.upstream, self.admin, self.public = root, upstream, admin, public
        self.proc = None
        self.writes: list[str] = []

    def config(self, closed=False):
        value = proxy_config(0, tts_ingress_closed=closed)
        value["admin"]["listen"] = f"127.0.0.1:{self.admin}"
        server = value["apps"]["http"]["servers"]["ttakkari"]
        server["listen"] = [f"127.0.0.1:{self.public}"]
        handler = server["routes"][-1]["handle"][0]
        handler["upstreams"] = [{"dial": f"127.0.0.1:{self.upstream}"}]
        handler["request_buffers"] = 16  # upstream countRequest 이전 대기를 관측 가능하게 만드는 fixture 차이
        return value

    async def load(self, closed):
        async with httpx.AsyncClient(timeout=5, trust_env=False) as client:
            (await client.post(f"http://127.0.0.1:{self.admin}/load", json=self.config(closed))).raise_for_status()
        self.writes.append("closed" if closed else "open")

    async def tts_closed(self) -> bool:
        async with httpx.AsyncClient(timeout=5, trust_env=False) as client:
            response = await client.get(f"http://127.0.0.1:{self.admin}/config/")
            response.raise_for_status()
        return response.json()["apps"]["http"]["servers"]["ttakkari"]["routes"][0]["handle"][0].get(
            "status_code") == 503

    def slots(self):
        rig = self

        class _RigSlots(Slots):
            def origin(self, slot):
                return f"http://127.0.0.1:{rig.upstream}"

            async def close_tts_ingress(self, slot):
                await rig.load(True)

            async def switch(self, slot):
                await rig.load(False)

        return _RigSlots(self.root)


@pytest_asyncio.fixture(loop_scope="session")
async def rig(two_generations, tmp_path):
    caddy = shutil.which("caddy")
    if caddy is None:
        pytest.skip("caddy binary not installed; real proxy chain cannot be exercised")
    from ttakkari.main import app

    a, _ = two_generations
    await a.listen()  # /api/ready 의 ready 는 LISTEN 연결을 요구한다
    upstream, admin, public = _free_port(), _free_port(), _free_port()
    assert len({upstream, admin, public}) == 3 and not {upstream, admin, public} & FORBIDDEN_PORTS
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=upstream, lifespan="off",
                                           log_level="warning"))
    serving = asyncio.create_task(server.serve())
    rig = _Rig(tmp_path, upstream, admin, public)
    (tmp_path / "proxy.json").write_text(json.dumps(rig.config()))
    log = (tmp_path / "caddy.log").open("wb")
    try:
        for _ in range(200):
            if server.started:
                break
            await asyncio.sleep(0.02)  # 기동 대기일 뿐 안전 판정이 아니다
        assert server.started
        rig.proc = await asyncio.create_subprocess_exec(
            caddy, "run", "--config", str(tmp_path / "proxy.json"), stdout=log, stderr=log,
            env=dict(os.environ, XDG_CONFIG_HOME=str(tmp_path / "xdg-config"),
                     XDG_DATA_HOME=str(tmp_path / "xdg-data")))
        async with httpx.AsyncClient(timeout=2, trust_env=False) as client:
            for _ in range(200):
                assert rig.proc.returncode is None, (tmp_path / "caddy.log").read_text()
                with contextlib.suppress(httpx.ConnectError):
                    (await client.get(f"http://127.0.0.1:{admin}/config/")).raise_for_status()
                    break
                await asyncio.sleep(0.02)
            else:
                raise AssertionError("isolated Caddy readiness failed")
        yield rig
    finally:
        if rig.proc is not None and rig.proc.returncode is None:
            rig.proc.terminate()  # 이 시험이 만든 자식만
            try:
                await asyncio.wait_for(rig.proc.wait(), 5)
            except TimeoutError:
                rig.proc.kill()
                await rig.proc.wait()
        log.close()
        server.should_exit = True
        with contextlib.suppress(Exception):
            await asyncio.wait_for(serving, 10)


class _Held:
    """헤더와 Expect: 100-continue 만 보내 Caddy 의 본문 버퍼 단계(upstream 카운터 이전)에 붙잡은 POST."""

    def __init__(self, reader, writer, body: bytes):
        self.reader, self.writer, self.body = reader, writer, body

    @classmethod
    async def open(cls, port: int, token: str, text: str, *, complete_headers=True):
        body = json.dumps({"text": text}).encode()
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        head = (f"POST /api/tts/jobs HTTP/1.1\r\nHost: probe\r\nAuthorization: Bearer {token}\r\n"
                f"Content-Type: application/json\r\nContent-Length: {len(body)}\r\n").encode()
        held = cls(reader, writer, body)
        if not complete_headers:
            writer.write(head)  # 빈 줄이 없어 헤더가 끝나지 않았다
            await writer.drain()
            held.rest = b"\r\n"
            return held
        writer.write(head + b"Expect: 100-continue\r\n\r\n")
        await writer.drain()
        interim = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 5)
        assert interim.startswith(b"HTTP/1.1 100 Continue")  # 본문 읽기 단계 진입 확인(고정 sleep 아님)
        held.rest = b""
        return held

    async def finish(self) -> tuple[int, dict]:
        self.writer.write(self.rest + self.body)
        await self.writer.drain()
        while True:  # upstream 도 Expect 에 1xx 를 보낼 수 있다
            head = await asyncio.wait_for(self.reader.readuntil(b"\r\n\r\n"), 10)
            status = int(head.split(b" ", 2)[1])
            if status >= 200:
                break
        length = next((int(line.split(b":", 1)[1]) for line in head.split(b"\r\n")
                       if line.lower().startswith(b"content-length:")), 0)
        payload = await asyncio.wait_for(self.reader.readexactly(length), 10) if length else b"{}"
        self.writer.close()
        with contextlib.suppress(ConnectionError):
            await self.writer.wait_closed()
        return status, json.loads(payload)


def _token() -> str:
    from ttakkari.security.auth import issue_access_token
    return issue_access_token()[0]


async def _upstream_requests(rig) -> int:
    async with httpx.AsyncClient(timeout=5, trust_env=False) as client:
        response = await client.get(f"http://127.0.0.1:{rig.admin}/reverse_proxy/upstreams")
        response.raise_for_status()
    return sum(row["num_requests"] for row in response.json() if row["address"] == f"127.0.0.1:{rig.upstream}")


async def test_real_proxy_pre_counter_post_after_activation_is_refused_not_lost(rig, two_generations, tmp_path):
    a, b = two_generations
    held = await _Held.open(rig.public, _token(), "프록시 버퍼에 붙잡힌 문장입니다.")
    assert await _upstream_requests(rig) == 0 and a.http_inflight == 0 and await _count_jobs() == 0
    control = GenerationController(JobStore(tmp_path / "jobs"), slots=rig.slots())
    await control.drain_previous_tts(_job(a), timeout=2, poll=0.01)
    assert rig.writes == []  # 운영과 같은 proxy 조작 없이 판정
    await activate(b.id, expected=a.id)
    status, payload = await held.finish()
    assert (status, payload.get("code")) == (503, "generation_not_active"), payload
    assert await _count_jobs() == 0  # 커밋 전 거절: 클라이언트가 원문을 가진다
    assert (await generation_service.ready(a))["drain_ready"] is True


async def test_real_proxy_commit_before_activation_finishes_in_draining_predecessor(rig, two_generations, tmp_path,
                                                                                     monkeypatch):
    a, b = two_generations
    checked, gate = asyncio.Event(), asyncio.Event()
    lookup = cache.lookup_many

    async def slow_lookup(db, keys, **kwargs):
        checked.set()
        await gate.wait()
        return await lookup(db, keys, **kwargs)

    monkeypatch.setattr(cache, "lookup_many", slow_lookup)
    held = await _Held.open(rig.public, _token(), "활성 확인을 통과한 문장입니다.")
    control = GenerationController(JobStore(tmp_path / "jobs"), slots=rig.slots())
    await control.drain_previous_tts(_job(a), timeout=2, poll=0.01)
    finish = asyncio.create_task(held.finish())
    await asyncio.wait_for(checked.wait(), 10)  # 구세대 accept 가 active 를 확인했다
    await activate(b.id, expected=a.id)
    assert (await generation_service.ready(a))["drain_ready"] is False  # http_inflight
    gate.set()
    status, payload = await finish
    assert status == 202
    row = await job_row(uuid.UUID(payload["job_id"]))
    assert (row.status, row.owner_id) == ("queued", a.id)
    assert (await generation_service.ready(a))["drain_ready"] is False  # 소유 queued
    assert await jobs.tick() is True
    assert (await job_row(row.id)).status == "succeeded"
    assert await _eventually_drain_ready(a)


async def test_real_proxy_slow_headers_across_activation_never_commit(rig, two_generations, tmp_path):
    a, b = two_generations
    held = await _Held.open(rig.public, _token(), "헤더가 늦게 끝나는 문장입니다.", complete_headers=False)
    control = GenerationController(JobStore(tmp_path / "jobs"), slots=rig.slots())
    await control.drain_previous_tts(_job(a), timeout=2, poll=0.01)
    await activate(b.id, expected=a.id)
    status, payload = await held.finish()
    assert (status, payload.get("code")) == (503, "generation_not_active"), payload
    assert await _count_jobs() == 0


async def test_real_proxy_negative_control_without_contract_refuses_and_restores(rig, two_generations, tmp_path,
                                                                                 monkeypatch):
    """계약을 보고하지 않는 구세대(23e0cbe 형태): 기존 stock 거절과 route 복원, 붙잡힌 요청은 구세대에서 완료."""
    a, _ = two_generations
    original = generation_service.ready

    async def legacy_ready(runtime):
        value = await original(runtime)
        value.pop("tts_admission", None)
        return value

    monkeypatch.setattr(generation_service, "ready", legacy_ready)
    held = await _Held.open(rig.public, _token(), "계약 없는 구세대의 문장입니다.")
    control = GenerationController(JobStore(tmp_path / "jobs"), slots=rig.slots())
    with pytest.raises(StageError, match="transport completion unavailable"):
        await control.drain_previous_tts(_job(a), timeout=2, poll=0.01)
    assert rig.writes == ["closed", "open"] and await rig.tts_closed() is False
    assert await a.refresh() == "active"
    status, payload = await held.finish()
    assert status == 202
    assert await jobs.tick() is True
    assert (await job_row(uuid.UUID(payload["job_id"]))).status == "succeeded"


async def test_real_proxy_rollback_reopens_predecessor_admission(rig, two_generations, tmp_path):
    a, b = two_generations
    control = GenerationController(JobStore(tmp_path / "jobs"), slots=rig.slots())
    await control.drain_previous_tts(_job(a), timeout=2, poll=0.01)
    await activate(b.id, expected=a.id)
    refused = await _Held.open(rig.public, _token(), "전환 중 문장입니다.")
    assert (await refused.finish())[0] == 503
    await activate(a.id, expected=b.id)  # rollback: 같은 구세대가 다시 active
    again = await _Held.open(rig.public, _token(), "복원 뒤 문장입니다.")
    status, payload = await again.finish()
    assert status == 202
    assert await jobs.tick() is True
    assert (await job_row(uuid.UUID(payload["job_id"]))).status == "succeeded"
