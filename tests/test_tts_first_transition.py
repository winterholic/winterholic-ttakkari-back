"""첫 혼합 세대 전환의 TTS 보호(구세대 코드는 바꿀 수 없다).

"구세대 서빙 코드"는 917ea4b 의 tts/jobs.py(accept 가 active 를 한 번 확인한 뒤 조회·커밋, _guard_loop 가
active 가 아니면 queued 를 generation_draining 으로 실패)를 옮겨 적은 모델이다. 실제 구세대 바이너리를 띄운
측정이 아니다. DB 는 정상 conftest 의 격리 PostgreSQL 을 쓴다.
"""
from __future__ import annotations

import asyncio
import uuid

import pytest
from sqlalchemy import select, text, update

from ttakkari.db import sessionmaker
from ttakkari.deployment import JobStore
from ttakkari.generation import activate
from ttakkari.generation_deployment import (
    GenerationController,
    StageError,
    proxy_config,
)
from ttakkari.models import RuntimeControl, RuntimeGeneration
from ttakkari.tts_models import TtsJob


class _Slots:
    def __init__(self, old_id, inflight=0):
        self.old_id, self.inflight = old_id, inflight
        self.route, self.tts_closed, self.events = 0, None, []

    async def close_tts_ingress(self, slot):
        self.tts_closed = slot
        self.events.append("closed")

    async def wait_tts_transport_drained(self, slot):
        # Model-only completed transport; the real Slots implementation refuses.
        return True

    async def switch(self, slot):
        self.route, self.tts_closed = slot, None
        self.events.append(f"switch:{slot}")

    async def snapshot(self, slot):
        return {"generation": str(self.old_id), "http_inflight": self.inflight}


async def _seed():
    old_id, new_id = uuid.uuid4(), uuid.uuid4()
    async with sessionmaker()() as db:
        for gid, port, state in ((old_id, 18791, "active"), (new_id, 18792, "standby")):
            db.add(RuntimeGeneration(id=gid, revision=str(gid.int)[:1] * 40, role="api", protocol=1,
                                     api_url=f"http://127.0.0.1:{port}", pid=1, process_started_at=1.0, state=state))
        await db.flush()
        db.add(RuntimeControl(id=1, active_generation=old_id, executor_generation=old_id, epoch=1))
        await db.commit()
    return old_id, new_id


async def _state(gid):
    async with sessionmaker()() as db:
        return await db.scalar(select(RuntimeGeneration.state).where(RuntimeGeneration.id == gid))


async def _old_accept(owner, gate: asyncio.Event, checked: asyncio.Event, chunks):
    """917 accept(): active 를 한 번 확인하고, 조회(gate)를 기다린 뒤 queued 행을 커밋한다."""
    if await _state(owner) != "active":
        raise RuntimeError("503 generation_not_active")
    checked.set()
    await gate.wait()
    job_id = uuid.uuid4()
    async with sessionmaker()() as db:
        db.add(TtsJob(id=job_id, owner_id=owner, status="queued", params={}, chunks_json=chunks))
        await db.commit()
    return job_id


async def _old_guard_pass(owner):
    """917 _guard_loop 한 번: active 가 아니면 queued 를 실패시킨다."""
    if await _state(owner) != "active":
        async with sessionmaker()() as db:
            await db.execute(update(TtsJob).where(TtsJob.owner_id == owner, TtsJob.status == "queued").values(
                status="failed", error_code="generation_draining"))
            await db.commit()


async def _job_row(job_id):
    async with sessionmaker()() as db:
        row = await db.get(TtsJob, job_id)
        return row.status, row.error_code, row.chunks_json


def _controller(tmp_path, slots):
    return GenerationController(JobStore(tmp_path), slots=slots)


def _job(old_id):
    return {"previous_slot": 0, "previous_generation": str(old_id)}


async def test_race_loses_accepted_job_when_only_queue_is_checked_before_activate(tmp_path):
    """수정 전 순서의 재현(구세대 모델): 큐 0건 관측 → activate → 구 POST 커밋 → 구 guard 가 실패 처리."""
    old_id, new_id = await _seed()
    gate, checked = asyncio.Event(), asyncio.Event()
    post = asyncio.create_task(_old_accept(old_id, gate, checked, ["원문 청크"]))
    await checked.wait()  # POST 는 active 를 이미 확인했다
    async with sessionmaker()() as db:
        assert await db.scalar(text("select count(*) from tts_jobs where status='queued'")) == 0  # 대기 코드의 0건 관측
    await activate(new_id, expected=old_id)
    gate.set()
    job_id = await post  # 구 POST 는 성공 응답(202)을 받는다
    await _old_guard_pass(old_id)
    assert await _job_row(job_id) == ("failed", "generation_draining", ["원문 청크"])


async def test_drain_waits_for_inflight_post_then_queue_so_no_accepted_job_is_failed(tmp_path):
    old_id, new_id = await _seed()
    slots = _Slots(old_id, inflight=1)
    control = _controller(tmp_path, slots)
    gate, checked = asyncio.Event(), asyncio.Event()
    post = asyncio.create_task(_old_accept(old_id, gate, checked, ["원문 청크"]))
    await checked.wait()
    drain = asyncio.create_task(control.drain_previous_tts(_job(old_id), timeout=5, poll=0.02))
    await asyncio.sleep(0.15)
    assert slots.tts_closed == 0 and not drain.done()  # 진행 HTTP 1개가 남아 있는 동안 activate 로 못 간다
    gate.set()
    job_id = await post
    slots.inflight = 0  # 구세대 POST 응답 완료
    await asyncio.sleep(0.1)
    assert not drain.done()  # 큐에 접수된 job 이 남아 있다
    async with sessionmaker()() as db:  # 구세대 실행부가 active 인 동안 끝까지 처리한다
        await db.execute(update(TtsJob).where(TtsJob.id == job_id).values(status="succeeded"))
        await db.commit()
    await asyncio.wait_for(drain, 3)
    assert slots.tts_closed == 0  # activate 와 cutover 사이에도 새 접수는 503 재시도로 닫혀 있다
    await activate(new_id, expected=old_id)
    await _old_guard_pass(old_id)
    assert await _job_row(job_id) == ("succeeded", None, ["원문 청크"])
    await slots.switch(1)
    assert slots.tts_closed is None


async def test_drain_returns_immediately_when_previous_is_idle(tmp_path):
    old_id, _ = await _seed()
    slots = _Slots(old_id)
    await _controller(tmp_path, slots).drain_previous_tts(_job(old_id), timeout=5, poll=0.01)
    assert slots.events == ["closed"]


@pytest.mark.parametrize("case", ["inflight", "queued", "db_error", "unreachable", "identity"])
async def test_drain_refuses_activation_and_reopens_ingress_when_idle_is_unproven(tmp_path, monkeypatch, case):
    old_id, _ = await _seed()
    slots = _Slots(old_id, inflight=1 if case == "inflight" else 0)
    control = _controller(tmp_path, slots)
    if case == "queued":
        async with sessionmaker()() as db:
            db.add(TtsJob(owner_id=old_id, status="queued", params={}, chunks_json=["x"]))
            await db.commit()
    if case == "db_error":
        async def boom(_):
            raise RuntimeError("db down")
        monkeypatch.setattr(control, "_previous_tts_queued", boom)
    if case == "unreachable":
        async def gone(_):
            return None
        monkeypatch.setattr(slots, "snapshot", gone)
    if case == "identity":
        slots.old_id = uuid.uuid4()
    with pytest.raises(StageError) as raised:
        await control.drain_previous_tts(_job(old_id), timeout=0.2, poll=0.02)
    assert raised.value.stage == "tts-drain"
    if case in {"unreachable", "identity"}:
        assert slots.events == []  # close 전 identity 실패에는 프록시를 쓰지 않는다
    else:
        assert slots.events[-1] == "switch:0"
    assert slots.tts_closed is None  # 거절 상태를 남기지 않는다
    assert await _state(old_id) == "active"  # activate 하지 않았다


def test_proxy_config_closes_only_new_tts_job_posts_and_keeps_the_upstream_route_last():
    from ttakkari.generation_deployment import Slots
    open_config, closed = proxy_config(1), proxy_config(1, tts_ingress_closed=True)
    open_routes = open_config["apps"]["http"]["servers"]["ttakkari"]["routes"]
    routes = closed["apps"]["http"]["servers"]["ttakkari"]["routes"]
    assert routes[1:] == open_routes and len(routes) == len(open_routes) + 1
    first = routes[0]
    assert first["match"] == [{"method": ["POST"], "path": ["/api/tts/jobs", "/api/tts/jobs/"]}]
    assert first["handle"][0]["status_code"] == 503 and first["handle"][0]["headers"]["Retry-After"] == ["5"]
    assert Slots.config_slot(closed) == Slots.config_slot(open_config) == 1
