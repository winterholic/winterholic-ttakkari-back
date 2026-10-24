"""Step 6: NotebookLM·Reverse·수동 색인은 API 가 접수만 하고 실행부 소비자가 끝까지 돌린다.

실제 로그인·브라우저·Google·모델은 쓰지 않는다. 가짜 NotebookLM 백엔드, 가짜 모델, 임시 폴더만 쓴다.
원격 모드의 API 는 generation.current 를 role=api/execution_mode=remote 로 두어 흉내 낸다.
"""
from __future__ import annotations

import asyncio
import json
import uuid
from datetime import timedelta
from types import SimpleNamespace

import pytest
import pytest_asyncio
from conftest import ALLOWED
from sqlalchemy import select, update
from test_notebooklm_service import FakeBackend, _cookies
from test_reverse import _fixture_repo, _install_fake_review, _seed_legacy

from ttakkari import background_consumers, generation, reverse
from ttakkari.config import get_settings
from ttakkari.context import index_queue
from ttakkari.db import sessionmaker
from ttakkari.google_account import login
from ttakkari.models import (
    CtxSource,
    NotebookLMJob,
    NotebookLMSource,
    ReverseJob,
    utcnow,
)
from ttakkari.notebooklm import metrics, redact, session
from ttakkari.notebooklm import service as nlm_module
from ttakkari.notebooklm.service import NotebookLMService, service

# ------------------------------------------------------------------ 공통 준비

@pytest_asyncio.fixture(loop_scope="session")
async def remote_api():
    """원격 모드 API 를 흉내 낸다. 전역 소비자는 멈춰 둬서 시험이 queued 를 직접 관찰할 수 있게 한다."""
    await background_consumers.consumers.stop(drain_seconds=0)
    previous = generation.current
    generation.current = SimpleNamespace(role="api", execution_mode="remote", http_inflight=0, state="active",
                                         id=uuid.uuid4())
    try:
        yield
    finally:
        generation.current = previous
        await background_consumers.consumers.start()


@pytest.fixture
def nlm(monkeypatch):
    backend = FakeBackend()
    before = service._factory
    service.set_backend_factory(lambda _a: backend)
    service._sems.clear()
    service._last_verify.clear()
    metrics.reset()
    redact.clear_secrets()
    st = get_settings()
    monkeypatch.setattr(st, "notebooklm_read_retry_base_seconds", 0.0)
    monkeypatch.setattr(st, "notebooklm_read_retry_cap_seconds", 0.0)
    session.save_cookies("primary", _cookies())
    yield backend
    session.delete("primary")
    service._factory = before
    service._backends.clear()
    login.manager = login.LoginManager()


def _executor(backend: FakeBackend) -> NotebookLMService:
    """API 와 다른 프로세스의 서비스 인스턴스를 흉내 낸다(메모리를 공유하지 않는다)."""
    return NotebookLMService(lambda _a: backend)


async def _settle(*services: NotebookLMService, timeout: float = 10.0) -> None:
    async def wait() -> None:
        while True:
            tasks = [t for s in services for t in s._tasks.values()] + list(reverse._tasks) + list(index_queue._tasks)
            tasks = [t for t in tasks if not t.done()]
            if not tasks:
                return
            await asyncio.gather(*tasks, return_exceptions=True)
    await asyncio.wait_for(wait(), timeout)


async def _job_row(job_id: str) -> NotebookLMJob:
    async with sessionmaker()() as db:
        return await db.get(NotebookLMJob, uuid.UUID(job_id))


async def _get_job(client, auth, job_id: str) -> dict:
    r = await client.get(f"/api/notebooklm/jobs/{job_id}", headers=auth)
    assert r.status_code == 200, r.text
    return r.json()


def test_deferred_is_true_only_for_a_remote_mode_api():
    previous = generation.current
    try:
        generation.current = None
        assert background_consumers.deferred() is False
        generation.current = SimpleNamespace(role="api", execution_mode="local")
        assert background_consumers.deferred() is False
        generation.current = SimpleNamespace(role="executor", execution_mode="local")
        assert background_consumers.deferred() is False
        generation.current = SimpleNamespace(role="api", execution_mode="remote")
        assert background_consumers.deferred() is True
    finally:
        generation.current = previous


# ------------------------------------------------------------------ NotebookLM

async def test_remote_query_is_recorded_without_a_task_then_the_consumer_finishes_it(client, auth, nlm, remote_api):
    question = "이 노트북은 무엇을 다루나?"
    r = await client.post("/api/notebooklm/notebooks/nb1/queries", json={"question": question}, headers=auth)
    assert r.status_code == 202
    job = r.json()["job"]
    assert job["status"] == "queued" and job["kind"] == "query" and "spec" not in job
    assert service._tasks == {} and nlm.calls == []  # API 는 Google 에 나가지 않고 task 도 만들지 않았다
    assert (await _job_row(job["id"])).spec["question"] == question

    executor = _executor(nlm)
    assert await executor.pump_queued() == 1
    await _settle(executor)
    done = await _get_job(client, auth, job["id"])
    assert done["status"] == "succeeded" and done["result"]["query"]["answer"] == "분석 [1]"
    assert nlm.count("ask") == 1
    assert not (await _job_row(job["id"])).spec  # 입력 원본은 끝나면 남기지 않는다


async def test_remote_text_source_survives_in_the_job_row_and_is_dropped_after_success(client, auth, nlm, remote_api):
    body = "본문-" + "가" * 50
    r = await client.post("/api/notebooklm/notebooks/nb1/sources/text", headers=auth,
                          json={"title": "메모", "text": body})
    assert r.status_code == 202 and r.json()["job"]["status"] == "queued"
    job_id = r.json()["job"]["id"]
    assert body not in json.dumps(r.json()) and nlm.calls == []
    async with sessionmaker()() as db:
        ledger = await db.scalar(select(NotebookLMSource).where(NotebookLMSource.job_id == uuid.UUID(job_id)))
    assert ledger.status == "reserved"
    # 같은 소스를 다시 접수하면 진행 중으로 막힌다(중복 부수효과 방지가 접수 단계에서 이미 유지된다).
    dup = await client.post("/api/notebooklm/notebooks/nb1/sources/text", headers=auth,
                            json={"title": "메모", "text": body})
    assert dup.status_code == 409

    executor = _executor(nlm)
    assert await executor.pump_queued() == 1
    await _settle(executor)
    assert (await _get_job(client, auth, job_id))["status"] == "succeeded" and nlm.count("add_text") == 1
    async with sessionmaker()() as db:
        ledger = await db.scalar(select(NotebookLMSource).where(NotebookLMSource.job_id == uuid.UUID(job_id)))
    assert ledger.status == "added"
    row = await _job_row(job_id)
    assert not row.spec and body not in json.dumps(row.params) and body not in json.dumps(row.result)


async def test_remote_upload_keeps_the_file_for_the_consumer_and_removes_it_afterwards(client, auth, nlm, remote_api):
    r = await client.post("/api/notebooklm/notebooks/nb1/sources/upload", headers=auth,
                          files={"file": ("보고서.pdf", b"%PDF-1.4 fake", "application/pdf")}, data={"title": "보고서"})
    assert r.status_code == 202
    job_id = r.json()["job"]["id"]
    assert len(list(session.uploads_dir().glob("*/*"))) == 1  # API 는 지우지 않는다
    assert nlm.calls == []
    executor = _executor(nlm)
    assert await executor.pump_queued() == 1
    await _settle(executor)
    assert (await _get_job(client, auth, job_id))["status"] == "succeeded" and nlm.count("add_file") == 1
    assert not any(session.uploads_dir().glob("*/*"))


async def test_cancel_of_a_job_nobody_took_is_final_and_frees_the_source_reservation(client, auth, nlm, remote_api):
    payload = {"title": "취소", "text": "취소할 본문 " * 5}
    r = await client.post("/api/notebooklm/notebooks/nb1/sources/text", headers=auth, json=payload)
    job_id = r.json()["job"]["id"]
    c = await client.post(f"/api/notebooklm/jobs/{job_id}/cancel", headers=auth)
    assert c.status_code == 200 and c.json()["status"] == "cancelled" and c.json()["error_code"] == "cancelled"
    assert await _executor(nlm).pump_queued() == 0 and nlm.calls == []
    again = await client.post("/api/notebooklm/notebooks/nb1/sources/text", headers=auth, json=payload)
    assert again.status_code == 202  # 예약이 풀려서 같은 소스를 다시 접수할 수 있다
    assert (await client.post(f"/api/notebooklm/jobs/{job_id}/cancel", headers=auth)).status_code == 409


async def test_cancel_reaches_a_job_another_process_is_running(client, auth, nlm, remote_api, monkeypatch):
    monkeypatch.setattr(nlm_module, "HEARTBEAT_SECONDS", 0.05)
    nlm.gate = asyncio.Event()  # 실행부가 등록 요청을 보내기 직전에 멈춰 있다
    r = await client.post("/api/notebooklm/notebooks/nb1/sources/text", headers=auth,
                          json={"title": "진행", "text": "진행 중 취소 " * 5})
    job_id = r.json()["job"]["id"]
    executor = _executor(nlm)
    assert await executor.pump_queued() == 1
    await asyncio.wait_for(nlm.entered.wait(), 5)
    assert (await _get_job(client, auth, job_id))["status"] == "running"
    assert service._tasks == {}  # API 쪽에는 task 가 없다
    c = await client.post(f"/api/notebooklm/jobs/{job_id}/cancel", headers=auth)
    assert c.status_code == 200 and c.json()["status"] == "cancelled" and c.json()["result"] == {"outcome_unknown": True}
    await _settle(executor)


async def test_api_shutdown_does_not_interrupt_a_job_the_executor_is_running(client, auth, nlm, remote_api):
    release = asyncio.Event()
    nlm.plan["ask"] = [release.wait]
    r = await client.post("/api/notebooklm/notebooks/nb1/queries", headers=auth, json={"question": "계속 돌아야 한다"})
    job_id = r.json()["job"]["id"]
    executor = _executor(nlm)
    assert await executor.pump_queued() == 1
    for _ in range(100):
        if nlm.count("ask"):
            break
        await asyncio.sleep(0.02)
    await service.shutdown()  # API 세대 종료. 실행부의 task 와는 무관하다
    assert (await _get_job(client, auth, job_id))["status"] == "running"
    assert not next(iter(executor._tasks.values())).done()
    release.set()
    await _settle(executor)
    assert (await _get_job(client, auth, job_id))["status"] == "succeeded"


async def test_two_consumers_never_run_the_same_job_twice(client, auth, nlm, remote_api):
    r = await client.post("/api/notebooklm/notebooks/nb1/queries", headers=auth, json={"question": "한 번만"})
    job_id = r.json()["job"]["id"]
    first, second = _executor(nlm), _executor(nlm)
    started = await asyncio.gather(first.pump_queued(), second.pump_queued())
    assert sum(started) == 1
    await _settle(first, second)
    assert nlm.count("ask") == 1 and (await _get_job(client, auth, job_id))["status"] == "succeeded"


async def test_one_account_runs_at_most_the_configured_concurrency_and_the_rest_stay_queued(
        client, auth, nlm, remote_api, monkeypatch):
    monkeypatch.setattr(get_settings(), "notebooklm_concurrency", 1)
    release = asyncio.Event()

    nlm.plan["ask"] = [release.wait]
    ids = []
    for q in ("하나", "둘"):
        ids.append((await client.post("/api/notebooklm/notebooks/nb1/queries", headers=auth,
                                      json={"question": q})).json()["job"]["id"])
    executor = _executor(nlm)
    assert await executor.pump_queued() == 1
    assert await executor.pump_queued() == 0  # 같은 계정은 상한까지만
    statuses = [(await _get_job(client, auth, i))["status"] for i in ids]
    assert sorted(statuses) == ["queued", "running"]
    release.set()
    await _settle(executor)
    assert await executor.pump_queued() == 1
    await _settle(executor)
    assert [(await _get_job(client, auth, i))["status"] for i in ids] == ["succeeded", "succeeded"]


async def test_running_rows_are_never_claimed_and_recovery_keeps_queued_input(client, auth, nlm, remote_api):
    old = utcnow() - timedelta(hours=1)
    async with sessionmaker()() as db:
        running = NotebookLMJob(id=uuid.uuid4(), account_id="primary", kind="query", notebook_id="nb1",
                                status="running", params={}, heartbeat_at=old, spec={"question": "q"})
        kept = NotebookLMJob(id=uuid.uuid4(), account_id="primary", kind="query", notebook_id="nb1",
                             status="queued", params={}, heartbeat_at=old, spec={"question": "대기"})
        legacy = NotebookLMJob(id=uuid.uuid4(), account_id="primary", kind="query", notebook_id="nb1",
                               status="queued", params={}, heartbeat_at=old)
        db.add_all([running, kept, legacy])
        await db.commit()
    executor = _executor(nlm)
    changed = await executor.recover_jobs(stale_seconds=120)
    assert changed == 2  # running → unknown, spec 없는 이전 세대 queued → failed
    assert (await _get_job(client, auth, str(running.id)))["status"] == "unknown"
    assert (await _get_job(client, auth, str(legacy.id)))["status"] == "failed"
    assert (await _get_job(client, auth, str(kept.id)))["status"] == "queued"  # 보존
    assert await executor.pump_queued() == 1  # 가져가는 건 보존된 queued 하나뿐
    await _settle(executor)
    assert nlm.count("ask") == 1
    assert (await _get_job(client, auth, str(running.id)))["status"] == "unknown"  # 다시 돌리지 않았다


async def test_local_mode_still_runs_the_job_in_process_exactly_once(client, auth, nlm):
    assert generation.current is None
    await background_consumers.consumers.stop(drain_seconds=0)
    try:
        r = await client.post("/api/notebooklm/notebooks/nb1/queries", headers=auth, json={"question": "로컬"})
        job_id = r.json()["job"]["id"]
        assert len(service._tasks) == 1
        await service.pump_queued()  # 소비자가 같은 job 을 가져가려 해도 한 번만 돈다
        await _settle(service)
        assert (await _get_job(client, auth, job_id))["status"] == "succeeded" and nlm.count("ask") == 1
    finally:
        await background_consumers.consumers.start()


# ------------------------------------------------------------------ Reverse

async def _reverse_row(job_id) -> ReverseJob:
    async with sessionmaker()() as db:
        return await db.get(ReverseJob, uuid.UUID(str(job_id)))


async def test_remote_reverse_start_only_records_and_a_stale_queue_is_not_failed(client, auth, make_git_dir, remote_api,
                                                                                monkeypatch):
    _install_fake_review(monkeypatch)
    root = make_git_dir()
    _fixture_repo(root)
    started = await _seed_legacy(root)
    job_id = started["id"]
    assert started["status"] == "queued" and not reverse._tasks
    async with sessionmaker()() as db:  # 실행부가 한참 못 가져가도 queued 는 실패로 만들지 않는다
        await db.execute(update(ReverseJob).where(ReverseJob.id == uuid.UUID(job_id)).values(
            updated_at=utcnow() - timedelta(hours=3)))
        await db.commit()
    body = (await client.get(f"/api/reverse/{job_id}", headers=auth)).json()
    assert body["status"] == "queued"
    assert (await client.get("/api/reverse", headers=auth)).json()["items"][0]["status"] == "queued"

    assert await reverse.pump_queued() == 1
    await _settle()
    body = (await client.get(f"/api/reverse/{job_id}", headers=auth)).json()
    assert body["status"] == "done", body.get("error")
    assert (root / "PWNED").exists() is False


async def test_remote_reverse_retry_requeues_without_a_task_and_the_consumer_reruns_it(client, auth, make_git_dir,
                                                                                      remote_api, monkeypatch):
    _install_fake_review(monkeypatch)
    root = make_git_dir()
    _fixture_repo(root)
    async with sessionmaker()() as db:
        job = ReverseJob(target=str(root), kind="local_path", status="failed", stages=reverse._fresh_stages(),
                         error="이전 실패")
        db.add(job)
        await db.commit()
        job_id = str(job.id)
    r = await client.post(f"/api/reverse/{job_id}/retry", headers=auth)
    assert r.status_code == 202 and r.json()["status"] == "queued" and not reverse._tasks
    assert (await client.post(f"/api/reverse/{job_id}/retry", headers=auth)).status_code == 409
    assert await reverse.pump_queued() == 1
    await _settle()
    assert (await client.get(f"/api/reverse/{job_id}", headers=auth)).json()["status"] == "done"


async def test_reverse_consumer_claims_only_queued_non_reconstruction_rows_within_the_cap(remote_api, make_git_dir,
                                                                                        monkeypatch):
    root = make_git_dir()
    release = asyncio.Event()
    seen: list[uuid.UUID] = []

    async def hold(job_id, target):
        seen.append(job_id)
        await release.wait()
    monkeypatch.setattr(reverse, "_pipeline", hold)
    ids = []
    async with sessionmaker()() as db:
        for _ in range(3):
            j = ReverseJob(id=uuid.uuid4(), target=str(root), kind="local_path", status="queued",
                           stages=reverse._fresh_stages())
            db.add(j)
            ids.append(j.id)
        running = ReverseJob(id=uuid.uuid4(), target=str(root), kind="local_path", status="running",
                             stages=reverse._fresh_stages())
        recon = ReverseJob(id=uuid.uuid4(), target="재현", kind="reconstruction", status="queued", stages=[])
        db.add_all([running, recon])
        await db.commit()
    assert await reverse.pump_queued() == reverse.MAX_CONCURRENT == 2
    await asyncio.sleep(0.05)
    assert await reverse.pump_queued() == 0  # 상한
    assert running.id not in seen and recon.id not in seen
    assert (await _reverse_row(running.id)).status == "running" and (await _reverse_row(recon.id)).status == "queued"
    statuses = sorted([(await _reverse_row(i)).status for i in ids])
    assert statuses == ["queued", "running", "running"]
    release.set()
    await _settle()


async def test_reverse_target_is_validated_again_when_the_consumer_claims_it(remote_api):
    async with sessionmaker()() as db:
        job = ReverseJob(target="/nonexistent-dir-xyz", kind="local_path", status="queued",
                         stages=reverse._fresh_stages())
        db.add(job)
        await db.commit()
    assert await reverse.pump_queued() == 0
    row = await _reverse_row(job.id)
    assert row.status == "failed" and "대상 검증 실패" in row.error


async def test_reverse_running_row_that_went_stale_is_still_failed_on_read(client, auth):
    async with sessionmaker()() as db:
        job = ReverseJob(target="x", kind="local_path", status="running", stages=reverse._fresh_stages())
        db.add(job)
        await db.commit()
        await db.execute(update(ReverseJob).where(ReverseJob.id == job.id).values(
            updated_at=utcnow() - timedelta(minutes=reverse.STALE_MINUTES + 5)))
        await db.commit()
    body = (await client.get(f"/api/reverse/{job.id}", headers=auth)).json()
    assert body["status"] == "failed" and "중단" in body["error"]


# ------------------------------------------------------------------ 수동 색인

def _notes(name: str, text: str = "# 제목\n색인될 본문이다.\n"):
    d = ALLOWED / name
    d.mkdir()
    (d / "a.md").write_text(text)
    return d


async def _source(db_row_id) -> CtxSource:
    async with sessionmaker()() as db:
        return await db.get(CtxSource, uuid.UUID(str(db_row_id)))


async def test_remote_source_registration_records_a_request_and_the_consumer_indexes_it(client, auth, remote_api):
    d = _notes("bg-index-remote")
    r = await client.post("/api/assistant/sources", json={"path": str(d)}, headers=auth)
    assert r.status_code == 201 and r.json()["indexing"] is True and r.json()["documents"] == 0
    assert not index_queue._tasks
    assert (await _source(r.json()["id"])).index_requested_at is not None
    assert await index_queue.pump() == 1
    await _settle()
    out = (await client.get("/api/assistant/sources", headers=auth)).json()["sources"][0]
    assert out["documents"] == 1 and out["indexing"] is False and out["last_indexed_at"]
    src = await _source(out["id"])
    assert src.index_requested_at is None and src.index_started_at is None


async def test_remote_reindex_collapses_duplicate_requests_and_does_not_queue_behind_a_running_index(
        client, auth, remote_api, monkeypatch):
    d = _notes("bg-index-reindex")
    created = (await client.post("/api/assistant/sources", json={"path": str(d)}, headers=auth)).json()
    sid = created["id"]
    for _ in range(3):
        assert (await client.post(f"/api/assistant/sources/{sid}/reindex", headers=auth)).status_code == 200
    release, calls = asyncio.Event(), []

    async def slow_index(db, src):
        calls.append(src.id)
        await release.wait()
        return {}
    monkeypatch.setattr(index_queue, "index_source", slow_index)
    assert await index_queue.pump() == 1
    await asyncio.sleep(0.05)
    assert (await client.post(f"/api/assistant/sources/{sid}/reindex", headers=auth)).json()["indexing"] is True
    assert (await _source(sid)).index_requested_at is None  # 실행 중인 색인 뒤에 쌓지 않는다
    assert await index_queue.pump() == 0
    release.set()
    await _settle()
    assert len(calls) == 1


async def test_index_claim_is_exclusive_across_manual_consumer_and_periodic_index(client, auth, remote_api,
                                                                                 monkeypatch):
    from ttakkari.context.indexer import index_all
    d = _notes("bg-index-exclusive")
    sid = (await client.post("/api/assistant/sources", json={"path": str(d)}, headers=auth)).json()["id"]
    release, calls = asyncio.Event(), []

    async def slow_index(db, src):
        calls.append(src.id)
        await release.wait()
        return {}
    monkeypatch.setattr(index_queue, "index_source", slow_index)
    from ttakkari.context import indexer
    monkeypatch.setattr(indexer, "index_source", slow_index)
    assert await index_queue.pump() == 1
    await asyncio.sleep(0.05)
    async with sessionmaker()() as db:
        assert await index_all(db) == 0  # 수동 소비자가 돌리는 출처는 주기 색인이 건너뛴다
    release.set()
    await _settle()
    assert len(calls) == 1
    async with sessionmaker()() as db:
        assert await index_all(db) == 1  # 끝난 뒤에는 주기 색인이 다시 돌 수 있다
    assert (await _source(sid)).index_started_at is None


async def test_index_claim_left_by_a_dead_process_is_reclaimed_only_after_it_goes_stale(client, auth, remote_api):
    d = _notes("bg-index-stale")
    sid = (await client.post("/api/assistant/sources", json={"path": str(d)}, headers=auth)).json()["id"]
    async with sessionmaker()() as db:
        await db.execute(update(CtxSource).where(CtxSource.id == uuid.UUID(sid)).values(
            index_started_at=utcnow() - timedelta(minutes=5), index_requested_at=utcnow()))
        await db.commit()
    assert await index_queue.pump() == 0  # 최근 claim 은 살아 있는 것으로 본다
    async with sessionmaker()() as db:
        await db.execute(update(CtxSource).where(CtxSource.id == uuid.UUID(sid)).values(
            index_started_at=utcnow() - timedelta(seconds=index_queue.CLAIM_STALE_SECONDS + 60)))
        await db.commit()
    assert await index_queue.pump() == 1
    await _settle()
    assert (await _source(sid)).last_indexed_at is not None


# ------------------------------------------------------------------ 소비자 생명주기

async def test_consumer_loops_finish_recorded_work_and_stop_drains_in_flight_but_keeps_queued_rows(
        client, auth, nlm, remote_api, make_git_dir, monkeypatch):
    monkeypatch.setattr(get_settings(), "runtime_poll_seconds", 0.05)
    nlm_release, reverse_release = asyncio.Event(), asyncio.Event()

    nlm.plan["ask"] = [nlm_release.wait]

    async def hold_pipeline(job_id, target):
        started.append(job_id)
        await reverse_release.wait()
        await reverse._locked_finish(job_id, "summarize", result={"ok": True}, error=None, job_status="done")
    started: list[uuid.UUID] = []
    monkeypatch.setattr(reverse, "_pipeline", hold_pipeline)
    root = make_git_dir()
    d = _notes("bg-index-lifecycle")

    query_id = (await client.post("/api/notebooklm/notebooks/nb1/queries", headers=auth,
                                  json={"question": "첫 질문"})).json()["job"]["id"]
    reverse_ids = []
    async with sessionmaker()() as db:
        for _ in range(3):
            j = ReverseJob(id=uuid.uuid4(), target=str(root), kind="local_path", status="queued",
                           stages=reverse._fresh_stages())
            db.add(j)
            reverse_ids.append(j.id)
        await db.commit()
    sid = (await client.post("/api/assistant/sources", json={"path": str(d)}, headers=auth)).json()["id"]

    consumers = background_consumers.BackgroundConsumers()
    await consumers.start()
    for _ in range(200):
        if nlm.count("ask") and len(started) == reverse.MAX_CONCURRENT and (await _source(sid)).last_indexed_at:
            break
        await asyncio.sleep(0.05)
    assert nlm.count("ask") == 1 and len(started) == reverse.MAX_CONCURRENT
    assert (await _source(sid)).last_indexed_at is not None

    stopper = asyncio.create_task(consumers.stop(drain_seconds=20))
    await asyncio.sleep(0.3)
    assert not stopper.done()  # 실행 중인 작업이 끝나길 기다린다
    nlm_release.set()
    reverse_release.set()
    await asyncio.wait_for(stopper, 10)
    assert (await _get_job(client, auth, query_id))["status"] == "succeeded"
    statuses = [(await _reverse_row(i)).status for i in reverse_ids]
    assert sorted(statuses) == ["done", "done", "queued"]  # 새 claim 은 멈췄고 남은 하나는 queued 그대로
    assert not consumers.running


async def test_stop_after_the_drain_window_cancels_what_is_left_through_the_domain_cancel_path(
        client, auth, nlm, remote_api):
    nlm.plan["ask"] = [lambda: asyncio.sleep(60)]
    job_id = (await client.post("/api/notebooklm/notebooks/nb1/queries", headers=auth,
                                json={"question": "오래 걸린다"})).json()["job"]["id"]
    assert await service.pump_queued() == 1
    for _ in range(100):
        if nlm.count("ask"):
            break
        await asyncio.sleep(0.02)
    await background_consumers.BackgroundConsumers().stop(drain_seconds=0.1)
    job = await _get_job(client, auth, job_id)
    assert job["status"] == "unknown" and job["error_code"] == "interrupted"  # 결과를 모르는 채로 정직하게 남는다


# ------------------------------------------------------------------ 배선(어느 프로세스가 소비자를 띄우는가)

async def test_only_the_executor_and_local_mode_start_the_consumers_never_a_remote_api(monkeypatch):
    from unittest.mock import AsyncMock

    from ttakkari import executor, generation_service
    from ttakkari.artifacts import preview
    from ttakkari.meetings.local_stt import jobs as stt_jobs
    from ttakkari.runs.desk_qwen_service import service as qwen
    from ttakkari.tts import jobs as tts_jobs

    started = AsyncMock()
    monkeypatch.setattr(background_consumers, "start", started)
    for target, attr in ((preview, "start_worker"), (stt_jobs, "start"), (tts_jobs, "start"), (qwen, "start")):
        monkeypatch.setattr(target, attr, AsyncMock())

    await generation_service.start_api_services("remote")
    started.assert_not_awaited()  # 원격 모드 API 는 접수와 조회만 한다
    await generation_service.start_api_services("local")
    assert started.await_count == 1  # 로컬 모드는 롤백 경로라 이 프로세스가 소비한다

    monkeypatch.setattr(generation_service, "maintenance_jobs", lambda _s: [])
    await executor.start_background_services()
    assert started.await_count == 2  # 실행부


async def test_stop_claiming_leaves_inflight_work_alone_and_new_rows_queued(client, auth, nlm, remote_api, monkeypatch):
    monkeypatch.setattr(get_settings(), "runtime_poll_seconds", 0.05)
    release = asyncio.Event()
    nlm.plan["ask"] = [release.wait]
    first = (await client.post("/api/notebooklm/notebooks/nb1/queries", headers=auth,
                               json={"question": "먼저"})).json()["job"]["id"]
    consumers = background_consumers.BackgroundConsumers()
    await consumers.start()
    for _ in range(100):
        if nlm.count("ask"):
            break
        await asyncio.sleep(0.05)
    consumers.stop_claiming()  # 종료 신호가 오는 순간
    second = (await client.post("/api/notebooklm/notebooks/nb1/queries", headers=auth,
                                json={"question": "나중"})).json()["job"]["id"]
    await asyncio.sleep(0.3)
    assert (await _get_job(client, auth, second))["status"] == "queued"
    assert (await _get_job(client, auth, first))["status"] == "running"
    release.set()
    await consumers.stop(drain_seconds=10)
    assert (await _get_job(client, auth, first))["status"] == "succeeded"
    assert (await _get_job(client, auth, second))["status"] == "queued"
