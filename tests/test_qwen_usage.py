from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import select

from ttakkari.config import get_settings
from ttakkari.db import sessionmaker
from ttakkari.models import ChatSession, QwenTokenUsage, Run, Workspace
from ttakkari.runs import desk_qwen, desk_qwen_server, qwen_usage


@pytest.mark.asyncio
async def test_usage_sums_operational_calls_and_keeps_unmetered_separate():
    run_id = uuid4()
    request_id = uuid4()
    async with sessionmaker()() as db:
        await _insert_run(db, run_id)
        await qwen_usage.record_call(db, run_id=run_id, request_id=request_id, call_index=0, stage="kind",
                                     prompt_tokens=31, completion_tokens=4, total_tokens=35)
        await qwen_usage.record_call(db, run_id=run_id, request_id=request_id, call_index=1, stage="verify",
                                     prompt_tokens=None, completion_tokens=None, total_tokens=None,
                                     status="usage_missing")
        await db.commit()
    async with sessionmaker()() as db:
        rows = list(await db.scalars(select(QwenTokenUsage).where(QwenTokenUsage.run_id == run_id).order_by(QwenTokenUsage.call_index)))
    assert len(rows) == 2
    assert rows[0].input_tokens == 31 and rows[0].output_tokens == 4 and rows[0].total_tokens == 35
    assert rows[1].status == "usage_missing"
    assert rows[1].input_tokens is None and rows[1].total_tokens is None


@pytest.mark.asyncio
async def test_duplicate_callback_is_idempotent_and_rejects_inconsistent_total():
    run_id = uuid4()
    request_id = uuid4()
    async with sessionmaker()() as db:
        await _insert_run(db, run_id)
        first = await qwen_usage.record_call(db, run_id=run_id, request_id=request_id, call_index=0, stage="kind",
                                             prompt_tokens=10, completion_tokens=2, total_tokens=12)
        duplicate = await qwen_usage.record_call(db, run_id=run_id, request_id=request_id, call_index=0, stage="kind",
                                                 prompt_tokens=10, completion_tokens=2, total_tokens=12)
        with pytest.raises(ValueError, match="total_tokens"):
            await qwen_usage.record_call(db, run_id=run_id, request_id=request_id, call_index=1, stage="kind",
                                         prompt_tokens=10, completion_tokens=2, total_tokens=99)
        retry = await qwen_usage.record_call(db, run_id=run_id, request_id=uuid4(), call_index=0, stage="kind",
                                             prompt_tokens=10, completion_tokens=2, total_tokens=12)
        assert first is True and duplicate is False
        assert retry is True  # a separate routing attempt is counted even when its call index restarts
        await db.commit()


@pytest.mark.asyncio
async def test_concurrent_duplicate_callbacks_insert_exactly_one_row():
    run_id = uuid4()
    request_id = uuid4()
    async with sessionmaker()() as db:
        await _insert_run(db, run_id)
        await db.commit()
    async def write():
        async with sessionmaker()() as db:
            inserted = await qwen_usage.record_call(db, run_id=run_id, request_id=request_id, call_index=0, stage="kind",
                prompt_tokens=8, completion_tokens=2, total_tokens=10)
            await db.commit()
            return inserted
    assert sorted(await asyncio.gather(write(), write())) == [False, True]


@pytest.mark.asyncio
async def test_today_and_week_are_kst_monday_windows(client, auth):
    now = datetime.now(UTC)
    before_today = now - timedelta(days=8)
    async with sessionmaker()() as db:
        run_ids = [uuid4() for _ in range(3)]
        for run_id in run_ids:
            await _insert_run(db, run_id)
        for idx, at, p, c, status in (
            (0, before_today, 5, 1, "metered"),
            (1, now, 20, 3, "metered"),
            (2, now, None, None, "usage_missing"),
        ):
            db.add(QwenTokenUsage(run_id=run_ids[idx], request_id=uuid4(), call_index=idx, stage="kind", status=status,
                                  input_tokens=p, output_tokens=c, total_tokens=p + c if p is not None else None,
                                  recorded_at=at))
        await db.commit()
    response = await client.get("/api/accounts/qwen-usage", headers=auth)
    assert response.status_code == 200
    body = response.json()
    assert body["timezone"] == "Asia/Seoul"
    assert body["week"]["input_tokens"] == 20
    assert body["week"]["output_tokens"] == 3
    assert body["week"]["total_tokens"] == 23
    assert body["week"]["calls"] == 2 and body["week"]["unmetered_calls"] == 1
    assert body["today"]["input_tokens"] == 20 and body["today"]["calls"] == 2


def test_period_bounds_use_kst_today_and_monday_start():
    # Sunday 23:59:59 -> following Monday 00:00 KST crosses both day and week.
    before = datetime(2026, 10, 18, 14, 59, 59, tzinfo=UTC)
    after = datetime(2026, 10, 18, 15, 0, 0, tzinfo=UTC)
    assert qwen_usage.period_bounds(before)["week"][0] == datetime(2026, 10, 11, 15, tzinfo=UTC)
    assert qwen_usage.period_bounds(after)["week"][0] == datetime(2026, 10, 18, 15, tzinfo=UTC)


@pytest.mark.asyncio
async def test_actual_http_usage_is_captured_per_stage_and_missing_or_failure_stays_unmetered(monkeypatch):
    saved = []

    async def persist(**row):
        saved.append(row)

    monkeypatch.setattr(qwen_usage, "persist_call", persist)
    replies = [
        httpx.Response(200, json={"choices": [{"message": {"content": "{}"}}],
                                "usage": {"prompt_tokens": 12, "completion_tokens": 3, "total_tokens": 15}}),
        httpx.Response(200, json={"choices": [{"message": {"content": "{}"}}], "usage": {"prompt_tokens": 4}}),
        httpx.Response(503, json={"error": "not ready"}),
    ]

    async def handler(_request):
        return replies.pop(0)

    run_id = uuid4()
    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        with desk_qwen.measure_operational_run(run_id, "http://127.0.0.1:19000"):
            for name in ("desk_kind", "desk_verify", "desk_lead"):
                try:
                    await desk_qwen._chat(client, [], {"type": "object"}, name, 4)
                except httpx.HTTPStatusError:
                    pass
    assert [(x["call_index"], x["stage"], x["status"]) for x in saved] == [
        (0, "kind", "metered"), (1, "verification", "usage_missing"), (2, "lead_selection", "failed")]
    assert saved[0]["prompt_tokens"] == 12 and saved[0]["completion_tokens"] == 3 and saved[0]["total_tokens"] == 15
    assert saved[1]["total_tokens"] is None and saved[2]["total_tokens"] is None


@pytest.mark.asyncio
async def test_remote_qwen_url_never_spawns_a_local_process(monkeypatch):
    # 사용자가 지정한 주소가 죽어 있어도 로컬 모델을 대신 켜지 않는다(서버 수명주기: runs/desk_qwen_service).
    from ttakkari.runs.desk_qwen_service import QwenServer
    monkeypatch.setattr(get_settings(), "desk_qwen_enabled", True)
    monkeypatch.setattr(get_settings(), "desk_qwen_base_url", "https://qwen.example.invalid")
    from ttakkari.runs import desk_qwen_service
    q = QwenServer()
    monkeypatch.setattr(desk_qwen_service, "service", q)
    monkeypatch.setattr("ttakkari.runs.desk_qwen_server.service", q)
    q.state, q.external_ready = "external", False
    with pytest.raises(RuntimeError):
        await desk_qwen_server.ensure_running()
    assert desk_qwen_server.status()["status"] == "unavailable" and q.spawn_count == 0 and q._child is None


@pytest.mark.asyncio
async def test_healthy_configured_server_is_reused_and_never_owned(monkeypatch):
    from ttakkari.runs import desk_qwen_service
    monkeypatch.setattr(get_settings(), "desk_qwen_enabled", True)
    monkeypatch.setattr(get_settings(), "desk_qwen_base_url", "http://127.0.0.1:18080")
    q = desk_qwen_service.QwenServer()
    monkeypatch.setattr(desk_qwen_service, "service", q)
    monkeypatch.setattr("ttakkari.runs.desk_qwen_server.service", q)
    q.state, q.external_ready = "external", True
    assert await desk_qwen_server.ensure_running() == "http://127.0.0.1:18080"
    assert desk_qwen_server.status()["status"] == "external"
    assert q.spawn_count == 0 and q._child is None


@pytest.mark.asyncio
async def test_generation_server_terminates_only_its_owned_child(monkeypatch, tmp_path):
    # 자식 프로세스 소유·종료는 실제 가짜 llama-server 로 확인한다(상세: tests/test_desk_qwen_service.py).
    import time
    from pathlib import Path

    import psutil

    from ttakkari.runs import desk_qwen_service
    fake = Path(__file__).parent / "fake_agents" / "fake_llama_server.py"
    model = tmp_path / "model.gguf"
    model.write_bytes(b"x")
    s = get_settings()
    for key, value in {"home": tmp_path / "home", "desk_qwen_enabled": True, "desk_qwen_manage": True, "desk_qwen_base_url": "",
                       "desk_qwen_server_path": str(fake), "desk_qwen_model_path": str(model),
                       "desk_qwen_min_free_memory_gb": 0.001}.items():
        monkeypatch.setattr(s, key, value)
    q = desk_qwen_service.QwenServer()
    monkeypatch.setattr(desk_qwen_service, "service", q)
    monkeypatch.setattr("ttakkari.runs.desk_qwen_server.service", q)
    await q.start()
    deadline = time.monotonic() + 20
    while q.state != "ready" and time.monotonic() < deadline:
        await asyncio.sleep(0.1)
    assert q.state == "ready" and desk_qwen_server.status()["status"] == "running"
    pid = q._child.pid
    await desk_qwen_server.stop()
    assert not psutil.pid_exists(pid) or psutil.Process(pid).status() == psutil.STATUS_ZOMBIE
    assert desk_qwen_server.status()["status"] == "not_started"


async def _insert_run(db, run_id):
    ws = Workspace(name=f"usage-{run_id.hex[:8]}", root_path=f"/tmp/{run_id.hex}", default_engine="claude")
    db.add(ws)
    await db.flush()
    sess = ChatSession(workspace_id=ws.id, title="usage", engine="claude", kind="desk")
    db.add(sess)
    await db.flush()
    db.add(Run(id=run_id, session_id=sess.id, workspace_id=ws.id, prompt="opaque", engine="qwen",
               model="local", effort=None, status="succeeded", origin="user", usage={}))
    await db.flush()


@pytest.mark.asyncio
async def test_flash_usage_endpoint_is_authenticated_and_independent(client, auth, monkeypatch):
    from ttakkari.runs import qwen_flash_monitor

    async def snapshot():
        return {"scope": "server_lifetime", "input_tokens": 42}

    monkeypatch.setattr(qwen_flash_monitor, "snapshot", snapshot)
    assert (await client.get("/api/accounts/qwen-flash-usage")).status_code == 401
    response = await client.get("/api/accounts/qwen-flash-usage", headers=auth)
    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "no-store"
    assert response.json()["input_tokens"] == 42
    assert "today" in response.json()
    assert "week" in response.json()
    intake = await client.get("/api/accounts/qwen-usage", headers=auth)
    assert intake.json()["today"]["input_tokens"] == 0


@pytest.mark.asyncio
async def test_flash_usage_records_deltas_and_survives_restarts():
    from ttakkari.runs import qwen_flash_monitor

    now = datetime.now(UTC)

    # 1. First observation on a fresh database
    obs1 = {
        "model": "Qwen3.8-Flash-Next Q2_K",
        "server": {"status": "running"},
        "metrics_status": "available",
        "input_tokens": 100,
        "cached_input_tokens": 20,
        "output_tokens": 10,
    }
    async with sessionmaker()() as db:
        await qwen_flash_monitor.record_observation(db, obs1, recorded_at=now)
        summ1 = await qwen_flash_monitor.summary(db, now=now, live=obs1)
    assert summ1["today"]["input_tokens"] == 100
    assert summ1["today"]["cached_input_tokens"] == 20
    assert summ1["today"]["output_tokens"] == 10
    assert summ1["today"]["total_tokens"] == 110

    # 2. Second observation after simulated backend restart (new DB session, monotonic increase)
    obs2 = {
        "model": "Qwen3.8-Flash-Next Q2_K",
        "server": {"status": "running"},
        "metrics_status": "available",
        "input_tokens": 150,
        "cached_input_tokens": 25,
        "output_tokens": 15,
    }
    async with sessionmaker()() as db:
        await qwen_flash_monitor.record_observation(db, obs2, recorded_at=now)
        summ2 = await qwen_flash_monitor.summary(db, now=now, live=obs2)
    # Delta was 50 input, 5 cached, 5 output; cumulative is 150 input, 25 cached, 15 output
    assert summ2["today"]["input_tokens"] == 150
    assert summ2["today"]["cached_input_tokens"] == 25
    assert summ2["today"]["output_tokens"] == 15
    assert summ2["today"]["total_tokens"] == 165

    # 3. Third observation after simulated llama.cpp server restart (counter reset to lower value)
    obs3 = {
        "model": "Qwen3.8-Flash-Next Q2_K",
        "server": {"status": "running"},
        "metrics_status": "available",
        "input_tokens": 30,
        "cached_input_tokens": 10,
        "output_tokens": 5,
    }
    async with sessionmaker()() as db:
        await qwen_flash_monitor.record_observation(db, obs3, recorded_at=now)
        summ3 = await qwen_flash_monitor.summary(db, now=now, live=obs3)
    # Server restarted so 30, 10, 5 is added as a fresh delta on top of existing 150, 25, 15
    assert summ3["today"]["input_tokens"] == 180
    assert summ3["today"]["cached_input_tokens"] == 35
    assert summ3["today"]["output_tokens"] == 20
    assert summ3["today"]["total_tokens"] == 200
    assert summ3["week"]["total_tokens"] == 200


@pytest.mark.asyncio
async def test_flash_partial_token_metrics_do_not_reset_or_double_count_counters():
    from ttakkari.runs import qwen_flash_monitor

    now = datetime.now(UTC)
    full = {
        "server": {"status": "running"},
        "metrics_status": "available",
        "input_tokens": 100,
        "cached_input_tokens": 5,
        "output_tokens": 10,
    }
    partial = {
        "server": {"status": "running"},
        "metrics_status": "partial",
        "input_tokens": None,
        "cached_input_tokens": None,
        "output_tokens": 20,
    }
    recovered = {**full, "input_tokens": 120, "cached_input_tokens": 8, "output_tokens": 30}

    async with sessionmaker()() as db:
        await qwen_flash_monitor.record_observation(db, full, recorded_at=now)
    async with sessionmaker()() as db:
        assert await qwen_flash_monitor.record_observation(db, partial, recorded_at=now + timedelta(seconds=1)) is None
    async with sessionmaker()() as db:
        await qwen_flash_monitor.record_observation(db, recovered, recorded_at=now + timedelta(seconds=2))
        summary = await qwen_flash_monitor.summary(db, now=now + timedelta(seconds=2))

    assert summary["today"]["input_tokens"] == 120
    assert summary["today"]["output_tokens"] == 30
    assert summary["today"]["total_tokens"] == 150


@pytest.mark.asyncio
async def test_flash_usage_is_collected_by_the_background_maintenance_loop(monkeypatch):
    from ttakkari import generation_service
    from ttakkari.runs import qwen_flash_monitor

    observed = {
        "model": "Qwen3.8-Flash-Next Q2_K",
        "server": {"status": "running"},
        "metrics_status": "available",
        "input_tokens": 123,
        "cached_input_tokens": 10,
        "output_tokens": 17,
        "requests_processing": 0,
        "requests_deferred": 0,
    }

    async def snapshot():
        return observed

    monkeypatch.setattr(qwen_flash_monitor, "snapshot", snapshot)
    settings = get_settings().model_copy(update={"qwen_flash_poll_seconds": 60})
    jobs = {name: (callback, interval) for name, callback, interval in
            generation_service.maintenance_jobs(settings)}

    assert "qwen-flash-usage" in jobs
    callback, interval = jobs["qwen-flash-usage"]
    assert interval == 60
    await callback()
    async with sessionmaker()() as db:
        summary = await qwen_flash_monitor.summary(db)
    assert summary["today"]["input_tokens"] == 123
    assert summary["today"]["output_tokens"] == 17
    assert summary["today"]["total_tokens"] == 140

    disabled_jobs = {name for name, _, _ in generation_service.maintenance_jobs(
        settings.model_copy(update={"qwen_flash_poll_seconds": 0}))}
    assert "qwen-flash-usage" not in disabled_jobs
