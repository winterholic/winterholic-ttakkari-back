"""영속 예약 작업(ScheduledAction) 및 세대 전환/중복 방지 테스트."""
from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta

import pytest

from ttakkari.db import sessionmaker
from ttakkari.generation import GenerationRuntime, activate
from ttakkari.models import ChatSession, Run, RunStatus, utcnow
from ttakkari.schedules import (
    acquire_due_schedules,
    cancel_schedule,
    create_schedule,
    execute_action,
    get_schedule,
    list_schedules,
    parse_due_at,
    process_due_schedules,
)


def test_scheduled_actions_migration_in_chain():
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    script = ScriptDirectory.from_config(Config("alembic.ini"))
    rev = script.get_revision("0057_scheduled_actions")
    assert rev is not None
    assert rev.down_revision == "0056_research_lab"


def test_parse_due_at():
    now = datetime(2026, 10, 20, 12, 0, 0, tzinfo=UTC)
    # 상대 시간 파싱
    assert parse_due_at("+10m", base=now) == now + timedelta(minutes=10)
    assert parse_due_at("+300s", base=now) == now + timedelta(seconds=300)
    assert parse_due_at("+1h", base=now) == now + timedelta(hours=1)
    assert parse_due_at("+2d", base=now) == now + timedelta(days=2)
    assert parse_due_at("+60", base=now) == now + timedelta(seconds=60)
    assert parse_due_at("30", base=now) == now + timedelta(seconds=30)

    # 타임스탬프
    ts = now.timestamp()
    assert parse_due_at(ts) == now

    # ISO 문자열
    iso_z = "2026-10-21T04:30:00Z"
    assert parse_due_at(iso_z) == datetime(2026, 10, 21, 4, 30, 0, tzinfo=UTC)
    iso_offset = "2026-10-21T04:30:00+09:00"
    parsed_kst = parse_due_at(iso_offset)
    assert parsed_kst.astimezone(UTC) == datetime(2026, 10, 20, 19, 30, 0, tzinfo=UTC)


async def test_create_list_and_cancel_schedule():
    async with sessionmaker()() as db:
        item = await create_schedule(
            db,
            action_type="stop_all",
            due_at="+10m",
            description="테스트 정지 예약",
        )
        assert item.status == "pending"
        assert item.action_type == "stop_all"
        assert item.description == "테스트 정지 예약"

        # 단건 조회
        fetched = await get_schedule(db, item.id)
        assert fetched is not None
        assert fetched.id == item.id

        # 목록 조회
        pending_list = await list_schedules(db, status="pending")
        assert any(s.id == item.id for s in pending_list)

        # 취소
        cancelled = await cancel_schedule(db, item.id)
        assert cancelled is True

        refetched = await get_schedule(db, item.id)
        assert refetched.status == "cancelled"

        # 이미 취소된 것은 다시 취소 불가
        assert await cancel_schedule(db, item.id) is False


async def test_invalid_schedule_payload_rejected():
    async with sessionmaker()() as db:
        # cancel_run without run_id
        with pytest.raises(ValueError, match="run_id"):
            await create_schedule(db, action_type="cancel_run", due_at="+5m", payload={})

        # send_message without session_id
        with pytest.raises(ValueError, match="session_id"):
            await create_schedule(db, action_type="send_message", due_at="+5m", payload={"message": "hi"})

        # send_message without message/prompt
        with pytest.raises(ValueError, match="message"):
            await create_schedule(db, action_type="send_message", due_at="+5m",
                                  payload={"session_id": str(uuid.uuid4())})


async def test_execute_action_stop_all(new_session):
    ws, sess = await new_session()
    async with sessionmaker()() as db:
        r1 = Run(session_id=uuid.UUID(sess["id"]), workspace_id=uuid.UUID(ws["id"]),
                 prompt="run 1", engine="claude", status=RunStatus.queued)
        r2 = Run(session_id=uuid.UUID(sess["id"]), workspace_id=uuid.UUID(ws["id"]),
                 prompt="run 2", engine="claude", status=RunStatus.queued)
        db.add_all([r1, r2])
        await db.commit()
        await db.refresh(r1)
        await db.refresh(r2)

        sched = await create_schedule(db, action_type="stop_all", due_at="-10s")

    # execute stop_all
    res = await execute_action(sched)
    assert res["count"] >= 2
    assert str(r1.id) in res["stopped"]
    assert str(r2.id) in res["stopped"]

    async with sessionmaker()() as db:
        assert (await db.get(Run, r1.id)).status == RunStatus.cancelled
        assert (await db.get(Run, r2.id)).status == RunStatus.cancelled


async def test_execute_action_cancel_run(new_session):
    ws, sess = await new_session()
    async with sessionmaker()() as db:
        target = Run(session_id=uuid.UUID(sess["id"]), workspace_id=uuid.UUID(ws["id"]),
                     prompt="target run", engine="claude", status=RunStatus.queued)
        other = Run(session_id=uuid.UUID(sess["id"]), workspace_id=uuid.UUID(ws["id"]),
                    prompt="other run", engine="claude", status=RunStatus.queued)
        db.add_all([target, other])
        await db.commit()
        await db.refresh(target)
        await db.refresh(other)

        sched = await create_schedule(db, action_type="cancel_run", due_at="-10s",
                                      payload={"run_id": str(target.id)})

    res = await execute_action(sched)
    assert res["cancelled"] is True
    assert res["run_id"] == str(target.id)

    async with sessionmaker()() as db:
        assert (await db.get(Run, target.id)).status == RunStatus.cancelled
        assert (await db.get(Run, other.id)).status == RunStatus.queued


async def test_execute_action_send_message(new_session):
    _ws, sess = await new_session()
    sid = uuid.UUID(sess["id"])
    async with sessionmaker()() as db:
        sched = await create_schedule(db, action_type="send_message", due_at="-10s",
                                      payload={"session_id": str(sid), "message": "예약 알림 메시지"})

    res = await execute_action(sched)
    assert res["session_id"] == str(sid)
    queued_run_id = uuid.UUID(res["queued_run_id"])

    async with sessionmaker()() as db:
        run = await db.get(Run, queued_run_id)
        assert run is not None
        assert run.session_id == sid
        assert run.prompt == "예약 알림 메시지"


async def test_two_generations_concurrent_polling_zero_duplicates():
    """복수 세대가 동시에 due 작업을 폴링할 때 FOR UPDATE SKIP LOCKED 로 중복 없이 각 작업이 1회만 차지됨을 검증."""
    created_ids = []
    async with sessionmaker()() as db:
        for i in range(8):
            item = await create_schedule(db, action_type="stop_all", due_at="-10s",
                                         description=f"concurrent-test-{i}")
            created_ids.append(item.id)

    # 4개의 동시 폴러 시뮬레이션
    async def poll_batch(owner: str):
        async with sessionmaker()() as db:
            leased = await acquire_due_schedules(db, owner_id=owner, limit=10)
            await db.commit()
            return [s.id for s in leased]

    results = await asyncio.gather(
        poll_batch("gen-1"),
        poll_batch("gen-2"),
        poll_batch("gen-3"),
        poll_batch("gen-4"),
    )

    all_leased = [sid for sublist in results for sid in sublist]
    # 생성된 8개 모두 차지되었고
    assert set(created_ids) <= set(all_leased)
    # 중복된 ID가 전혀 없어야 함 (각 ID는 정확히 1번만 차지됨)
    assert len(all_leased) == len(set(all_leased))

    # 서로 다른 폴러의 결과는 상호 배타적이어야 함
    for i in range(len(results)):
        for j in range(i + 1, len(results)):
            assert set(results[i]).isdisjoint(set(results[j]))


async def test_generation_switch_executes_schedule_exactly_once(tmp_path):
    """세대 A에서 등록된 예약이 세대 B로 전환된 후 새 세대 B에서 정확히 1회 실행됨을 검증."""
    a = GenerationRuntime(tmp_path, revision="a" * 40, api_url="http://127.0.0.1:18791")
    b = GenerationRuntime(tmp_path, revision="b" * 40, api_url="http://127.0.0.1:18792")
    await a.register()
    await b.register()
    try:
        await activate(a.id, expected=None)
        assert (await a.snapshot())["state"] == "active"

        # 세대 A 시절에 과거 due_at (즉시 실행 대상) 예약 등록
        async with sessionmaker()() as db:
            item = await create_schedule(db, action_type="stop_all", due_at="-5s",
                                         description="switch-test")
            sched_id = item.id

        # 세대 B 로 전환 (세대 A는 draining)
        await activate(b.id, expected=a.id)
        assert (await b.snapshot())["state"] == "active"
        assert (await a.snapshot())["state"] == "draining"

        # 세대 B 가 도래한 예약을 폴링하여 실행
        processed = await process_due_schedules(owner_id=f"gen-{b.id}")
        assert processed >= 1

        async with sessionmaker()() as db:
            finished = await get_schedule(db, sched_id)
            assert finished.status == "completed"
            assert finished.owner_id == f"gen-{b.id}"
            assert finished.attempts == 1
            assert finished.executed_at is not None

        # 세대 A 와 B 가 다시 폴링해도 이미 completed 된 작업은 실행되지 않음 (0건)
        assert await process_due_schedules(owner_id=f"gen-{a.id}") == 0
        assert await process_due_schedules(owner_id=f"gen-{b.id}") == 0

        async with sessionmaker()() as db:
            finished2 = await get_schedule(db, sched_id)
            assert finished2.attempts == 1  # 추가 실행 없음
    finally:
        await a.close()
        await b.close()


async def test_missed_schedule_recovery():
    """배포 중 서버 다운 등으로 인해 지난 시각(due_at) 예약이 새 세대 시작 시 즉시 복구 및 실행됨을 검증."""
    async with sessionmaker()() as db:
        missed = await create_schedule(
            db,
            action_type="stop_all",
            due_at=utcnow() - timedelta(minutes=30),  # 30분 전
            description="missed schedule during downtime",
        )
        missed_id = missed.id

    # 새 세대 시작 시 복구 폴링
    count = await process_due_schedules(owner_id="new-generation-startup")
    assert count >= 1

    async with sessionmaker()() as db:
        recovered = await get_schedule(db, missed_id)
        assert recovered.status == "completed"
        assert recovered.owner_id == "new-generation-startup"
        assert recovered.attempts == 1
        assert recovered.executed_at is not None


async def test_late_limit_expiration_policy():
    """late_limit_seconds 초과 시 실행하지 않고 expired 로 기록하는 정책 검증."""
    async with sessionmaker()() as db:
        sched = await create_schedule(
            db,
            action_type="stop_all",
            due_at=utcnow() - timedelta(hours=2),  # 2시간 전
            payload={"late_limit_seconds": 600},  # 10분 초과 시 만료
            description="late expired schedule",
        )
        sched_id = sched.id

    count = await process_due_schedules(owner_id="test-poller")
    assert count >= 1

    async with sessionmaker()() as db:
        item = await get_schedule(db, sched_id)
        assert item.status == "expired"
        assert "more than 600" in item.result.get("reason", "")


async def test_orchestrator_api_and_mcp_schedules(client, auth, make_dir):
    """오케스트레이터 세션 토큰으로 /api/orchestrator/schedules 및 MCP 도구 경로 검증."""
    from ttakkari.orchestrator.runtime import issue_token

    # 오케스트레이터 세션 생성
    root = make_dir()
    ws_res = await client.post("/api/workspaces", json={"name": "sched-ws", "root_path": str(root)}, headers=auth)
    ws_id = ws_res.json()["id"]

    sess_res = await client.post("/api/sessions", json={"workspace_id": ws_id, "kind": "orchestrator", "title": "orch"}, headers=auth)
    sess_id = sess_res.json()["id"]
    token = issue_token(uuid.UUID(sess_id))
    headers = {"Authorization": f"Bearer {token}"}

    # 1. 예약 등록 POST /api/orchestrator/schedules
    create_res = await client.post("/api/orchestrator/schedules", json={
        "action_type": "stop_all",
        "due_at": "+30m",
        "description": "API 예약 테스트",
    }, headers=headers)
    assert create_res.status_code == 201
    created = create_res.json()
    assert created["action_type"] == "stop_all"
    assert created["status"] == "pending"
    sched_id = created["id"]

    # 2. 예약 목록 GET /api/orchestrator/schedules
    list_res = await client.get("/api/orchestrator/schedules", headers=headers)
    assert list_res.status_code == 200
    items = list_res.json()["schedules"]
    assert any(s["id"] == sched_id for s in items)

    # 3. 예약 취소 DELETE /api/orchestrator/schedules/{id}
    del_res = await client.delete(f"/api/orchestrator/schedules/{sched_id}", headers=headers)
    assert del_res.status_code == 200
    assert del_res.json()["cancelled"] is True

    # 취소 후 상태 확인
    refetch = await client.get("/api/orchestrator/schedules", params={"status": "cancelled"}, headers=headers)
    assert any(s["id"] == sched_id for s in refetch.json()["schedules"])


async def test_quota_wait_persists_resume_after_across_restart(new_session):
    """한도 대기(run.waiting) 시 run.resume_after 가 DB 에 영속 저장되어 재기동 후에도 유지됨을 검증."""
    from ttakkari.runs import profiles
    from ttakkari.runs.manager import manager, resume_queued

    ws, sess = await new_session()
    async with sessionmaker()() as db:
        run = Run(session_id=uuid.UUID(sess["id"]), workspace_id=uuid.UUID(ws["id"]),
                  prompt="quota wait test", engine="claude", status=RunStatus.queued)
        db.add(run)
        await db.commit()
        await db.refresh(run)

        # manager._wait_for_reset 호출 시뮬레이션
        chat_sess = await db.get(ChatSession, run.session_id)
        future_until = (utcnow() + timedelta(minutes=15)).timestamp()
        accounts = await profiles.accounts(db)
        for a in accounts:
            profiles.cool_down(a.id, 900)
        prof = type("FakeProfile", (), {"name": "claude-main", "engine": "claude"})()
        await manager._wait_for_reset(db, run, chat_sess, [(prof, future_until)], handoff="test handoff")

    # DB에 resume_after 가 저장되었는지 확인
    async with sessionmaker()() as db:
        refreshed = await db.get(Run, run.id)
        assert refreshed.resume_after is not None
        assert refreshed.resume_after > utcnow()
        saved_ts = refreshed.resume_after.timestamp()

    # 프로세스 메모리 초기화 (재시작 시뮬레이션)
    manager._waiting.clear()
    manager._wakers.clear()

    # 새 세대 기동 시 resume_queued 호출
    await resume_queued()

    # resume_after 가 있으므로 manager._waiting 에 다시 re-arm 되었는지 확인
    assert run.id in manager._waiting
    assert abs(manager._waiting[run.id][0] - saved_ts) < 1.0
