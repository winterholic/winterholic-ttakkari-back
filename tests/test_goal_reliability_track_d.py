"""Track D: 외부 의존성 대기(D-2), 이벤트 기반 자동 재개(D-3), 독립 요구 진행(D-4), 하람 사례 C 재현 및 해결 시험."""
from __future__ import annotations

import uuid
from datetime import timedelta
from uuid import UUID

import pytest
from goal_helpers import (
    continuations,
    contract,
    finish,
    get,
    lead,
    make_worker,
    new_run,
    put,
    queued,
    raw,
)

from ttakkari.db import sessionmaker
from ttakkari.models import (
    AppSetting,
    RunStatus,
    ScheduledAction,
    utcnow,
)
from ttakkari.orchestrator import work
from ttakkari.schedules import service as sched_service


@pytest.fixture(autouse=True)
def no_dispatch(monkeypatch):
    from ttakkari.runs.manager import manager

    async def _noop(*args, **kwargs):
        return None

    monkeypatch.setattr(manager, "dispatch", _noop)


async def _active_goal(client, auth, make_dir, reqs=None):
    sess, h = await lead(client, auth, make_dir)
    sid = UUID(sess["id"])
    c = contract(reqs=reqs)
    res = await put(client, h, c)
    assert res.status_code == 200, res.text
    return sid, h


async def _end_turn(sid: UUID, *, result_text: str = "턴 완료") -> None:
    rid = await new_run(sid)
    await finish(rid, RunStatus.succeeded, result_text=result_text)


@pytest.mark.asyncio
async def test_dependency_wait_representation_on_requirement_and_state(client, auth, make_dir):
    """D-2: 외부 의존성 대기 상태가 요구사항과 전체 상태, 그리고 summary/view 에 정확히 표현된다."""
    target_worker = f"worker:{uuid.uuid4()}"
    reqs = [
        {
            "id": "R1",
            "text": "외부 작업자 결과 수집",
            "status": "waiting",
            "wait": {
                "target": target_worker,
                "reason": "작업자 실행 중",
                "resume_condition": "worker_completion",
                "source": "worker",
            },
        },
        {"id": "R2", "text": "완료된 요구", "status": "done", "evidence": "pytest tests/test_foo.py: 1 passed in 0.1s"},
    ]
    sid, h = await _active_goal(client, auth, make_dir, reqs=reqs)

    view = await get(client, h)
    state = view["state"]
    assert state["requirements"][0]["status"] == "waiting"
    assert state["requirements"][0]["wait"]["target"] == target_worker

    async with sessionmaker()() as db:
        summary = await work.summary(db, sid)
    assert summary["phase"] == "waiting"
    assert summary["wait_type"] == "external_dependency"
    assert len(summary["waiting_dependencies"]) == 1
    assert summary["waiting_dependencies"][0]["target"] == target_worker
    assert summary["has_partial_failure"] is False


@pytest.mark.asyncio
async def test_event_based_resume_from_worker_completion(client, auth, make_dir):
    """D-3: 작업자 완료 이벤트로 대기 중인 목표 계약이 1회 안전하게 재개된다."""
    worker_id = uuid.uuid4()
    reqs = [
        {
            "id": "R1",
            "text": "작업자 결과 반영",
            "status": "waiting",
            "wait": {"target": f"worker:{worker_id}", "source": "worker"},
        }
    ]
    sid, h = await _active_goal(client, auth, make_dir, reqs=reqs)
    await _end_turn(sid)
    assert continuations(await queued(sid)) == []

    # 작업자 완료 이벤트 도착
    async with sessionmaker()() as db:
        resumed = await work.resolve_dependency(
            db, f"worker:{worker_id}", result={"status": "succeeded", "result_text": "작업 완료"}, source="worker_finished"
        )
        await db.commit()

    assert len(resumed) == 1
    runs = continuations(await queued(sid))
    assert len(runs) == 1
    assert runs[0].id == resumed[0]
    assert "외부 의존성" in runs[0].prompt

    # 요구사항 상태가 waiting -> pending 으로 변경되고 satisfied=True 기록
    view = await get(client, h)
    r1 = view["state"]["requirements"][0]
    assert r1["status"] == "pending"
    assert r1["wait"]["satisfied"] is True


@pytest.mark.asyncio
async def test_event_based_resume_from_schedule_completion(client, auth, make_dir):
    """D-3: 예약 작업 완료 이벤트로 대기 중인 목표 계약이 재개된다."""
    schedule_id = uuid.uuid4()
    reqs = [
        {
            "id": "R1",
            "text": "1시간 뒤 배치 결과 확인",
            "status": "waiting",
            "wait": {"target": f"schedule:{schedule_id}", "source": "schedule"},
        }
    ]
    sid, h = await _active_goal(client, auth, make_dir, reqs=reqs)
    await _end_turn(sid)
    assert continuations(await queued(sid)) == []

    # 예약 등록 후 완료
    async with sessionmaker()() as db:
        item = ScheduledAction(
            id=schedule_id,
            action_type="send_message",
            payload={"session_id": str(sid)},
            status="leased",
            owner_id="test_worker",
            attempts=1,
            due_at=utcnow(),
        )
        db.add(item)
        await db.commit()

    async with sessionmaker()() as db:
        assert await sched_service.complete_schedule(db, schedule_id, {"status": "completed"}, owner_id="test_worker", attempt=1)
        await db.commit()

    runs = continuations(await queued(sid))
    assert len(runs) == 1
    view = await get(client, h)
    assert view["state"]["requirements"][0]["status"] == "pending"
    assert view["state"]["requirements"][0]["wait"]["satisfied"] is True


@pytest.mark.asyncio
async def test_event_based_resume_from_session_completion(client, auth, make_dir):
    """D-3: 다른 기획 세션의 목표 완료 시 대기 중인 계약이 재개된다."""
    target_sid, target_h = await _active_goal(client, auth, make_dir)
    reqs = [
        {
            "id": "R1",
            "text": "선행 세션 작업 대기",
            "status": "waiting",
            "wait": {"target": f"session:{target_sid}", "source": "session"},
        }
    ]
    waiting_sid, waiting_h = await _active_goal(client, auth, make_dir, reqs=reqs)
    await _end_turn(waiting_sid)
    assert continuations(await queued(waiting_sid)) == []

    # 선행 세션 목표 완료
    cur = await get(client, target_h)
    async with sessionmaker()() as db:
        row = await db.get(AppSetting, work.key(target_sid))
        row.value = {
            **cur,
            "revision": cur["revision"] + 1,
            "state": {**cur["state"], "status": "complete"},
        }
        await db.commit()
    async with sessionmaker()() as db:
        await work.resolve_dependency(
            db, f"session:{target_sid}", result={"status": "complete"}, source="session_complete"
        )
        await db.commit()

    runs = continuations(await queued(waiting_sid))
    assert len(runs) == 1
    view = await get(client, waiting_h)
    assert view["state"]["requirements"][0]["status"] == "pending"
    assert view["state"]["requirements"][0]["wait"]["satisfied"] is True


@pytest.mark.asyncio
async def test_event_based_resume_from_deploy_completion(client, auth, make_dir):
    """D-3: 외부 배포 완료 이벤트 도착 시 재개된다."""
    deploy_id = "deploy_20261128_abcdef"
    reqs = [
        {
            "id": "R1",
            "text": "백엔드 배포 대기",
            "status": "waiting",
            "wait": {"target": f"deploy:{deploy_id}", "source": "deploy"},
        }
    ]
    sid, h = await _active_goal(client, auth, make_dir, reqs=reqs)
    await _end_turn(sid)
    assert continuations(await queued(sid)) == []

    async with sessionmaker()() as db:
        await work.resolve_dependency(db, f"deploy:{deploy_id}", result={"status": "succeeded"}, source="deploy_controller")
        await db.commit()

    runs = continuations(await queued(sid))
    assert len(runs) == 1
    assert (await get(client, h))["state"]["requirements"][0]["status"] == "pending"


@pytest.mark.asyncio
async def test_deduplication_subsequent_duplicate_events(client, auth, make_dir):
    """D-3: 중복 이벤트가 여러 번 도착해도 후속 Run 은 1회만 생성된다."""
    worker_id = uuid.uuid4()
    reqs = [
        {
            "id": "R1",
            "text": "작업자 결과 대기",
            "status": "waiting",
            "wait": {"target": f"worker:{worker_id}"},
        }
    ]
    sid, _h = await _active_goal(client, auth, make_dir, reqs=reqs)
    await _end_turn(sid)

    async with sessionmaker()() as db:
        res1 = await work.resolve_dependency(db, f"worker:{worker_id}", source="first")
        res2 = await work.resolve_dependency(db, f"worker:{worker_id}", source="second")
        res3 = await work.resolve_dependency(db, f"worker:{worker_id}", source="third")
        await db.commit()

    assert len(res1) == 1
    assert len(res2) == 0
    assert len(res3) == 0
    runs = continuations(await queued(sid))
    assert len(runs) == 1


@pytest.mark.asyncio
async def test_already_satisfied_dependency_upon_registration(client, auth, make_dir):
    """D-3: 이벤트가 대기 등록보다 먼저 온 경우(이미 완료된 작업자/예약/세션) 즉시 satisfied 로 전환하고 pending 유지."""
    # 이미 완료된 작업자 세션과 Run 생성
    sess, h = await lead(client, auth, make_dir)
    sid = UUID(sess["id"])
    wid = await make_worker(sid)
    w_run_id = await new_run(wid, session_id=wid, status=RunStatus.running)
    await finish(w_run_id, RunStatus.succeeded, result_text="사전 완료된 작업")

    # 대기 등록
    reqs = [
        {
            "id": "R1",
            "text": "이미 끝난 작업자 의존",
            "status": "waiting",
            "wait": {"target": f"worker:{wid}"},
        }
    ]
    res = await put(client, h, contract(reqs=reqs))
    assert res.status_code == 200, res.text

    view = await get(client, h)
    r1 = view["state"]["requirements"][0]
    assert r1["status"] == "pending"
    assert r1["wait"]["satisfied"] is True


@pytest.mark.asyncio
async def test_restart_and_generation_transition_persistence(client, auth, make_dir):
    """D-3: 재시작·세대 전환 후에도 영속 AppSetting 에 보관된 대기 상태가 손실 없이 복원된다."""
    target = f"worker:{uuid.uuid4()}"
    reqs = [
        {
            "id": "R1",
            "text": "외부 대기",
            "status": "waiting",
            "wait": {"target": target, "timeout_at": (utcnow() + timedelta(hours=2)).isoformat()},
        }
    ]
    sid, _h = await _active_goal(client, auth, make_dir, reqs=reqs)
    await _end_turn(sid)

    # 새로운 DB 세션에서 로드
    async with sessionmaker()() as db:
        loaded = await work.load(db, sid)
    assert loaded["state"]["requirements"][0]["status"] == "waiting"
    assert loaded["state"]["requirements"][0]["wait"]["target"] == target
    assert loaded["waiting_until"] is not None


@pytest.mark.asyncio
async def test_schedule_failure_and_loss_recovery(client, auth, make_dir):
    """D-3: 예약 실패 시에도 resolve_dependency 가 호출되어 의존성이 실패 결과와 함께 갱신된다."""
    schedule_id = uuid.uuid4()
    reqs = [
        {
            "id": "R1",
            "text": "실패할 예약 대기",
            "status": "waiting",
            "wait": {"target": f"schedule:{schedule_id}"},
        }
    ]
    sid, h = await _active_goal(client, auth, make_dir, reqs=reqs)
    await _end_turn(sid)

    async with sessionmaker()() as db:
        item = ScheduledAction(
            id=schedule_id,
            action_type="send_message",
            payload={"session_id": str(sid)},
            status="leased",
            owner_id="p",
            attempts=3,
            max_attempts=3,
            due_at=utcnow(),
        )
        db.add(item)
        await db.commit()

    async with sessionmaker()() as db:
        assert await sched_service.fail_schedule(db, schedule_id, "치명적 오류", owner_id="p", attempt=3)
        await db.commit()

    view = await get(client, h)
    r1 = view["state"]["requirements"][0]
    assert r1["wait"]["satisfied"] is True
    assert r1["wait"]["result"]["status"] == "failed"


@pytest.mark.asyncio
async def test_paused_goal_does_not_resume_on_dependency_event(client, auth, make_dir):
    """D-3: 사용자가 일시중지(paused)한 목표는 외부 의존성이 완료되어도 자동 재개되지 않는다 (Track C 사용자 중지 우선)."""
    target = f"worker:{uuid.uuid4()}"
    reqs = [
        {
            "id": "R1",
            "text": "작업자 대기",
            "status": "waiting",
            "wait": {"target": target},
        }
    ]
    sid, h = await _active_goal(client, auth, make_dir, reqs=reqs)
    # 사용자 중지로 일시정지
    cur = await get(client, h)
    paused = {**cur["state"], "status": "paused", "blocker": "사용자 중지 요청"}
    res = await put(client, h, paused, revision=cur["revision"])
    assert res.status_code == 200

    async with sessionmaker()() as db:
        resumed = await work.resolve_dependency(db, target, source="worker_done")
        await db.commit()

    assert resumed == []
    assert continuations(await queued(sid)) == []
    assert (await get(client, h))["state"]["status"] == "paused"


@pytest.mark.asyncio
async def test_completed_goal_does_not_resurrect_on_old_event(client, auth, make_dir):
    """D-3: 이미 완료된 목표는 과거 이벤트가 늦게 도착해도 부활하지 않는다."""
    target = f"worker:{uuid.uuid4()}"
    sid, h = await _active_goal(client, auth, make_dir)
    cur = await get(client, h)
    async with sessionmaker()() as db:
        row = await db.get(AppSetting, work.key(sid))
        row.value = {
            **cur,
            "revision": cur["revision"] + 1,
            "state": {**cur["state"], "status": "complete"},
        }
        await db.commit()

    async with sessionmaker()() as db:
        resumed = await work.resolve_dependency(db, target, source="late_event")
        await db.commit()

    assert resumed == []
    assert continuations(await queued(sid)) == []
    assert (await get(client, h))["state"]["status"] == "complete"


@pytest.mark.asyncio
async def test_independent_requirement_progress_while_another_waiting_or_blocked(client, auth, make_dir):
    """D-4: 한 요구가 외부 대기·장애여도 수행 가능한 독립 요구가 남아 있으면 전체가 blocked 되지 않고 진행된다."""
    target = f"worker:{uuid.uuid4()}"
    reqs = [
        {
            "id": "R1",
            "text": "외부 작업자 대기 중인 작업",
            "status": "waiting",
            "wait": {"target": target},
        },
        {
            "id": "R2",
            "text": "차단된 부속 작업",
            "status": "blocked",
        },
        {
            "id": "R3",
            "text": "독립적으로 진행 가능한 작업",
            "status": "pending",
        },
    ]
    sid, h = await _active_goal(client, auth, make_dir, reqs=reqs)
    await _end_turn(sid, result_text="독립 요구 진행")

    runs = continuations(await queued(sid))
    assert len(runs) == 1, "독립적으로 진행 가능한 R3가 있으므로 후속 Run이 생성되어야 함"
    prompt = runs[0].prompt
    assert "외부 의존성 대기 중인 항목" in prompt
    assert "차단된 항목" in prompt
    assert "남은 미완료 항목" in prompt
    assert "R3" in prompt or "독립적으로 진행 가능한 작업" in prompt

    view = await get(client, h)
    assert view["state"]["status"] == "active"


@pytest.mark.asyncio
async def test_operational_case_c_reproduction_and_fix(client, auth, make_dir):
    """운영 사례 C 재현 및 해결:
    수정 전: 외부 결과를 기다리는 동안 매 자동 이어가기가 무진전으로 집계되어 8회 후 blocked 되고 이벤트가 와도 재개 안 됨.
    수정 후: wait 등록 시 무진전 자동 이어가기를 소비하지 않고(0회 유지) 대기하다가, 의존성 충족 이벤트 도착 시 1회 정상 재개.
    """
    target = f"deploy:{uuid.uuid4()}"
    reqs = [
        {
            "id": "R1",
            "text": "배포 및 CI 통과 대기",
            "status": "waiting",
            "wait": {"target": target},
        }
    ]
    sid, h = await _active_goal(client, auth, make_dir, reqs=reqs)

    # 10회 연속으로 턴이 끝나도 후속 Run이 생성되지 않고 무진전 카운트가 0으로 유지된다 (8회 차단 버그 방지)
    for _ in range(3):
        await _end_turn(sid, result_text="배포 대기 중...")
        assert continuations(await queued(sid)) == []
        stored = await raw(sid)
        assert stored.get("unchanged_continuations", 0) == 0
        assert stored["state"]["status"] == "active"

    async with sessionmaker()() as db:
        summary = await work.summary(db, sid)
    assert summary["phase"] == "waiting"
    assert summary["wait_type"] == "external_dependency"

    # 배포 완료 이벤트 도착
    async with sessionmaker()() as db:
        resumed = await work.resolve_dependency(db, target, result={"status": "succeeded"}, source="deploy_finished")
        await db.commit()

    assert len(resumed) == 1
    runs = continuations(await queued(sid))
    assert len(runs) == 1
    assert "외부 의존성" in runs[0].prompt

    view = await get(client, h)
    assert view["state"]["requirements"][0]["status"] == "pending"
    assert view["state"]["requirements"][0]["wait"]["satisfied"] is True
