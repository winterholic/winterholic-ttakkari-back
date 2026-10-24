"""TTS 큐(설계 §8): CAS 경합, 세대별 claim, drain·퇴역 순서(queued guard), 소유자 사망 확인, 원문 수명."""
from __future__ import annotations

import asyncio
import uuid

import pytest
import pytest_asyncio
from sqlalchemy import select, text
from tts_helpers import insert_job, job_row, plan_for

from ttakkari import generation, generation_service
from ttakkari.config import get_settings
from ttakkari.db import sessionmaker
from ttakkari.generation import GenerationRuntime, activate
from ttakkari.runs.manager import _background
from ttakkari.tts import jobs, service
from ttakkari.tts_models import TtsJob

pytest_plugins = ("conftest_tts",)


async def wait_for(predicate, timeout=5.0):
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if await predicate():
            return
        await asyncio.sleep(0.02)
    raise AssertionError("조건이 시간 안에 충족되지 않았다")


async def status_of(job_id) -> str:
    return (await job_row(job_id)).status


# ---------------------------------------------------------------- terminal CAS (§8.3.2)


async def test_terminal_states_never_transition(tts_env):
    me = jobs.owner_id()
    for terminal in ("succeeded", "failed", "cancelled"):
        jid = await insert_job(owner=me, status=terminal)
        for target in ("queued", "running", "succeeded", "failed", "cancelled"):
            assert await jobs.cas(jid, expected=("queued", "running"), status=target) is False
            assert await jobs.cas(jid, expected=("queued", "running"), status=target, owner=me) is False
        assert await status_of(jid) == terminal


async def test_cas_requires_owner_match(tts_env):
    me, other = jobs.owner_id(), uuid.uuid4()
    jid = await insert_job(owner=other, status="running")
    assert await jobs.cas(jid, expected=("running",), status="succeeded", owner=me) is False
    assert await status_of(jid) == "running"
    assert await jobs.cas(jid, expected=("running",), status="succeeded", owner=other) is True
    assert (await job_row(jid)).finished_at is not None


async def test_cancel_before_worker_success_keeps_cancelled(tts_env):
    """DELETE 가 먼저 cancelled 로 확정하면 worker 의 running→succeeded 는 0행이고 cancelled 가 유지된다."""
    me = jobs.owner_id()
    jid = await insert_job(owner=me, status="running")
    assert await jobs.cas(jid, expected=("queued", "running"), status="cancelled") is True  # DELETE
    assert await jobs.cas(jid, expected=("running",), status="succeeded", owner=me, metadata={"x": 1}) is False
    row = await job_row(jid)
    assert row.status == "cancelled" and row.result_metadata == {}  # 실패한 전이는 부수 효과(결과 연결)를 남기지 않는다


async def test_delete_after_success_returns_succeeded(client, auth, tts_env):
    me = jobs.owner_id()
    jid = await insert_job(owner=me, status="running")
    assert await jobs.cas(jid, expected=("running",), status="succeeded", owner=me)
    r = await client.delete(f"/api/tts/jobs/{jid}", headers=auth)
    assert r.status_code == 200 and r.json()["status"] == "succeeded"


async def test_concurrent_cancel_and_success_have_exactly_one_winner(tts_env):
    me = jobs.owner_id()
    for _ in range(10):
        jid = await insert_job(owner=me, status="running")
        won = await asyncio.gather(
            jobs.cas(jid, expected=("queued", "running"), status="cancelled"),
            jobs.cas(jid, expected=("running",), status="succeeded", owner=me),
        )
        assert sorted(won) == [False, True]
        assert await status_of(jid) in ("cancelled", "succeeded")


async def test_cancel_while_worker_is_running_wins_and_hides_result(client, auth, tts_env):
    tts_env.delay = 5
    r = await client.post("/api/tts/jobs", json={"text": "오래 걸리는 문장입니다."}, headers=auth)
    jid = uuid.UUID(r.json()["job_id"])
    worker = asyncio.create_task(jobs.tick())
    await asyncio.wait_for(tts_env.started.wait(), 5)
    assert await status_of(jid) == "running"
    cancelled = await client.delete(f"/api/tts/jobs/{jid}", headers=auth)
    assert cancelled.json()["status"] == "cancelled"
    assert await asyncio.wait_for(worker, 5) is True  # 소유 세대가 감지해 워커 태스크를 취소했다
    state = (await client.get(f"/api/tts/jobs/{jid}", headers=auth)).json()
    assert state["status"] == "cancelled" and "audio_full_url" not in state and state["progress"]["done"] == 0
    assert jid not in jobs._pending_texts and jid not in jobs._running


async def test_cancel_by_another_generation_is_detected_by_owner(tts_env):
    """DELETE 를 받은 세대가 소유 세대와 달라도 소유 세대가 DB 의 cancelled 를 감지해 워커를 멈춘다."""
    tts_env.delay = 5
    me = jobs.owner_id()
    plan = plan_for(tts_env, "다른 세대가 취소할 문장입니다.")
    created = await jobs.accept(plan)
    jid = uuid.UUID(created["job_id"])
    worker = asyncio.create_task(jobs.tick())
    await asyncio.wait_for(tts_env.started.wait(), 5)
    assert await jobs.cas(jid, expected=("queued", "running"), status="cancelled")  # 다른 세대의 DELETE: owner 조건 없음
    assert await asyncio.wait_for(worker, 5) is True
    assert await status_of(jid) == "cancelled" and jid not in jobs._pending_texts
    assert me == (await job_row(jid)).owner_id


# ---------------------------------------------------------------- claim (§8.3.4)


async def test_claim_ignores_other_owners_queued(tts_env):
    me, other = jobs.owner_id(), uuid.uuid4()
    theirs = await insert_job(owner=other, status="queued")
    assert await jobs._claim(me) is None
    assert await status_of(theirs) == "queued"
    mine = await insert_job(owner=me, status="queued")
    assert await jobs._claim(me) == mine
    assert await status_of(mine) == "running" and (await job_row(mine)).started_at is not None


async def test_claim_blocked_while_any_running_row_exists(tts_env):
    me, other = jobs.owner_id(), uuid.uuid4()
    orphan = await insert_job(owner=other, status="running")  # 소유자가 누구든 running 이 남아 있으면
    queued = await insert_job(owner=me, status="queued")
    assert await jobs._claim(me) is None and await status_of(queued) == "queued"
    await jobs.cas(orphan, expected=("running",), status="failed")
    assert await jobs._claim(me) == queued


async def test_claim_takes_oldest_first(tts_env):
    me = jobs.owner_id()
    newer = await insert_job(owner=me, age_seconds=1)
    older = await insert_job(owner=me, age_seconds=60)
    assert await jobs._claim(me) == older
    await jobs.cas(older, expected=("running",), status="succeeded")
    assert await jobs._claim(me) == newer


async def test_concurrent_claims_start_exactly_one_job(tts_env):
    me = jobs.owner_id()
    ids = [await insert_job(owner=me) for _ in range(4)]
    claimed = await asyncio.gather(*(jobs._claim(me) for _ in range(8)))
    won = [c for c in claimed if c is not None]
    assert len(won) == 1 and won[0] in ids
    async with sessionmaker()() as db:
        running = (await db.scalars(select(TtsJob.id).where(TtsJob.status == "running"))).all()
    assert running == won


async def test_missing_input_fails_the_claimed_job(tts_env):
    """chunks_json=NULL 이면서 메모리 payload 도 없는 과거 행은 input_missing 으로 실패한다."""
    me = jobs.owner_id()
    jid = await insert_job(owner=me, status="queued")  # 접수 프로세스가 아니라 _pending_texts 가 없고 chunks_json=None
    assert await jobs.tick() is True
    row = await job_row(jid)
    assert row.chunks_json is None
    assert (row.status, row.error_code) == ("failed", "input_missing")


# ---------------------------------------------------------------- 원문 수명 (§8.2)


async def test_pending_texts_are_released_on_every_terminal_path(tts_env, client, auth):
    # 성공
    ok = uuid.UUID((await client.post("/api/tts/jobs", json={"text": "성공할 문장입니다."}, headers=auth)).json()["job_id"])
    assert ok in jobs._pending_texts
    await jobs.tick()
    assert ok not in jobs._pending_texts and await status_of(ok) == "succeeded"
    # 워커 실패와 시간 초과
    for exc, code in ((RuntimeError("boom"), "worker_failed"), (TimeoutError(), "worker_timeout")):
        tts_env.fail = exc
        jid = uuid.UUID((await client.post("/api/tts/jobs", json={"text": f"{code} 문장입니다."}, headers=auth)).json()["job_id"])
        await jobs.tick()
        row = await job_row(jid)
        assert (row.status, row.error_code) == ("failed", code) and jid not in jobs._pending_texts
    tts_env.fail = None
    # 대기 중 취소
    queued = uuid.UUID((await client.post("/api/tts/jobs", json={"text": "취소할 문장입니다."}, headers=auth)).json()["job_id"])
    assert queued in jobs._pending_texts
    await client.delete(f"/api/tts/jobs/{queued}", headers=auth)
    assert queued not in jobs._pending_texts


async def test_queued_ttl_expires_and_releases_text(tts_env, monkeypatch):
    monkeypatch.setattr(get_settings(), "local_tts_queued_ttl_seconds", 1)
    created = await jobs.accept(plan_for(tts_env, "오래 기다린 문장입니다."))
    jid = uuid.UUID(created["job_id"])
    async with sessionmaker()() as db:
        await db.execute(text("UPDATE tts_jobs SET created_at = now() - interval '10 minutes'"))
        await db.commit()
    await jobs._housekeeping(jobs.owner_id())
    row = await job_row(jid)
    assert (row.status, row.error_code) == ("failed", "input_ttl_expired") and jid not in jobs._pending_texts


async def test_housekeeping_drops_texts_of_jobs_cancelled_elsewhere(tts_env):
    created = await jobs.accept(plan_for(tts_env, "다른 경로로 취소된 문장입니다."))
    jid = uuid.UUID(created["job_id"])
    await jobs.cas(jid, expected=("queued",), status="cancelled")
    assert jid in jobs._pending_texts
    await jobs._housekeeping(jobs.owner_id())
    assert jid not in jobs._pending_texts


async def test_consumer_loop_runs_jobs_when_enabled(tts_env, client, auth, monkeypatch):
    monkeypatch.setattr(get_settings(), "local_tts_enabled", True)
    jobs.kick()
    try:
        jid = uuid.UUID((await client.post("/api/tts/jobs", json={"text": "소비 루프 문장입니다."}, headers=auth)).json()["job_id"])
        await wait_for(lambda: _is(jid, "succeeded"))
    finally:
        await jobs.stop()


async def _is(jid, status):
    return await status_of(jid) == status


async def test_single_process_start_fails_leftover_jobs_from_previous_process(tts_env, monkeypatch):
    left = [await insert_job(owner=uuid.uuid4(), status=s) for s in ("queued", "running")]
    mine = await insert_job(owner=jobs.owner_id(), status="running")  # queued 면 소비 루프가 먼저 집어 갈 수 있다
    monkeypatch.setattr(get_settings(), "local_tts_enabled", True)
    await jobs.start()
    await jobs.stop()
    for jid in left:
        assert (await job_row(jid)).error_code == "server_restarted"
    assert await status_of(mine) == "failed"  # stop() 이 소유 job 을 owner_shutdown 으로 확정한다
    assert (await job_row(mine)).error_code == "owner_shutdown"


# ---------------------------------------------------------------- 세대·drain·퇴역 순서 (§8.3.3)


@pytest_asyncio.fixture(loop_scope="session")
async def two_generations(tmp_path, tts_env, monkeypatch):
    monkeypatch.setattr(get_settings(), "runtime_poll_seconds", 0.05)
    a = GenerationRuntime(tmp_path, revision="a" * 40, api_url="http://127.0.0.1:18811")
    b = GenerationRuntime(tmp_path, revision="b" * 40, api_url="http://127.0.0.1:18812")
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


async def guard_alive() -> bool:
    return any(t.get_name() == "tts-queued-guard" and not t.done() for t in _background)


async def test_queued_guard_blocks_retirement_until_text_is_cleared(two_generations, tts_env):
    a, b = two_generations
    created = await jobs.accept(plan_for(tts_env, "퇴역 전에 정리될 문장입니다."))
    jid = uuid.UUID(created["job_id"])
    # 소유 queued 가 있는 동안 guard 가 _background 에 있고 drain_ready 는 False 다.
    assert await guard_alive()
    assert (await generation_service.ready(a))["drain_ready"] is False
    await activate(b.id, expected=a.id)  # a 는 draining
    assert await a.refresh() == "draining"
    # FND-01: draining 이어도 queued 작업이 generation_draining 으로 실패하지 않고 보존된다.
    assert (await job_row(jid)).status == "queued"
    assert (await generation_service.ready(a))["drain_ready"] is False
    assert await a.can_retire() is False
    # draining 세대가 tick 으로 기수락 작업을 실행하여 완료한다.
    assert await jobs.tick() is True
    assert await status_of(jid) == "succeeded"
    assert jid not in jobs._pending_texts
    # 작업을 마친 뒤에야 guard 가 종료하고 drain_ready 가 True 가 된다.
    await wait_for(lambda: _not_guard())
    ready = await generation_service.ready(a)
    assert ready["drain_ready"] is True and ready["finalizers"] == 0


async def _not_guard():
    return not await guard_alive()


async def test_drain_ready_is_false_for_whole_window_until_cleanup(two_generations, tts_env):
    """기수락 작업이 완료되기 전 어느 시점에도 drain_ready 가 True 가 되지 않는다."""
    a, b = two_generations
    tts_env.delay = 0.2
    created = await jobs.accept(plan_for(tts_env, "천천히 정리되는 문장입니다."))
    jid = uuid.UUID(created["job_id"])
    await activate(b.id, expected=a.id)
    assert await a.refresh() == "draining"

    observed: list[bool] = []
    worker = asyncio.create_task(jobs.tick())
    await asyncio.wait_for(tts_env.started.wait(), 5)

    for _ in range(5):
        observed.append((await generation_service.ready(a))["drain_ready"])
        await asyncio.sleep(0.02)

    await worker
    assert await status_of(jid) == "succeeded"
    assert observed and not any(observed)
    await wait_for(lambda: _not_guard())
    assert (await generation_service.ready(a))["drain_ready"] is True


async def test_running_job_blocks_retirement_and_finishes_in_draining_generation(two_generations, tts_env):
    a, b = two_generations
    tts_env.delay = 0.4
    created = await jobs.accept(plan_for(tts_env, "끝까지 가야 하는 문장입니다."))
    jid = uuid.UUID(created["job_id"])
    worker = asyncio.create_task(jobs.tick())
    await asyncio.wait_for(tts_env.started.wait(), 5)
    await activate(b.id, expected=a.id)
    assert await a.can_retire() is False  # work("tts") 가 _work 에 있다
    assert (await generation_service.ready(a))["drain_ready"] is False
    await worker
    assert await status_of(jid) == "succeeded"  # draining 이어도 이미 running 인 job 은 끝까지 간다
    await wait_for(lambda: _not_guard())
    assert (await generation_service.ready(a))["drain_ready"] is True


async def test_draining_generation_never_starts_new_job(two_generations, tts_env):
    """FND-01: draining 세대는 신규 접수는 거절하지만, 기수락된 queued 작업은 끝까지 실행한다."""
    a, b = two_generations
    created = await jobs.accept(plan_for(tts_env, "시작되면 안 되는 문장입니다."))
    jid = uuid.UUID(created["job_id"])
    await activate(b.id, expected=a.id)
    assert await a.refresh() == "draining"

    # 신규 작업 접수는 거절된다(503 generation_not_active)
    with pytest.raises(service.TtsError) as exc_info:
        await jobs.accept(plan_for(tts_env, "드레인 중 신규 접수"))
    assert exc_info.value.code == "generation_not_active"

    # 기수락된 작업은 draining 중에도 정상 실행된다(accepted work 보존)
    assert await jobs.tick() is True
    assert len(tts_env.calls) == 1
    assert await status_of(jid) == "succeeded"


async def test_post_during_http_inflight_blocks_retirement(two_generations, tts_env, client, auth):
    a, b = two_generations
    await activate(b.id, expected=a.id)
    a.http_inflight = 1  # 처리 중인 POST 는 InflightMiddleware 가 올려 둔다
    assert await a.can_retire() is False
    a.http_inflight = 0
    assert await a.can_retire() is True


async def test_guard_is_recreated_for_new_job_after_it_exits(two_generations, tts_env):
    _a, _ = two_generations
    first = await jobs.accept(plan_for(tts_env, "첫 문장입니다."))
    await jobs.tick()
    assert await status_of(uuid.UUID(first["job_id"])) == "succeeded"
    await wait_for(lambda: _not_guard())
    second = await jobs.accept(plan_for(tts_env, "둘째 문장입니다."))
    assert await guard_alive()  # 소유 queued 가 생기기 전에 guard 가 먼저 보장된다
    await jobs.tick()
    assert await status_of(uuid.UUID(second["job_id"])) == "succeeded"


async def test_guard_does_not_exit_while_owned_queued_remains(two_generations, tts_env):
    _a, _ = two_generations
    created = await jobs.accept(plan_for(tts_env, "남아 있는 문장입니다."))
    await asyncio.sleep(0.3)  # guard 주기(0.05초)를 여러 번 돈다
    assert await guard_alive()
    assert uuid.UUID(created["job_id"]) in jobs._pending_texts


async def test_no_guard_in_single_process_mode(tts_env):
    assert generation.current is None
    await jobs.accept(plan_for(tts_env, "단일 프로세스 문장입니다."))
    assert jobs._guard_task is None and not await guard_alive()


async def test_guard_race_with_new_post_never_leaves_queued_without_guard(two_generations, tts_env):
    """guard 의 종료 판정과 새 POST 는 같은 락으로 직렬화된다. POST 가 락 안에서 커밋한 queued 는 guard 가 보고 남는다."""
    a, _ = two_generations
    first = await jobs.accept(plan_for(tts_env, "먼저 끝나는 문장입니다."))
    await jobs.tick()
    assert await status_of(uuid.UUID(first["job_id"])) == "succeeded"
    async with jobs.guard_scope():
        await asyncio.sleep(0.3)  # guard 는 여러 번 돌지만 락을 못 잡아 종료 판정을 하지 못한다
        assert await guard_alive()
        await insert_job(owner=a.id, status="queued")  # 락 안에서 커밋된 접수
    await asyncio.sleep(0.3)
    assert await guard_alive()  # 판정 시점에 소유 queued 가 보이므로 종료하지 않는다


# ---------------------------------------------------------------- 소유자 생존 (§8.3.5)


async def test_job_of_offline_generation_is_failed_owner_gone_on_read(client, auth, two_generations, tts_env):
    _a, b = two_generations
    jid = await insert_job(owner=b.id, status="queued")
    await b.close()  # 정상 종료: runtime_generations.state='offline'
    r = await client.get(f"/api/tts/jobs/{jid}", headers=auth)
    assert r.status_code == 200 and r.json()["status"] == "failed"
    assert r.json()["error_code"] == "owner_gone" and r.json()["retryable"] is True
    again = await client.get(f"/api/tts/jobs/{jid}", headers=auth)
    assert again.json()["status"] == "failed" and again.json()["error_code"] == "owner_gone"  # 같은 응답으로 수렴


async def test_free_flock_is_death_evidence_even_if_state_not_offline(client, auth, two_generations, tts_env):
    _a, b = two_generations
    jid = await insert_job(owner=b.id, status="queued")
    b._liveness.close()  # 프로세스가 SIGKILL 로 죽어 OS 가 잠금을 풀어 준 상황. DB 상태는 아직 standby.
    b._liveness = None
    r = await client.get(f"/api/tts/jobs/{jid}", headers=auth)
    assert r.json()["status"] == "failed" and r.json()["error_code"] == "owner_gone"


async def test_stale_heartbeat_of_live_owner_is_not_death(client, auth, two_generations, tts_env):
    a, b = two_generations
    jid = await insert_job(owner=b.id, status="queued")
    running = await insert_job(owner=b.id, status="running")
    async with sessionmaker()() as db:
        await db.execute(text("UPDATE runtime_generations SET heartbeat_at = now() - interval '1 day'"))
        await db.commit()
    for _ in range(2):
        r = await client.get(f"/api/tts/jobs/{jid}", headers=auth)
        assert r.json()["status"] == "queued"
    assert (await client.get(f"/api/tts/jobs/{running}", headers=auth)).json()["status"] == "running"
    await jobs._housekeeping(a.id)
    assert await status_of(jid) == "queued" and await status_of(running) == "running"


async def test_unknown_owner_is_not_death(client, auth, two_generations, tts_env):
    jid = await insert_job(owner=uuid.uuid4(), status="queued")  # runtime_generations 에 행이 없다: 증거 없음
    assert (await client.get(f"/api/tts/jobs/{jid}", headers=auth)).json()["status"] == "queued"
    await jobs._housekeeping(generation.current.id)
    assert await status_of(jid) == "queued"


async def test_dead_owner_running_row_is_reaped_after_grace_and_blocks_claims_until_then(two_generations, tts_env, monkeypatch):
    a, b = two_generations
    running = await insert_job(owner=b.id, status="running")
    queued = await insert_job(owner=a.id, status="queued")
    await b.close()
    monkeypatch.setattr(jobs, "DEAD_GRACE_SECONDS", 3600.0)
    await jobs._housekeeping(a.id)
    assert await status_of(running) == "running"  # 부모 감시(1초 폴링) 유예 동안은 건드리지 않는다
    assert await jobs._claim(a.id) is None  # 그동안 NOT EXISTS running 이 새 claim 을 막는다
    monkeypatch.setattr(jobs, "DEAD_GRACE_SECONDS", 0.0)
    await jobs._housekeeping(a.id)
    row = await job_row(running)
    assert (row.status, row.error_code) == ("failed", "owner_gone")
    assert await jobs._claim(a.id) == queued


async def test_housekeeping_reaps_queued_of_dead_owner_immediately(two_generations, tts_env):
    a, b = two_generations
    jid = await insert_job(owner=b.id, status="queued")
    await b.close()
    await jobs._housekeeping(a.id)
    assert (await job_row(jid)).error_code == "owner_gone"


async def test_new_generation_does_not_take_over_old_generation_jobs(two_generations, tts_env):
    a, b = two_generations
    old = await insert_job(owner=a.id, status="queued")
    await activate(b.id, expected=a.id)
    generation.current = b
    assert await jobs._claim(b.id) is None  # 원문이 없으므로 새 세대는 구 세대의 job 을 claim 하지 않는다
    await jobs._housekeeping(b.id)
    assert await status_of(old) == "queued"  # a 는 아직 살아 있다: 증거 없이 실패시키지 않는다


async def test_housekeeping_waits_for_uncommitted_accept(tts_env):
    """접수는 guard 락 안에서 원문을 넣고 커밋한다. 정리가 커밋 전 job 의 원문을 행 없음으로 오인해 지우면 안 된다."""
    jid = uuid.uuid4()
    async with jobs.guard_scope():
        jobs._pending_texts[jid] = ["아직 커밋되지 않은 문장"]
        sweep = asyncio.create_task(jobs._housekeeping(jobs.owner_id()))
        await asyncio.sleep(0.1)
        assert not sweep.done() and jid in jobs._pending_texts
    await sweep
    assert jid not in jobs._pending_texts  # 락이 풀린 뒤에는 행이 끝내 없으면 정리된다
