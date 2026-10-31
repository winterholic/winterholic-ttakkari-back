"""재현 및 검증 테스트: 세션 소유권과 배포 세대를 넘어 동작하는 에이전트 전부 중지."""
from __future__ import annotations

import asyncio
import uuid
from datetime import timedelta

from sqlalchemy import select

from ttakkari.db import sessionmaker
from ttakkari.models import (
    ChatSession,
    Run,
    RunOrigin,
    RunStatus,
    RuntimeCommand,
    utcnow,
)
from ttakkari.orchestrator.runtime import issue_token
from ttakkari.runs.manager import manager


async def create_session(client, auth, make_dir, *, kind="orchestrator", pool="personal", workspace=None, parent=None):
    if workspace is None:
        root = make_dir()
        r = await client.post("/api/workspaces", headers=auth, json={"name": root.name, "root_path": str(root)})
        assert r.status_code == 201, r.text
        workspace = r.json()["id"]
    r = await client.post("/api/sessions", headers=auth, json={"workspace_id": workspace, "kind": "chat" if kind == "worker" else kind, "pool": pool})
    assert r.status_code == 201, r.text
    s = r.json()
    if parent or kind == "worker":
        async with sessionmaker()() as db:
            row = await db.get(ChatSession, uuid.UUID(s["id"]))
            row.kind = kind
            row.parent_id = uuid.UUID(parent) if parent else None
            await db.commit()
    return s, {"Authorization": "Bearer " + issue_token(s["id"])}


async def create_run(sess, **kw):
    kw.setdefault("prompt", "test prompt")
    async with sessionmaker()() as db:
        r = Run(
            session_id=uuid.UUID(sess["id"]),
            workspace_id=uuid.UUID(sess["workspace_id"]),
            engine="claude",
            **kw,
        )
        db.add(r)
        await db.commit()
        return r


async def test_cross_orchestrator_worker_stop(client, auth, make_dir):
    """재현 1: 다른 오케스트레이터 소유의 작업자(AIP 등)도 전역 중지에서 멈춰야 한다."""
    orch1, h1 = await create_session(client, auth, make_dir)
    orch2, _ = await create_session(client, auth, make_dir)
    worker2, _ = await create_session(client, auth, make_dir, kind="worker", parent=orch2["id"])

    run_orch1 = await create_run(orch1)
    run_orch2 = await create_run(orch2)
    run_worker2 = await create_run(worker2)

    # orch1이 전역 중지 요청
    r = await client.post("/api/orchestrator/stop-all", headers=h1)
    assert r.status_code == 200, r.text
    res = r.json()
    assert str(run_worker2.id) in res["stopped"]
    assert str(run_orch2.id) in res["stopped"]
    assert str(run_orch1.id) in res["stopped"]

    async with sessionmaker()() as db:
        for r_id in (run_orch1.id, run_orch2.id, run_worker2.id):
            row = await db.get(Run, r_id)
            assert row.status == RunStatus.cancelled


async def test_resume_after_quota_wait_cancelled(client, auth, make_dir):
    """재현 2: 한도 대기(resume_after) 상태인 Run도 취소되고 재무장 타이머가 해제되어야 한다."""
    orch, h = await create_session(client, auth, make_dir)
    future_time = utcnow() + timedelta(hours=1)
    run = await create_run(orch, resume_after=future_time)
    manager._arm(run.id, uuid.UUID(orch["id"]), future_time.timestamp())

    assert manager.is_waiting(run.id)

    r = await client.post("/api/orchestrator/stop-all", headers=h)
    assert r.status_code == 200, r.text
    assert str(run.id) in r.json()["stopped"]

    assert not manager.is_waiting(run.id)
    async with sessionmaker()() as db:
        row = await db.get(Run, run.id)
        assert row.status == RunStatus.cancelled
        assert row.resume_after is None


async def test_other_generation_running_run_cancelled(client, auth, make_dir, tmp_path, monkeypatch):
    """재현 3: 다른 세대(draining)가 소유한 running Run도 cancel RuntimeCommand로 취소되어야 한다."""
    from ttakkari import generation
    from ttakkari.generation import activate
    from ttakkari.generation_service import _commands
    from ttakkari.runs.manager import RunManager

    orch, h = await create_session(client, auth, make_dir)
    worker, _ = await create_session(client, auth, make_dir, kind="worker", parent=orch["id"])
    run = await create_run(worker)

    async with sessionmaker()() as db:
        row = await db.get(Run, run.id)
        row.prompt = "SLEEP"
        await db.commit()

    gen_a = generation.GenerationRuntime(tmp_path, revision="a" * 40, api_url="http://127.0.0.1:18791")
    gen_b = generation.GenerationRuntime(tmp_path, revision="b" * 40, api_url="http://127.0.0.1:18792")
    await gen_a.register()
    await gen_b.register()

    owner = RunManager(runtime=gen_a)
    try:
        await activate(gen_a.id, expected=None)
        owner.start(run.id)
        for _ in range(150):
            handle = owner._handles.get(run.id)
            if handle and handle.proc:
                break
            await asyncio.sleep(0.03)
        assert handle and handle.proc and handle.proc.returncode is None

        # 세대 B가 활성화됨 (세대 A는 draining)
        await activate(gen_b.id, expected=gen_a.id)
        monkeypatch.setattr(generation, "current", gen_b)
        monkeypatch.setattr(manager, "_runtime", gen_b)

        # 세대 B 환경에서 전역 중지 실행
        r = await client.post("/api/orchestrator/stop-all", headers=h)
        assert r.status_code == 200, r.text

        body = r.json()
        # 소유 세대가 아직 집행하기 전이므로 멈춘 것으로 세지 않고 이유와 함께 돌려준다(조용히 건너뛰지 않는다).
        assert str(run.id) in body["remaining"]
        assert any(u["run_id"] == str(run.id) and "세대" in u["reason"] for u in body["unstopped"])

        # 세대 A 앞으로 cancel RuntimeCommand가 발행되었는지 검증
        cmds = await gen_a.commands()
        assert any(c.kind == "cancel" and c.run_id == run.id for c in cmds)

        # 세대 A가 명령을 수신하여 프로세스 취소 처리
        await _commands(gen_a, owner)
        await asyncio.wait_for(handle.task, 5)
        assert handle.proc.returncode is not None

        async with sessionmaker()() as db:
            row = await db.get(Run, run.id)
            assert row.status == RunStatus.cancelled
    finally:
        if handle and handle.proc and handle.proc.returncode is None:
            await owner._terminate(handle.proc)


async def test_stop_all_suppresses_worker_reports(client, auth, make_dir):
    """재현 4: 취소된 작업자로부터 오케스트레이터에게 새 보고 Run이 생성되지 않아야 한다."""
    orch, h = await create_session(client, auth, make_dir)
    worker, _ = await create_session(client, auth, make_dir, kind="worker", parent=orch["id"])
    await create_run(worker)

    r = await client.post("/api/orchestrator/stop-all", headers=h)
    assert r.status_code == 200, r.text

    async with sessionmaker()() as db:
        # parent(orch)에게 새 report run이 없어야 함
        report = await db.scalar(select(Run).where(Run.session_id == uuid.UUID(orch["id"]), Run.origin == RunOrigin.report))
        assert report is None


async def test_work_pool_scope_isolation(client, auth, make_dir):
    """검증 5: 지정 없는 중지는 같은 workspace만 건드리고, 명시 지정 중지는 표시 분류를 넘나든다."""
    work1, h_work1 = await create_session(client, auth, make_dir, pool="work")
    # 같은 work workspace의 다른 세션
    same_work, _ = await create_session(client, auth, make_dir, pool="work", workspace=work1["workspace_id"])
    # 다른 work workspace의 세션
    other_work, _ = await create_session(client, auth, make_dir, pool="work")
    # personal 세션
    personal, _ = await create_session(client, auth, make_dir, pool="personal")

    r_same = await create_run(same_work)
    r_other = await create_run(other_work)
    r_personal = await create_run(personal)

    # work1 오케스트레이터가 stop-all 요청
    r = await client.post("/api/orchestrator/stop-all", headers=h_work1)
    assert r.status_code == 200, r.text
    res = r.json()
    assert str(r_same.id) in res["stopped"]
    assert str(r_other.id) not in res["stopped"]
    assert str(r_personal.id) not in res["stopped"]

    # An explicit target may cross display groups; untargeted stop-all stays local.
    targeted = await client.post(f"/api/orchestrator/agents/{other_work['id']}/stop", headers=h_work1)
    assert targeted.status_code == 200
    assert str(r_other.id) in targeted.json()["stopped"]


async def test_scheduled_stop_all_action(client, auth, make_dir):
    """검증 6: ScheduledAction의 stop_all 액션도 강화된 전역 중지 로직을 수행한다."""
    from ttakkari.schedules.service import create_schedule, execute_action

    orch, _ = await create_session(client, auth, make_dir)
    run = await create_run(orch)

    async with sessionmaker()() as db:
        item = await create_schedule(db, action_type="stop_all", due_at="+0s", payload={"scope_pool": "all"})

    res = await execute_action(item)
    assert str(run.id) in res["stopped"]
    assert res["count"] >= 1

    async with sessionmaker()() as db:
        row = await db.get(Run, run.id)
        assert row.status == RunStatus.cancelled


async def test_system_stop_all_endpoint(client, auth, make_dir):
    """검증 7: /api/system/stop-all 엔드포인트도 강화된 전역 중지 로직을 반환한다."""
    orch, _ = await create_session(client, auth, make_dir)
    run = await create_run(orch)

    r = await client.post("/api/system/stop-all", headers=auth)
    assert r.status_code == 200, r.text
    assert str(run.id) in r.json()["stopped"]
    assert "remaining" in r.json()
    assert "unstopped" in r.json()


async def test_stop_worker_keeps_owner_restriction(client, auth, make_dir):
    """일반 stop_worker 는 다른 오케스트레이터의 작업자를 계속 거부한다. 전역 중지만 소유권을 넘는다."""
    _orch1, h1 = await create_session(client, auth, make_dir)
    orch2, _ = await create_session(client, auth, make_dir)
    worker2, _ = await create_session(client, auth, make_dir, kind="worker", parent=orch2["id"])
    run = await create_run(worker2)

    denied = await client.post(f"/api/orchestrator/workers/{worker2['id']}/stop", headers=h1)
    assert denied.status_code == 404
    async with sessionmaker()() as db:
        assert (await db.get(Run, run.id)).status == RunStatus.queued

    r = await client.post(f"/api/orchestrator/agents/{worker2['id']}/stop", headers=h1)
    assert r.status_code == 200, r.text
    assert str(run.id) in r.json()["stopped"]


async def test_interrupted_restart_resume_candidate_is_cancelled(client, auth, make_dir):
    """재배포로 중단돼 재개를 기다리는 Run 도 전역 중지 뒤 되살아나면 안 된다."""
    from ttakkari.runs.manager import resume_interrupted

    orch, h = await create_session(client, auth, make_dir)
    run = await create_run(orch, status=RunStatus.interrupted, error="서버 종료로 중단")

    r = await client.post("/api/orchestrator/stop-all", headers=h)
    assert r.status_code == 200, r.text
    assert str(run.id) in r.json()["stopped"]

    await resume_interrupted()
    async with sessionmaker()() as db:
        row = await db.get(Run, run.id)
        assert row.status == RunStatus.cancelled


async def test_report_from_unaware_generation_is_dropped_at_dispatch(client, auth, make_dir):
    """표식을 모르는 구세대가 만든 '취소 보고'는 신세대 dispatch 가 시작 전에 버린다. 정상 보고는 남긴다."""
    from ttakkari.runs.agent_control import (
        drop_suppressed_reports,
        mark_report_suppressed,
    )

    orch, _ = await create_session(client, auth, make_dir)
    worker, _ = await create_session(client, auth, make_dir, kind="worker", parent=orch["id"])
    other, _ = await create_session(client, auth, make_dir, kind="worker", parent=orch["id"])
    wrun = await create_run(worker, status=RunStatus.cancelled)
    async with sessionmaker()() as db:
        await mark_report_suppressed(db, await db.get(Run, wrun.id))
        await db.commit()

    stale = await create_run(orch, origin=RunOrigin.report,
                             prompt=f"[작업 보고] 작업 세션 'x' (worker_id={worker['id']}) 취소.")
    legit = await create_run(orch, origin=RunOrigin.report,
                             prompt=f"[작업 보고] 작업 세션 'y' (worker_id={other['id']}) 취소.")
    async with sessionmaker()() as db:
        rows = [await db.get(Run, stale.id), await db.get(Run, legit.id)]
        kept = await drop_suppressed_reports(db, rows)
        await db.commit()
        assert [r.id for r in kept] == [legit.id]
        assert rows[0].status == RunStatus.cancelled


async def test_stop_all_sweeps_reports_created_by_old_generation(client, auth, make_dir):
    """중지 직전에 구세대가 부모에게 넣은 대기 보고 Run 도 범위 안이면 함께 취소된다."""
    from ttakkari.runs.agent_control import _sweep_reports

    orch, _ = await create_session(client, auth, make_dir)
    report = await create_run(orch, origin=RunOrigin.report, prompt="[작업 보고] 취소")
    assert await _sweep_reports("personal", None, None, "test") == 1
    async with sessionmaker()() as db:
        assert (await db.get(Run, report.id)).status == RunStatus.cancelled


async def test_personal_orchestrator_can_target_work_session(client, auth, make_dir):
    _personal, hp = await create_session(client, auth, make_dir, pool="personal")
    work, _ = await create_session(client, auth, make_dir, pool="work")
    await create_run(work)
    targeted = await client.post(f"/api/orchestrator/agents/{work['id']}/stop", headers=hp)
    assert targeted.status_code == 200


async def test_resume_interrupted_honors_pending_cancel(client, auth, make_dir):
    """소유 세대 집행 전에 세대가 죽어 interrupted 로 남은 Run 은 재개하지 않고 취소로 마감한다."""
    from ttakkari.runs.manager import resume_interrupted

    sess, _ = await create_session(client, auth, make_dir)
    run = await create_run(sess, status=RunStatus.interrupted, error="서버 종료로 중단")
    async with sessionmaker()() as db:
        db.add(RuntimeCommand(key=f"cancel-{run.id}", kind="cancel", run_id=run.id, generation_id=None))
        await db.commit()
    await resume_interrupted()
    async with sessionmaker()() as db:
        row = await db.get(Run, run.id)
        assert row.status == RunStatus.cancelled
        assert row.resume_after is None


async def test_scheduled_stop_all_scope_is_clamped_to_orchestrator(client, auth, make_dir):
    """지정 없는 예약 중지는 페이로드로 범위를 넓힐 수 없고, 명시 세션 지정은 분류를 넘나든다."""
    work, hw = await create_session(client, auth, make_dir, pool="work")
    other, _ = await create_session(client, auth, make_dir, pool="personal")
    r = await client.post("/api/orchestrator/schedules", headers=hw,
                          json={"action_type": "stop_all", "due_at": "+3600s", "payload": {"scope_pool": "all"}})
    assert r.status_code == 201, r.text
    payload = r.json()["payload"]
    assert payload["scope_pool"] == "work"
    assert payload["workspace_id"] == work["workspace_id"]

    targeted = await client.post("/api/orchestrator/schedules", headers=hw,
                                 json={"action_type": "stop_all", "due_at": "+3600s",
                                       "payload": {"session_id": other["id"]}})
    assert targeted.status_code == 201
    assert targeted.json()["payload"]["scope_pool"] == "all"
    assert targeted.json()["payload"]["session_id"] == other["id"]


async def seed_orphan(sess):
    from ttakkari.models import RuntimeGeneration, RuntimeLease, RuntimeProcessGroup

    run = await create_run(sess, status=RunStatus.running)
    owner_id, token = uuid.uuid4(), uuid.uuid4()
    async with sessionmaker()() as db:
        db.add(RuntimeGeneration(id=owner_id, revision="a" * 40, api_url="http://127.0.0.1:18793",
                                pid=987654321, process_started_at=1.0, state="offline"))
        await db.flush()
        db.add(RuntimeLease(run_id=run.id, session_id=run.session_id, generation_id=owner_id,
                            token=token, lane="leads"))
        db.add(RuntimeProcessGroup(run_id=run.id, generation_id=owner_id, lease_token=token,
                                   attempt=1, pid=987654320, pgid=987654320, process_started_at=1.0))
        await db.commit()
    return run


async def test_ui_cancel_releases_dead_owner_without_touching_other_runs(client, auth, make_dir, tmp_path, monkeypatch):
    from ttakkari import executor_reconcile, generation
    from ttakkari.models import RuntimeLease, RuntimeProcessGroup

    sess, _ = await create_session(client, auth, make_dir)
    other, _ = await create_session(client, auth, make_dir)
    run, untouched = await seed_orphan(sess), await seed_orphan(other)
    runtime = generation.GenerationRuntime(tmp_path, revision="b" * 40, api_url="http://127.0.0.1:18792")
    await runtime.register()
    monkeypatch.setattr(manager, "_runtime", runtime)
    monkeypatch.setattr(executor_reconcile, "_process_dead", lambda *_: True)
    monkeypatch.setattr(executor_reconcile, "_group_state", lambda _: ("absent", True))

    response = await client.post(f"/api/runs/{run.id}/cancel", headers=auth)
    assert response.status_code == 200, response.text
    assert response.json()["status"] == RunStatus.cancelled
    async with sessionmaker()() as db:
        assert await db.get(RuntimeLease, run.id) is None
        assert await db.scalar(select(RuntimeProcessGroup.id).where(RuntimeProcessGroup.run_id == run.id)) is None
        assert (await db.get(Run, untouched.id)).status == RunStatus.running
        assert await db.get(RuntimeLease, untouched.id) is not None
        pending = await db.scalar(select(RuntimeCommand.id).where(
            RuntimeCommand.run_id == run.id, RuntimeCommand.kind == "cancel", RuntimeCommand.acknowledged_at.is_(None)))
        assert pending is None


async def test_targeted_stop_releases_dead_owner(client, auth, make_dir, tmp_path, monkeypatch):
    from ttakkari import executor_reconcile, generation
    from ttakkari.runs.agent_control import stop_all_agents

    sess, _ = await create_session(client, auth, make_dir)
    run = await seed_orphan(sess)
    runtime = generation.GenerationRuntime(tmp_path, revision="b" * 40, api_url="http://127.0.0.1:18792")
    await runtime.register()
    monkeypatch.setattr(manager, "_runtime", runtime)
    monkeypatch.setattr(executor_reconcile, "_process_dead", lambda *_: True)
    monkeypatch.setattr(executor_reconcile, "_group_state", lambda _: ("absent", True))
    async with sessionmaker()() as db:
        result = await stop_all_agents(db, session_id=uuid.UUID(sess["id"]), wait_timeout=0)
    assert result["stopped"] == [str(run.id)]
    assert result["remaining"] == []


async def test_orphan_cancel_requires_persisted_request(client, auth, make_dir, monkeypatch):
    from ttakkari import executor_reconcile
    from ttakkari.models import RuntimeLease

    sess, _ = await create_session(client, auth, make_dir)
    run = await seed_orphan(sess)
    monkeypatch.setattr(executor_reconcile, "_process_dead", lambda *_: True)
    monkeypatch.setattr(executor_reconcile, "_group_state", lambda _: ("absent", True))
    assert await executor_reconcile.cancel_orphan(run.id) is False
    async with sessionmaker()() as db:
        assert (await db.get(Run, run.id)).status == RunStatus.running
        assert await db.get(RuntimeLease, run.id) is not None


async def test_orphan_cancel_preserves_unknown_group(client, auth, make_dir, tmp_path, monkeypatch):
    from ttakkari import executor_reconcile, generation
    from ttakkari.models import RuntimeLease

    sess, _ = await create_session(client, auth, make_dir)
    run = await seed_orphan(sess)
    runtime = generation.GenerationRuntime(tmp_path, revision="b" * 40, api_url="http://127.0.0.1:18792")
    await runtime.register()
    monkeypatch.setattr(manager, "_runtime", runtime)
    monkeypatch.setattr(executor_reconcile, "_process_dead", lambda *_: True)
    monkeypatch.setattr(executor_reconcile, "_group_state", lambda _: ("unknown", False))
    assert await manager.cancel(run.id) is True  # Request accepted, termination unverified.
    async with sessionmaker()() as db:
        assert (await db.get(Run, run.id)).status == RunStatus.running
        assert await db.get(RuntimeLease, run.id) is not None
        assert await db.scalar(select(RuntimeCommand.id).where(
            RuntimeCommand.run_id == run.id, RuntimeCommand.kind == "cancel", RuntimeCommand.acknowledged_at.is_(None)))


async def seed_self_owned(sess, *, heartbeat_age=600, group_pid=987654320):
    """이 프로세스가 소유한 lease 와 이미 사라진 프로세스 그룹을 가진 running Run(핸들 분실 재현)."""
    import os

    from ttakkari.models import RuntimeGeneration, RuntimeLease, RuntimeProcessGroup

    run = await create_run(sess, status=RunStatus.running)
    owner_id, token = uuid.uuid4(), uuid.uuid4()
    async with sessionmaker()() as db:
        row = await db.get(Run, run.id)
        row.heartbeat_at = utcnow() - timedelta(seconds=heartbeat_age)
        db.add(RuntimeGeneration(id=owner_id, revision="a" * 40, api_url="http://127.0.0.1:18794",
                                 pid=os.getpid(), process_started_at=1.0, state="draining"))
        await db.flush()
        db.add(RuntimeLease(run_id=run.id, session_id=run.session_id, generation_id=owner_id,
                            token=token, lane="leads"))
        db.add(RuntimeProcessGroup(run_id=run.id, generation_id=owner_id, lease_token=token,
                                   attempt=1, pid=group_pid, pgid=group_pid, process_started_at=1.0))
        await db.commit()
    return run, owner_id


async def _lease_gone(run_id) -> bool:
    from ttakkari.models import RuntimeLease, RuntimeProcessGroup
    async with sessionmaker()() as db:
        return (await db.get(RuntimeLease, run_id) is None
                and await db.scalar(select(RuntimeProcessGroup.id).where(RuntimeProcessGroup.run_id == run_id)) is None)


async def test_handleless_run_is_reaped_to_interrupted(client, auth, make_dir, monkeypatch):
    from ttakkari import executor_reconcile

    sess, _ = await create_session(client, auth, make_dir)
    run, owner_id = await seed_self_owned(sess)
    monkeypatch.setattr(executor_reconcile, "_group_state", lambda _: ("absent", True))
    assert await executor_reconcile.reap_handleless_runs(generation_id=owner_id, handles=()) == 1
    async with sessionmaker()() as db:
        row = await db.get(Run, run.id)
        assert row.status == RunStatus.interrupted and row.pid is None
    assert await _lease_gone(run.id)


async def test_handleless_run_with_pending_cancel_becomes_cancelled(client, auth, make_dir, monkeypatch):
    from ttakkari import executor_reconcile

    sess, _ = await create_session(client, auth, make_dir)
    run, owner_id = await seed_self_owned(sess)
    async with sessionmaker()() as db:
        db.add(RuntimeCommand(key=str(uuid.uuid4()), kind="cancel", run_id=run.id,
                              session_id=run.session_id, generation_id=owner_id))
        await db.commit()
    monkeypatch.setattr(executor_reconcile, "_group_state", lambda _: ("absent", True))
    assert await executor_reconcile.reap_handleless_runs(generation_id=owner_id, handles=()) == 1
    async with sessionmaker()() as db:
        assert (await db.get(Run, run.id)).status == RunStatus.cancelled
        assert await db.scalar(select(RuntimeCommand.id).where(
            RuntimeCommand.run_id == run.id, RuntimeCommand.acknowledged_at.is_(None))) is None
    assert await _lease_gone(run.id)


async def test_run_with_live_handle_is_never_reaped(client, auth, make_dir, monkeypatch):
    from ttakkari import executor_reconcile

    sess, _ = await create_session(client, auth, make_dir)
    run, owner_id = await seed_self_owned(sess)
    monkeypatch.setattr(executor_reconcile, "_group_state", lambda _: ("absent", True))
    assert await executor_reconcile.reap_handleless_runs(generation_id=owner_id, handles=(run.id,)) == 0
    async with sessionmaker()() as db:
        assert (await db.get(Run, run.id)).status == RunStatus.running


async def test_run_with_live_or_unknown_process_group_is_never_reaped(client, auth, make_dir, monkeypatch):
    from ttakkari import executor_reconcile

    sess, _ = await create_session(client, auth, make_dir)
    run, owner_id = await seed_self_owned(sess)
    for state in (("present", True), ("unknown", False), ("absent", False)):
        monkeypatch.setattr(executor_reconcile, "_group_state", lambda _, s=state: s)
        assert await executor_reconcile.reap_handleless_runs(generation_id=owner_id, handles=()) == 0
    async with sessionmaker()() as db:
        assert (await db.get(Run, run.id)).status == RunStatus.running
    assert not await _lease_gone(run.id)


async def test_fresh_heartbeat_run_is_not_reaped(client, auth, make_dir, monkeypatch):
    from ttakkari import executor_reconcile

    sess, _ = await create_session(client, auth, make_dir)
    run, owner_id = await seed_self_owned(sess, heartbeat_age=5)
    monkeypatch.setattr(executor_reconcile, "_group_state", lambda _: ("absent", True))
    assert await executor_reconcile.reap_handleless_runs(generation_id=owner_id, handles=()) == 0
    async with sessionmaker()() as db:
        assert (await db.get(Run, run.id)).status == RunStatus.running


async def test_other_generation_and_other_process_leases_are_untouched(client, auth, make_dir, monkeypatch):
    from ttakkari import executor_reconcile

    sess, _ = await create_session(client, auth, make_dir)
    _, owner_id = await seed_self_owned(sess)
    other_run = await seed_orphan(sess)  # 다른 pid(987654321)가 소유한 lease
    monkeypatch.setattr(executor_reconcile, "_group_state", lambda _: ("absent", True))
    assert await executor_reconcile.reap_handleless_runs(generation_id=uuid.uuid4(), handles=()) == 0
    assert await executor_reconcile.reap_handleless_runs(generation_id=owner_id, handles=()) == 1
    async with sessionmaker()() as db:
        assert (await db.get(Run, other_run.id)).status == RunStatus.running
    assert not await _lease_gone(other_run.id)


async def test_reaper_is_idempotent(client, auth, make_dir, monkeypatch):
    from ttakkari import executor_reconcile

    sess, _ = await create_session(client, auth, make_dir)
    _, owner_id = await seed_self_owned(sess)
    monkeypatch.setattr(executor_reconcile, "_group_state", lambda _: ("absent", True))
    assert await executor_reconcile.reap_handleless_runs(generation_id=owner_id, handles=()) == 1
    assert await executor_reconcile.reap_handleless_runs(generation_id=owner_id, handles=()) == 0
