"""Recovery reuses the same Run, preserves progress, and never revives user stops."""
import importlib
import uuid
from datetime import timedelta
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from ttakkari.db import sessionmaker
from ttakkari.models import ChatSession, Run, RunEvent, RunStatus, utcnow
from ttakkari.runs import events, profiles
from ttakkari.runs.manager import manager

pytestmark = [pytest.mark.security]


@pytest.mark.parametrize("error,expected", [
    ("내부 오류: TimeoutError('QueuePool limit of size 5 overflow 3 reached')", True),
    ("ConnectionResetError: connection reset by peer", True),
    ("HTTP 503 Service Unavailable", True),
    ("connection reached EOF", True),  # Muse 일시 연결 끊김(2026-10-30 하루 10건)
    ("write tcp: broken pipe", True),
    ("Permission denied (quota 429)", False),
    ("403 Forbidden", False),
    ("unknown tool: bad_name", False),
    ("exit code 1", False),
])
def test_only_retryable_errors_are_compensated(error, expected):
    mod = importlib.import_module("ttakkari.runs.manager")
    assert mod.retryable_execution_error(error) is expected


async def test_transient_failure_requeues_same_run_with_bounded_progress(new_session, monkeypatch):
    mod = importlib.import_module("ttakkari.runs.manager")
    ws, sess = await new_session()
    async with sessionmaker()() as db:
        run = Run(session_id=sess["id"], workspace_id=ws["id"], engine="claude",
                  prompt="finish the authorized work", status=RunStatus.running,
                  checkpoint_ref="refs/ttakkari/keep", started_at=utcnow(),
                  resume_handoff="older provider wrote the report; do not recreate")
        db.add(run)
        await db.flush()
        events.append(db, run, "message", {"text": "step one saved"})
        rid = run.id
        assert await manager._retry_execution_failure(db, run, "HTTP 503 Service Unavailable")
        assert run.status == RunStatus.queued
        assert run.resume_after > utcnow()
        assert run.checkpoint_ref == "refs/ttakkari/keep"
        assert "step one saved" in run.resume_handoff
        assert "older provider wrote the report" in run.resume_handoff
        assert run.usage["automatic_recovery"]["attempts"] == 1
        await db.commit()
    manager._disarm(rid)
    async with sessionmaker()() as db:
        run = await db.get(Run, rid)
        for _ in range(mod.MAX_EXECUTION_RECOVERY_ATTEMPTS - 1):
            assert await manager._retry_execution_failure(db, run, "HTTP 503 Service Unavailable")
        assert not await manager._retry_execution_failure(db, run, "HTTP 503 Service Unavailable")


async def test_sweep_recovers_historical_failure_but_preserves_cancel_and_newer_turn(tmp_path, new_session, monkeypatch):
    mod = importlib.import_module("ttakkari.runs.manager")
    monkeypatch.setattr(manager, "dispatch", AsyncMock())
    ids = []
    for state, newer in [(RunStatus.failed, False), (RunStatus.cancelled, False), (RunStatus.failed, True)]:
        ws, sess = await new_session()
        async with sessionmaker()() as db:
            run = Run(session_id=sess["id"], workspace_id=ws["id"], engine="claude",
                      prompt="continue", status=state, finished_at=utcnow(),
                      error="내부 오류: TimeoutError('QueuePool limit reached')")
            db.add(run)
            await db.flush()
            ids.append(run.id)
            if newer:
                db.add(Run(session_id=sess["id"], workspace_id=ws["id"], engine="claude",
                           prompt="newer instruction", status=RunStatus.succeeded,
                           created_at=utcnow() + timedelta(seconds=1)))
            await db.commit()
    from test_generations import generations

    from ttakkari.generation import activate
    async with generations(tmp_path) as (runtime, _standby):
        await activate(runtime.id, expected=None)
        monkeypatch.setattr(manager, "_runtime", runtime)
        summary = await mod.compensate_runs()
    assert summary["requeued"] == 1
    async with sessionmaker()() as db:
        assert (await db.get(Run, ids[0])).status == RunStatus.queued
        assert (await db.get(Run, ids[1])).status == RunStatus.cancelled
        assert (await db.get(Run, ids[2])).status == RunStatus.failed
        receipt = await db.scalar(select(RunEvent).where(RunEvent.run_id == ids[0],
                                                       RunEvent.type == "run.recovery"))
        assert receipt.payload["source"] == "compensation"


async def test_active_coordinator_runs_compensation_before_queue(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from ttakkari import generation_service
    mod = importlib.import_module("ttakkari.runs.manager")
    seen = []
    async def compensate():
        seen.append("compensate")
    async def resume():
        seen.append("resume")
    monkeypatch.setattr(mod, "compensate_runs", compensate, raising=False)
    monkeypatch.setattr(mod, "resume_queued", resume)
    monkeypatch.setattr(generation_service, "_commands", AsyncMock())
    runtime = SimpleNamespace(draining=False)
    await generation_service.coordinate_runs(runtime, SimpleNamespace(remote=False), "active")
    assert seen == ["compensate", "resume"]
    seen.clear()
    await generation_service.coordinate_runs(runtime, SimpleNamespace(remote=False), "standby")
    assert seen == []


async def test_dead_unleased_run_recovers_but_live_or_unknown_pid_does_not(tmp_path, new_session, monkeypatch):
    mod = importlib.import_module("ttakkari.runs.manager")
    monkeypatch.setattr(manager, "dispatch", AsyncMock())
    ids = []
    for pid in (None, 1234):
        ws, sess = await new_session()
        async with sessionmaker()() as db:
            run = Run(session_id=sess["id"], workspace_id=ws["id"], engine="claude", prompt="resume",
                      status=RunStatus.running, pid=pid, heartbeat_at=utcnow() - timedelta(minutes=10))
            db.add(run)
            await db.commit()
            ids.append(run.id)
    monkeypatch.setattr(mod, "_inspect_run_process", lambda pid: "unknown")
    from test_generations import generations

    from ttakkari.generation import activate
    async with generations(tmp_path) as (runtime, _standby):
        await activate(runtime.id, expected=None)
        monkeypatch.setattr(manager, "_runtime", runtime)
        assert (await mod.compensate_runs())["requeued"] == 1
    async with sessionmaker()() as db:
        assert (await db.get(Run, ids[0])).status == RunStatus.queued
        assert (await db.get(Run, ids[1])).status == RunStatus.running


@pytest.mark.parametrize("status", [RunStatus.queued, RunStatus.cancelled, RunStatus.failed])
async def test_dead_owner_releases_lease_even_after_status_was_committed(tmp_path, new_session, monkeypatch, status):
    from test_generations import generations, queued

    from ttakkari import executor_reconcile
    from ttakkari.generation import activate
    from ttakkari.models import RuntimeLease

    run = await queued(new_session)
    async with generations(tmp_path) as (old, _new):
        await activate(old.id, expected=None)
        token = await old.claim(run.id, "workers", 2)
        await old.record_process_group(run.id, token, attempt=1, pid=999991, process_started_at=1, pgid=999991)
        async with sessionmaker()() as db:
            row = await db.get(Run, run.id)
            row.status = status
            await db.commit()
        monkeypatch.setattr(executor_reconcile, "_process_dead", lambda *_: True)
        monkeypatch.setattr(executor_reconcile, "_group_state", lambda _: ("absent", True))
        result = await executor_reconcile.reconcile_orphans(apply=True)
        assert result["reconciled"] == 1
        async with sessionmaker()() as db:
            assert await db.get(RuntimeLease, run.id) is None
            assert (await db.get(Run, run.id)).status == status


@pytest.mark.parametrize("contract_owner", ["parent", "self"])
async def test_completed_parent_goal_never_revives_its_old_failed_worker(tmp_path, new_session, monkeypatch, contract_owner):
    from ttakkari.models import AppSetting
    from ttakkari.orchestrator.work import key
    ws, parent = await new_session()
    async with sessionmaker()() as db:
        child = ChatSession(workspace_id=ws["id"], parent_id=parent["id"], title="old failed worker",
                            engine="claude", kind="worker")
        db.add(child)
        await db.flush()
        run = Run(session_id=child.id, workspace_id=ws["id"], engine="claude", prompt="old task",
                  status=RunStatus.failed, error="HTTP 503 Service Unavailable", finished_at=utcnow())
        db.add(run)
        owner = uuid.UUID(parent["id"]) if contract_owner == "parent" else child.id
        db.add(AppSetting(key=key(owner), value={"revision": 1, "state": {"status": "complete"}}))
        await db.commit()
        rid = run.id
    mod = importlib.import_module("ttakkari.runs.manager")
    from test_generations import generations

    from ttakkari.generation import activate
    async with generations(tmp_path) as (runtime, _standby):
        await activate(runtime.id, expected=None)
        monkeypatch.setattr(manager, "_runtime", runtime)
        assert (await mod.compensate_runs())["requeued"] == 0
    async with sessionmaker()() as db:
        assert (await db.get(Run, rid)).status == RunStatus.failed


async def test_one_compensation_failure_does_not_block_normal_queue(monkeypatch):
    from types import SimpleNamespace

    from ttakkari import generation_service
    mod = importlib.import_module("ttakkari.runs.manager")
    monkeypatch.setattr(mod, "compensate_runs", AsyncMock(side_effect=RuntimeError("one bad recovery record")))
    resume = AsyncMock()
    monkeypatch.setattr(mod, "resume_queued", resume)
    monkeypatch.setattr(generation_service, "_commands", AsyncMock())
    await generation_service.coordinate_runs(SimpleNamespace(draining=False), SimpleNamespace(remote=False), "active")
    resume.assert_awaited_once()


@pytest.mark.parametrize("text,expected", [
    ("connection reached EOF", profiles.TRANSIENT_COOLDOWN_SECONDS),
    ("unexpected EOF while reading", profiles.TRANSIENT_COOLDOWN_SECONDS),
    ("stream disconnected before completion", profiles.TRANSIENT_COOLDOWN_SECONDS),
    ("Permission denied: connection reached EOF", None),  # 권한 오류 줄은 다른 공급자로 재생하지 않는다
    ("Error: Model \"auto\" does not support reasoning effort configuration", profiles.ENGINE_BROKEN_COOLDOWN_SECONDS),
    ("exit code 1", None),
])
def test_transient_engine_errors_cool_the_account_instead_of_failing_for_good(text, expected):
    assert profiles.transient_cooldown(text) == expected
