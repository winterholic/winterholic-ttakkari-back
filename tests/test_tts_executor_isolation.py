"""Step4: TTS 합성 실행부를 API 프로세스에서 executor 로 옮긴다.

remote API 는 queued + chunks_json 만 DB 에 남기고, 별도 executor consumer 가 같은 원문으로 합성한다.
가짜 엔진과 임시 PostgreSQL 만 쓴다. 실제 모델·GPU·서버는 띄우지 않는다.
"""
from __future__ import annotations

import asyncio
import uuid
from contextlib import asynccontextmanager
from datetime import timedelta

import pytest
from tts_helpers import insert_job, job_row, plan_for

from ttakkari import generation, generation_service
from ttakkari.config import get_settings
from ttakkari.db import sessionmaker
from ttakkari.executor import ExecutorRuntime
from ttakkari.generation import GenerationRuntime, activate
from ttakkari.models import utcnow
from ttakkari.tts import jobs, service
from ttakkari.tts_models import TtsJob

pytest_plugins = ("conftest_tts",)

TEXT = "executor 가 끝까지 합성해야 하는 문장입니다."


async def wait_for(predicate, timeout=5.0):
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if await predicate():
            return
        await asyncio.sleep(0.02)
    raise AssertionError("조건이 시간 안에 충족되지 않았다")


async def status_of(job_id) -> str:
    return (await job_row(job_id)).status


def _is(job_id, status):
    async def check():
        return await status_of(job_id) == status

    return check


@pytest.fixture(autouse=True)
def fast_poll(monkeypatch):
    monkeypatch.setattr(get_settings(), "runtime_poll_seconds", 0.05)


@asynccontextmanager
async def remote_api_and_executor(tmp_path):
    """운영 배선과 같다: remote API 가 active 이고 executor 가 active 다. generation.current 는 호출자가 고른다."""
    api = GenerationRuntime(tmp_path, revision="a" * 40, api_url="http://127.0.0.1:18821")
    api.execution_mode = "remote"
    executor = ExecutorRuntime(tmp_path, revision="e" * 40)
    await api.register()
    await activate(api.id, expected=None)
    await executor.register(activate=True)
    try:
        yield api, executor
    finally:
        generation.current = None
        await jobs.stop()
        await executor.close()
        await api.close()


async def queue_job(provider, text: str, *, owner: uuid.UUID = jobs.QUEUE_OWNER, age_seconds: float = 0) -> uuid.UUID:
    """remote API 가 접수한 행과 같은 모양(queued + chunks_json)을 직접 만든다."""
    plan = plan_for(provider, text)
    job_id = uuid.uuid4()
    async with sessionmaker()() as db:
        db.add(TtsJob(id=job_id, owner_id=owner, status="queued", params=jobs._params(plan, cached=0),
                      chunks_json=list(plan.chunks), created_at=utcnow() - timedelta(seconds=age_seconds)))
        await db.commit()
    return job_id


# ---------------------------------------------------------------- API: 접수만


async def test_remote_api_persists_queued_text_and_never_starts_synthesis(tmp_path, tts_env, client, auth, monkeypatch):
    monkeypatch.setattr(get_settings(), "local_tts_enabled", True)  # 켜져 있어도 API 는 consumer 를 띄우지 않는다
    async with remote_api_and_executor(tmp_path) as (api, _executor):
        generation.current = api
        await jobs.start()
        r = await client.post("/api/tts/jobs", json={"text": TEXT}, headers=auth)
        assert r.status_code == 202 and r.json()["status"] == "queued"
        jid = uuid.UUID(r.json()["job_id"])
        row = await job_row(jid)
        assert (row.status, row.owner_id) == ("queued", jobs.QUEUE_OWNER)
        assert row.chunks_json == plan_for(tts_env, TEXT).chunks  # 원문은 프로세스 메모리가 아니라 DB 에 있다
        assert not jobs._pending_texts and jobs._task is None and jobs._guard_task is None
        jobs.kick()
        assert jobs._task is None  # kick 도 consumer 를 만들지 않는다
        assert tts_env.calls == []  # 합성 start 0
        await jobs.stop()  # API 종료는 queued job 을 건드리지 않는다
        assert await status_of(jid) == "queued"


async def test_remote_api_is_retirable_with_queued_job_and_executor_finishes_it(tmp_path, tts_env, client, auth):
    async with remote_api_and_executor(tmp_path) as (api, executor):
        generation.current = api
        jid = uuid.UUID((await client.post("/api/tts/jobs", json={"text": TEXT}, headers=auth)).json()["job_id"])
        new_api = GenerationRuntime(tmp_path, revision="b" * 40, api_url="http://127.0.0.1:18822")
        new_api.execution_mode = "remote"
        await new_api.register()
        try:
            await activate(new_api.id, expected=api.id)
            assert await api.refresh() == "draining"
            # 기수락 TTS 때문에 API 슬롯이 붙잡히지 않는다.
            assert await api.can_retire() is True
            ready = await generation_service.ready(api)
            assert ready["drain_ready"] is True and ready["finalizers"] == 0
            await api.close()  # 구 API 퇴역
            generation.current = new_api
            state = (await client.get(f"/api/tts/jobs/{jid}", headers=auth)).json()
            assert state["status"] == "queued"  # API 교체는 종료 사유가 아니다(owner_gone 아님)
            # 새 API 아래에서도 executor 가 접수 당시 원문 그대로 합성한다.
            generation.current = executor
            assert await jobs.tick() is True
            assert tts_env.calls == [plan_for(tts_env, TEXT).chunks]
            row = await job_row(jid)
            assert (row.status, row.owner_id) == ("succeeded", executor.id)
            generation.current = new_api
            done = (await client.get(f"/api/tts/jobs/{jid}", headers=auth)).json()
            assert done["status"] == "succeeded" and "audio_full_url" in done
            audio = await client.get(done["audio_full_url"])
            assert audio.status_code == 200 and audio.headers["content-type"] == "audio/wav"
        finally:
            await new_api.close()


async def test_cancel_queued_job_through_remote_api(tmp_path, tts_env, client, auth):
    async with remote_api_and_executor(tmp_path) as (api, executor):
        generation.current = api
        jid = uuid.UUID((await client.post("/api/tts/jobs", json={"text": TEXT}, headers=auth)).json()["job_id"])
        r = await client.delete(f"/api/tts/jobs/{jid}", headers=auth)
        assert r.json()["status"] == "cancelled"
        generation.current = executor
        assert await jobs.tick() is False and tts_env.calls == []


async def test_accept_notifies_executor_consumer(tmp_path, tts_env):
    async with remote_api_and_executor(tmp_path) as (api, _executor):
        generation.current = api
        await api.listen()
        await asyncio.sleep(0.1)
        jobs._wake.clear()
        await jobs.accept(plan_for(tts_env, TEXT))
        await wait_for(lambda: _wake_set())


async def _wake_set():
    return jobs._wake.is_set()


# ---------------------------------------------------------------- executor: FIFO 소비와 교체


async def test_executor_consumes_queue_in_fifo_order_one_at_a_time(tmp_path, tts_env):
    async with remote_api_and_executor(tmp_path) as (_api, executor):
        generation.current = executor
        newer = await queue_job(tts_env, "나중에 들어온 문장입니다.", age_seconds=1)
        older = await queue_job(tts_env, "먼저 들어온 문장입니다.", age_seconds=60)
        assert await jobs.tick() is True
        assert await status_of(older) == "succeeded" and await status_of(newer) == "queued"
        assert await jobs.tick() is True
        assert [c[0] for c in tts_env.calls] == ["먼저 들어온 문장입니다.", "나중에 들어온 문장입니다."]


async def test_executor_drain_stops_new_claims_finishes_inflight_and_keeps_queued(tmp_path, tts_env, monkeypatch):
    monkeypatch.setattr(get_settings(), "local_tts_enabled", True)
    tts_env.delay = 0.3
    async with remote_api_and_executor(tmp_path) as (_api, executor):
        generation.current = executor
        first = await queue_job(tts_env, "진행 중이던 문장입니다.", age_seconds=10)
        second = await queue_job(tts_env, "대기 중이던 문장입니다.", age_seconds=5)
        await jobs.start()
        await asyncio.wait_for(tts_env.started.wait(), 5)
        assert await status_of(first) == "running"
        await jobs.drain()
        assert await status_of(first) == "succeeded"  # 진행 중 1건은 끝까지 간다
        row = await job_row(second)
        assert (row.status, row.owner_id) == ("queued", jobs.QUEUE_OWNER)  # queued 는 DB 그대로
        assert len(tts_env.calls) == 1
        assert await jobs.tick() is False  # draining 중에는 새 claim 도 없다
        # 새 executor(consumer)가 같은 원문으로 이어서 처리한다.
        tts_env.delay = 0
        await jobs.start()
        await wait_for(_is(second, "succeeded"))
        assert tts_env.calls[1] == ["대기 중이던 문장입니다."]


async def test_executor_respects_tts_work_lock_budget(tmp_path, tts_env):
    async with remote_api_and_executor(tmp_path) as (_api, executor):
        generation.current = executor
        jid = await queue_job(tts_env, TEXT)
        held = generation._lock(executor.home / "runtime" / "work-tts.lock")  # 다른 합성(creative 등)이 예산을 쥐고 있다
        assert held is not None
        try:
            assert await jobs.tick() is False
            assert await status_of(jid) == "queued"
        finally:
            held.close()
        assert await jobs.tick() is True
        assert await status_of(jid) == "succeeded"


async def test_executor_defers_to_running_stt(tmp_path, tts_env, monkeypatch):
    monkeypatch.setattr(get_settings(), "local_tts_concurrency_policy", "defer_to_stt")
    stt = {"running": True}

    async def stt_running() -> bool:
        return stt["running"]

    monkeypatch.setattr(service, "_stt_running", stt_running)
    async with remote_api_and_executor(tmp_path) as (_api, executor):
        generation.current = executor
        jid = await queue_job(tts_env, TEXT)
        assert await jobs.tick() is False and await status_of(jid) == "queued"
        stt["running"] = False
        assert await jobs.tick() is True and await status_of(jid) == "succeeded"


async def test_executor_cancels_inflight_job_deleted_by_api(tmp_path, tts_env):
    tts_env.delay = 5
    async with remote_api_and_executor(tmp_path) as (_api, executor):
        generation.current = executor
        jid = await queue_job(tts_env, TEXT)
        worker = asyncio.create_task(jobs.tick())
        await asyncio.wait_for(tts_env.started.wait(), 5)
        assert await jobs.cas(jid, expected=("queued", "running"), status="cancelled")  # API 의 DELETE
        assert await asyncio.wait_for(worker, 5) is True
        assert await status_of(jid) == "cancelled"


# ---------------------------------------------------------------- 소유자 증거: 시간만으로 마감하지 않는다


async def test_running_job_of_alive_executor_is_not_closed_by_age(tmp_path, tts_env, client, auth):
    async with remote_api_and_executor(tmp_path) as (api, executor):
        generation.current = api
        timeout = get_settings().local_tts_timeout_seconds
        old = timeout + jobs.STALE_RUNNING_GRACE_SECONDS + 600
        jid = await insert_job(owner=executor.id, status="running", age_seconds=old)
        assert (await client.get(f"/api/tts/jobs/{jid}", headers=auth)).json()["status"] == "running"
        await jobs._housekeeping(api.id)
        assert await status_of(jid) == "running"


async def test_running_job_of_dead_executor_is_closed_only_on_death_evidence(tmp_path, tts_env, monkeypatch):
    async with remote_api_and_executor(tmp_path) as (api, executor):
        generation.current = api
        queued = await queue_job(tts_env, TEXT)
        running = await insert_job(owner=executor.id, status="running")
        monkeypatch.setattr(jobs, "DEAD_GRACE_SECONDS", 0.0)
        await jobs._housekeeping(api.id)
        assert await status_of(running) == "running"  # 아직 증거 없음
        await executor.close()  # offline 은 증거다
        await jobs._housekeeping(api.id)
        row = await job_row(running)
        assert (row.status, row.error_code) == ("failed", "owner_gone")
        assert await status_of(queued) == "queued"  # queued 는 소유 세대가 없으므로 계속 보존된다


# ---------------------------------------------------------------- local 모드·롤백 호환


async def test_local_api_adopts_queued_jobs_when_active_for_rollback(tmp_path, tts_env):
    local = GenerationRuntime(tmp_path, revision="l" * 40, api_url="http://127.0.0.1:18823")
    await local.register()
    await activate(local.id, expected=None)
    generation.current = local
    try:
        jid = await queue_job(tts_env, TEXT)
        assert await jobs.tick() is True
        row = await job_row(jid)
        assert (row.status, row.owner_id) == ("succeeded", local.id)
        assert tts_env.calls == [plan_for(tts_env, TEXT).chunks]
    finally:
        generation.current = None
        await jobs.stop()
        await local.close()


async def test_draining_local_api_does_not_adopt_queue(tmp_path, tts_env):
    local = GenerationRuntime(tmp_path, revision="l" * 40, api_url="http://127.0.0.1:18824")
    successor = GenerationRuntime(tmp_path, revision="m" * 40, api_url="http://127.0.0.1:18825")
    await local.register()
    await successor.register()
    await activate(local.id, expected=None)
    generation.current = local
    try:
        jid = await queue_job(tts_env, TEXT)
        await activate(successor.id, expected=local.id)
        assert await local.refresh() == "draining"
        assert await jobs.tick() is False  # 퇴역 중인 세대가 queue 를 끌어오면 퇴역이 늦어진다
        assert await status_of(jid) == "queued"
        assert await local.can_retire() is True  # local 모드의 소유 기준은 그대로다(소유 job 이 없다)
    finally:
        generation.current = None
        await jobs.stop()
        await local.close()
        await successor.close()


async def test_local_api_still_blocks_retirement_for_owned_jobs(tmp_path, tts_env):
    local = GenerationRuntime(tmp_path, revision="l" * 40, api_url="http://127.0.0.1:18826")
    successor = GenerationRuntime(tmp_path, revision="m" * 40, api_url="http://127.0.0.1:18827")
    await local.register()
    await successor.register()
    await activate(local.id, expected=None)
    generation.current = local
    try:
        await insert_job(owner=local.id, status="queued")
        await activate(successor.id, expected=local.id)
        assert await local.can_retire() is False  # 구버전/로컬 전환 보호는 유지한다
    finally:
        generation.current = None
        await local.close()
        await successor.close()


async def test_legacy_single_process_start_keeps_queue_owned_jobs(tts_env, monkeypatch):
    stale = await insert_job(owner=uuid.uuid4(), status="queued")
    kept = await queue_job(tts_env, TEXT)
    monkeypatch.setattr(get_settings(), "local_tts_enabled", True)
    assert generation.current is None
    await jobs.start()
    try:
        await wait_for(_is(kept, "succeeded"))
        row = await job_row(stale)
        assert (row.status, row.error_code) == ("failed", "server_restarted")
    finally:
        await jobs.stop()
