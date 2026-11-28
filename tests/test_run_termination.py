"""Run 종료 원인(취소 주체·출처·의도)을 요청 시점 기록으로만 가르고, 사용자 명시 중단일 때만 목표를 멈춘다(2단계 안정화 Track C).

핵심 규칙: 엔진 결과 문자열·종료 코드·Run.error 로 사용자 의도를 추정하지 않는다. 취소를 요청한 쪽이 남긴 기록이 없으면
사용자 중단이 아니라 원인 미확인이다.
"""
from __future__ import annotations

import asyncio
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
from helpers import run_events, start_run, wait_status
from sqlalchemy import select
from test_api_runs import pid_alive, wait_agent_log

from ttakkari.db import sessionmaker
from ttakkari.models import (
    AuditLog,
    Run,
    RunEvent,
    RunOrigin,
    RunStatus,
    ScheduledAction,
    utcnow,
)
from ttakkari.runs.manager import _Handle, manager

# 운영 사례 A(Run a89f5f4c, 2026-11-27)의 이벤트 모양. 개인 정보와 경로는 지웠고 순서·종류·마지막 줄은 그대로다:
# 마지막 tool.call 이 결과 없이 길게 이어지다가 usage → error(interrupted) → run.finished(cancelled, exit_code 1, error null).
CASE_A_EVENTS = [
    ("tool.call", {"call_id": "c101", "name": "run_command", "input": {"command": "git -C /work status --short"}}),
    ("tool.result", {"call_id": "c101", "is_error": False, "output": " M src/a.ts"}),
    ("tool.call", {"call_id": "c102", "name": "run_command",
                   "input": {"command": "npm run test:browser > /tmp/dsk-b.log 2>&1 &"}}),
    ("message", {"text": "Waiting for the test chain to finish.\nI'll end this turn and wait for it."}),
    ("usage", {"cost_usd": None, "input_tokens": 1200, "output_tokens": 310}),
    ("error", {"message": "interrupted"}),
]


async def _row(run_id: UUID) -> Run:
    async with sessionmaker()() as db:
        return await db.get(Run, run_id)


async def _types(run_id: UUID) -> list[str]:
    async with sessionmaker()() as db:
        return [t for t in await db.scalars(select(RunEvent.type).where(RunEvent.run_id == run_id).order_by(RunEvent.seq))]


async def _events(run_id: UUID, type_: str) -> list[dict]:
    async with sessionmaker()() as db:
        return [e.payload for e in await db.scalars(
            select(RunEvent).where(RunEvent.run_id == run_id, RunEvent.type == type_).order_by(RunEvent.seq))]


async def _state(client, h) -> dict:
    return (await get(client, h))["state"]


async def _user_request(run_id: UUID, **over) -> None:
    """사용자가 중지 API 를 눌렀다는 요청 시점 기록(운영의 /api/runs/{id}/cancel 과 같은 기록)."""
    from ttakkari.runs.termination import CancelRequest, record_request
    req = CancelRequest(actor="user", source="api.run_cancel", reason="사용자 중지", user_intent=True, scope="run")
    async with sessionmaker()() as db:
        await record_request(db, await db.get(Run, run_id), req if not over else CancelRequest(**{**req.__dict__, **over}))
        await db.commit()


# ---- 운영 사례 A 재현 -------------------------------------------------------------------------------------
async def test_case_a_engine_interrupted_is_not_treated_as_user_stop(client, auth, make_dir):
    sess, h = await lead(client, auth, make_dir)
    sid = UUID(sess["id"])
    await put(client, h, contract())
    run = await new_run(sid, events=CASE_A_EVENTS)
    await finish(run, RunStatus.cancelled)  # 운영과 같다: error 없음, 취소 요청 이벤트 없음

    st = await _state(client, h)
    assert st["status"] == "active", st  # 수정 전에는 사용자 중단으로 paused 가 됐다
    assert "work.paused" not in await _types(run)
    term = (await _row(run)).usage["termination"]
    assert term["cause"] == "engine_interrupted" and term["user_intent"] is False
    follow = continuations(await queued(sid))
    assert len(follow) == 1 and follow[0].usage["work_continuation"].get("transient") is True


async def test_case_a_error_text_cannot_impersonate_a_user_stop(client, auth, make_dir):
    sess, h = await lead(client, auth, make_dir)
    sid = UUID(sess["id"])
    await put(client, h, contract())
    run = await new_run(sid)
    await finish(run, RunStatus.cancelled, error="사용자가 중단했습니다.")  # 문자열이 사용자 같아도 기록이 없다
    assert (await _state(client, h))["status"] != "paused"
    term = (await _row(run)).usage["termination"]
    assert term["cause"] == "unknown" and term["user_intent"] is False


# ---- 사용자 중지 ------------------------------------------------------------------------------------------
async def test_user_stop_api_records_request_pauses_goal_and_reports_process_result(client, auth, make_dir):
    sess, h = await lead(client, auth, make_dir)
    sid = UUID(sess["id"])
    await put(client, h, contract())
    run = await start_run(client, auth, sess["id"], "SLEEP")
    await wait_status(client, auth, run["id"], "running")
    pid = (await wait_agent_log())[0]["pid"]
    assert pid_alive(pid)

    r = await client.post(f"/api/runs/{run['id']}/cancel", headers=auth)
    assert r.status_code == 200 and r.json()["status"] == "cancelled", r.text
    assert not pid_alive(pid)

    events = await run_events(client, auth, run["id"])
    types = [e["type"] for e in events]
    assert types.index("run.cancel_requested") < types.index("run.finished")
    req = next(e for e in events if e["type"] == "run.cancel_requested")["payload"]
    assert (req["actor"], req["source"], req["user_intent"], req["scope"]) == ("user", "api.run_cancel", True, "run")
    assert req["run_id"] == run["id"] and req["session"] == sess["id"] and req["requested_at"] and req["reason"]
    term = next(e for e in events if e["type"] == "run.finished")["payload"]["termination"]
    assert term["cause"] == "user_stop" and term["user_intent"] is True and term["auto_recoverable"] is False
    assert term["process"]["terminated"] is True and term["process"]["exit_code"] is not None
    st = await _state(client, h)
    assert st["status"] == "paused" and st["blocker"]
    assert continuations(await queued(sid)) == []


async def test_user_stop_race_with_completion_still_pauses_and_blocks_continuation(client, auth, make_dir):
    """사용자가 중지를 눌렀지만 Run 이 그 직전에 정상 종료한 경우에도 자동 이어가기가 사용자 취소를 뒤집지 않는다."""
    sess, h = await lead(client, auth, make_dir)
    sid = UUID(sess["id"])
    await put(client, h, contract())
    run = await new_run(sid)
    await _user_request(run)
    await finish(run, RunStatus.succeeded)
    assert (await _state(client, h))["status"] == "paused"
    assert continuations(await queued(sid)) == []


async def test_user_stop_cancels_pending_auto_continuation_and_nothing_revives_the_goal(client, auth, make_dir, monkeypatch):
    from ttakkari.orchestrator import work
    async def _noop(*args, **kwargs):
        pass
    monkeypatch.setattr(manager, "dispatch", _noop)
    sess, h = await lead(client, auth, make_dir)
    sid = UUID(sess["id"])
    await put(client, h, contract())
    await finish(await new_run(sid), RunStatus.succeeded)  # 자동 후속 하나가 대기
    pending = continuations(await queued(sid))
    assert len(pending) == 1
    async with sessionmaker()() as db:
        schedule = ScheduledAction(action_type="send_message", status="pending", due_at=utcnow() + timedelta(hours=1),
                                   payload={"session_id": str(sid), "created_by_session": str(sid)})
        db.add(schedule)
        await db.commit()
        schedule_id = schedule.id

    stopped = await new_run(sid)
    await _user_request(stopped)
    await finish(stopped, RunStatus.cancelled)  # 취소 완료와 자동 재개 예약이 같은 시점에 있다
    assert (await _state(client, h))["status"] == "paused"
    assert continuations(await queued(sid)) == []
    cancelled = await _row(pending[0].id)
    assert cancelled.status == RunStatus.cancelled
    assert cancelled.usage["termination"]["initiator"]["actor"] == "system"  # 사용자 중지가 아니라 시스템이 거둔 후속

    # 늦게 끝난 다른 Run, 예약 유실 복구, 이어가기 재시도가 중단된 목표를 되살리지 못한다.
    await finish(await new_run(sid), RunStatus.succeeded)
    async with sessionmaker()() as db:
        item = await db.get(ScheduledAction, schedule_id)
        item.status = "failed"
        assert await work.resume_after_schedule_loss(db, item) is None
        await db.commit()
    assert (await _state(client, h))["status"] == "paused"
    assert continuations(await queued(sid)) == []


# ---- 기획자의 작업자 중지 / 전역 중지 ---------------------------------------------------------------------
async def test_planner_worker_stop_is_recorded_and_does_not_stop_the_goal(client, auth, make_dir):
    sess, h = await lead(client, auth, make_dir)
    sid = UUID(sess["id"])
    await put(client, h, contract())
    worker = await make_worker(sid)
    run = await new_run(sid, status=RunStatus.queued, origin=RunOrigin.orchestrator, session_id=worker)

    r = await client.post(f"/api/orchestrator/workers/{worker}/stop", headers=h)
    assert r.status_code == 200, r.text
    row = await _row(run)
    assert row.status == RunStatus.cancelled
    term = row.usage["termination"]
    assert term["cause"] == "planner_stop" and term["user_intent"] is False
    assert term["initiator"] == {"actor": "orchestrator", "actor_id": str(sid)}
    assert term["source"] == "api.orchestrator_stop_worker" and term["target"] == {"run_id": str(run), "session": str(worker)}
    assert (await _state(client, h))["status"] == "active"

    # 같은 기록이 기획 세션의 Run 에 붙어도(잘못된 범위) 작업자 중지는 목표를 멈추지 않는다.
    mine = await new_run(sid)
    from ttakkari.runs.termination import CancelRequest, record_request
    async with sessionmaker()() as db:
        await record_request(db, await db.get(Run, mine), CancelRequest(
            actor="orchestrator", actor_id=str(sid), source="api.orchestrator_stop_worker", reason="작업자 중지",
            user_intent=False, scope="worker"))
        await db.commit()
    await finish(mine, RunStatus.cancelled)
    assert (await _state(client, h))["status"] == "active"
    assert "work.paused" not in await _types(mine)


@pytest.mark.parametrize("via,source,user_intent", [("system", "api.system_stop_all", True),
                                                    ("orchestrator", "api.orchestrator_stop_all", False)])
async def test_global_stop_pauses_goal_and_records_who_asked(client, auth, make_dir, via, source, user_intent):
    sess, h = await lead(client, auth, make_dir)
    sid = UUID(sess["id"])
    await put(client, h, contract())
    run = await new_run(sid, status=RunStatus.queued)
    r = await (client.post("/api/system/stop-all", headers=auth) if via == "system"
               else client.post("/api/orchestrator/stop-all", headers=h))
    assert r.status_code == 200, r.text
    term = (await _row(run)).usage["termination"]
    assert term["cause"] == "global_stop" and term["source"] == source and term["user_intent"] is user_intent
    assert (await _state(client, h))["status"] == "paused"
    assert continuations(await queued(sid)) == []


# ---- 원인 미확인 ------------------------------------------------------------------------------------------
async def test_unknown_cancel_gets_one_limited_recovery_then_goes_to_review(client, auth, make_dir):
    sess, h = await lead(client, auth, make_dir)
    sid = UUID(sess["id"])
    await put(client, h, contract())
    first = await new_run(sid)
    await finish(first, RunStatus.cancelled)
    assert (await _state(client, h))["status"] == "active"
    follow = continuations(await queued(sid))
    assert len(follow) == 1  # 제한적 복구 1회
    async with sessionmaker()() as db:  # 다음 판정이 소유자 없이 시작하게 대기 후속을 비운다
        (await db.get(Run, follow[0].id)).status = RunStatus.cancelled
        await db.commit()
    await finish(await new_run(sid), RunStatus.cancelled)
    st = await _state(client, h)
    assert st["status"] == "blocked" and "원인 미확인" in st["blocker"]
    assert (await raw(sid))["blocker_kind"] == "cancel_cause_unknown"
    assert continuations(await queued(sid)) == [] or all(r.id == follow[0].id for r in continuations(await queued(sid)))


async def test_unknown_cancel_with_an_existing_owner_does_not_start_a_duplicate(client, auth, make_dir):
    sess, h = await lead(client, auth, make_dir)
    sid = UUID(sess["id"])
    await put(client, h, contract())
    other = await new_run(sid)  # 이 세션에서 이미 실행 중인 소유자
    await finish(await new_run(sid), RunStatus.cancelled)
    assert (await _state(client, h))["status"] == "active"
    assert continuations(await queued(sid)) == []
    assert (await _row(other)).status == RunStatus.running


async def test_cancel_flag_without_any_request_record_goes_to_review_not_pause(client, auth, make_dir):
    """cancel() 이 불렸지만 요청 기록이 없다(구세대 API 등). 사용자 중단으로 단정하지도, 자동으로 이어가지도 않는다."""
    sess, h = await lead(client, auth, make_dir)
    sid = UUID(sess["id"])
    await put(client, h, contract())
    run = await new_run(sid)
    manager._handles[run] = _Handle(task=asyncio.get_running_loop().create_future(), cancel_requested=True)
    try:
        await finish(run, RunStatus.cancelled)
    finally:
        manager._handles.pop(run, None)
    st = await _state(client, h)
    assert st["status"] == "blocked" and "주체 미기록" in st["blocker"]
    assert (await raw(sid))["blocker_kind"] == "cancel_unattributed"
    assert (await _row(run)).usage["termination"]["cause"] == "cancel_unattributed"
    assert continuations(await queued(sid)) == []


# ---- 서버 재시작·세대 전환·제한시간·계정 한도 ---------------------------------------------------------------
async def test_server_restart_interruption_is_not_a_user_stop(client, auth, make_dir):
    sess, h = await lead(client, auth, make_dir)
    sid = UUID(sess["id"])
    await put(client, h, contract())
    run = await new_run(sid)
    await finish(run, RunStatus.interrupted, error="서버 종료로 중단")
    assert (await _state(client, h))["status"] == "active"
    term = (await _row(run)).usage["termination"]
    assert term["cause"] == "server_shutdown" and term["user_intent"] is False and term["auto_recoverable"] is True
    assert continuations(await queued(sid)) == []  # 재개는 기존 resume_interrupted 가 맡는다(중복 Run 없음)


async def test_generation_drain_and_timeout_causes_are_recorded_without_stopping_the_goal(client, auth, make_dir):
    sess, h = await lead(client, auth, make_dir)
    sid = UUID(sess["id"])
    await put(client, h, contract())
    drained = await new_run(sid)
    async with sessionmaker()() as db:
        await manager._finish(db, await db.get(Run, drained), RunStatus.interrupted, error="서버 종료로 중단",
                              facts={"shutting_down": True, "draining": True})
    assert (await _row(drained)).usage["termination"]["cause"] == "generation_drain"

    slow = await new_run(sid)
    async with sessionmaker()() as db:
        await manager._finish(db, await db.get(Run, slow), RunStatus.failed, error="시간 초과(7200초)",
                              facts={"timed_out": True})
    term = (await _row(slow)).usage["termination"]
    assert term["cause"] == "timeout" and term["user_intent"] is False and term["auto_recoverable"] is True
    assert (await _state(client, h))["status"] != "paused"


async def test_account_limit_keeps_progress_and_waits_instead_of_stopping(client, auth, make_dir):
    sess, h = await lead(client, auth, make_dir)
    sid = UUID(sess["id"])
    await put(client, h, contract())
    run = await new_run(sid)
    await finish(run, RunStatus.failed, error="You've hit your usage limit. Try again in 3 hours.")
    term = (await _row(run)).usage["termination"]
    assert term["cause"] == "usage_limit" and term["user_intent"] is False and term["auto_recoverable"] is True
    assert (await _state(client, h))["status"] == "active"
    follow = continuations(await queued(sid))
    assert len(follow) == 1 and follow[0].usage["work_continuation"]["failure_kind"] == "usage_limit"


# ---- 분류 규칙(순수 함수) ----------------------------------------------------------------------------------
def _req(actor="user", scope="run", user_intent=True, source="api.run_cancel"):
    return {"actor": actor, "actor_id": None, "source": source, "scope": scope, "user_intent": user_intent,
            "requested_at": "2026-11-28T00:00:00+00:00", "reason": "r"}


@pytest.mark.parametrize("status,error,requests,facts,cause,intent", [
    ("cancelled", None, [_req()], {}, "user_stop", True),
    ("cancelled", None, [_req()], {"engine_error": "interrupted", "cancel_flag": True}, "user_stop", True),
    ("cancelled", None, [_req("orchestrator", "worker", False, "api.orchestrator_stop_worker")], {}, "planner_stop", False),
    ("cancelled", None, [_req("orchestrator", "global", False, "api.orchestrator_stop_all")], {}, "global_stop", False),
    ("cancelled", None, [_req("user", "global", True, "api.system_stop_all")], {}, "global_stop", True),
    ("cancelled", None, [_req("schedule", "run", False, "schedule.cancel_run")], {}, "scheduled_stop", False),
    ("cancelled", None, [], {"cancel_flag": True}, "cancel_unattributed", False),
    ("cancelled", None, [], {"cancel_command": True, "engine_error": "interrupted"}, "cancel_unattributed", False),
    ("cancelled", None, [], {"engine_error": "interrupted"}, "engine_interrupted", False),
    ("cancelled", "사용자가 중단했습니다.", [], {}, "unknown", False),
    ("cancelled", None, [], {}, "unknown", False),
    ("interrupted", "서버 종료로 중단", [], {"shutting_down": True}, "server_shutdown", False),
    ("interrupted", "서버 종료로 중단", [], {"shutting_down": True, "draining": True}, "generation_drain", False),
    ("failed", "시간 초과(7200초)", [], {"timed_out": True}, "timeout", False),
    ("failed", "usage limit reached, try again in 2 hours", [], {}, "usage_limit", False),
    ("failed", "exit code -9", [], {"exit_code": -9}, "process_signal", False),
    ("failed", "connection reset by peer", [], {"exit_code": 1}, "engine_failure", False),
    ("failed", "boom", [], {"exit_code": 1}, "permanent_failure", False),
])
def test_classify_uses_request_records_only(status, error, requests, facts, cause, intent):
    from ttakkari.runs.termination import classify
    got = classify(status, error=error, requests=requests, facts=facts)
    assert got["cause"] == cause and got["user_intent"] is intent, got


def test_policy_pauses_only_for_explicit_stops():
    from ttakkari.runs.termination import goal_action
    table = {"user_stop": "pause", "global_stop": "pause", "planner_stop": "isolate", "scheduled_stop": "review",
             "cancel_unattributed": "review", "engine_interrupted": "recover", "unknown": "recover"}
    assert {k: goal_action({"cause": k}) for k in table} == table


# ---- 종료 원인 미확인 비율(§12 관측) -----------------------------------------------------------------------
async def test_unknown_cause_ratio_can_be_counted_from_existing_run_records(client, auth, make_dir):
    from ttakkari.runs.termination import cancellation_stats
    sess, h = await lead(client, auth, make_dir)
    sid = UUID(sess["id"])
    await put(client, h, contract())
    stored = await new_run(sid)
    await _user_request(stored)
    await finish(stored, RunStatus.cancelled)  # 새 기록: user_stop
    legacy_interrupted = await new_run(sid, status=RunStatus.cancelled, events=CASE_A_EVENTS)  # 옛 기록: usage 에 원인 없음
    legacy_blank = await new_run(sid, status=RunStatus.cancelled)
    legacy_user = await new_run(sid, status=RunStatus.cancelled)
    async with sessionmaker()() as db:  # 옛 API 는 요청 뒤 감사 로그 run.cancel 만 남겼다
        db.add(AuditLog(actor="user", action="run.cancel", outcome="ok", target_type="run", target_id=str(legacy_user), detail={}))
        await db.commit()
    async with sessionmaker()() as db:
        stats = await cancellation_stats(db)
    assert stats["cancelled"] == 4
    assert stats["by_cause"] == {"user_stop": 2, "engine_interrupted": 1, "unknown": 1}
    assert stats["unknown"] == 1 and stats["unknown_ratio"] == 0.25
    assert stats["unattributed_runs"] == [str(legacy_blank)] and legacy_interrupted and legacy_user


def test_cancel_request_audit_rows_never_carry_secrets_or_free_text_beyond_the_reason():
    from ttakkari.runs.termination import CancelRequest
    detail = CancelRequest(actor="user", source="api.run_cancel", reason="x" * 5000, user_intent=True).detail(
        run_id=str(uuid.uuid4()), session_id=str(uuid.uuid4()))
    assert len(detail["reason"]) <= 1000 and set(detail) >= {
        "run_id", "session", "actor", "actor_id", "source", "scope", "reason", "user_intent", "requested_at"}


# ---- 반례: 시스템이 자기 후속을 거두는 취소 ------------------------------------------------------------------
async def test_system_superseding_a_leased_waiting_continuation_does_not_block_the_active_goal(client, auth, make_dir):
    """사용자 새 지시가 한도 대기 후속을 거두는데 그 후속이 이미 세대 lease 를 잡았다. 소유 세대가 cancel 명령으로 마감한다.
    이 취소는 시스템이 낸 것이므로 요청 시점에 주체가 남아야 하고, 그렇지 않으면 '주체 미기록'으로 활성 목표가 blocked 가 된다."""
    from ttakkari.models import RuntimeCommand, RuntimeGeneration, RuntimeLease
    from ttakkari.orchestrator import work
    sess, h = await lead(client, auth, make_dir)
    sid = UUID(sess["id"])
    await put(client, h, contract())
    owner_id = uuid.uuid4()
    async with sessionmaker()() as db:
        db.add(RuntimeGeneration(id=owner_id, revision="a" * 40, api_url="http://127.0.0.1:18793",
                                 pid=987654321, process_started_at=1.0, state="active"))
        await db.flush()
        wait = Run(session_id=sid, workspace_id=(await db.get(Run, await new_run(sid))).workspace_id, engine="claude",
                   prompt="wait", origin=RunOrigin.orchestrator, status=RunStatus.queued,
                   usage={"work_continuation": {"source_run_id": None, "revision": 1, "transient": True}},
                   resume_after=utcnow() + timedelta(hours=1))
        db.add(wait)
        await db.flush()
        db.add(RuntimeLease(run_id=wait.id, session_id=sid, generation_id=owner_id, token=uuid.uuid4(), lane="leads"))
        await db.commit()
        wait_id = wait.id

    async with sessionmaker()() as db:
        await work.release_waiting_continuations(db, sid, "new user request supersedes waiting goal continuation")
        await db.commit()
    async with sessionmaker()() as db:
        assert await db.scalar(select(RuntimeCommand.id).where(RuntimeCommand.run_id == wait_id, RuntimeCommand.kind == "cancel"))

    await finish(wait_id, RunStatus.cancelled)  # 소유 세대가 cancel 명령을 집행해 마감한다

    term = (await _row(wait_id)).usage["termination"]
    assert term["cause"] == "system_stop" and term["user_intent"] is False, term
    assert term["initiator"]["actor"] == "system"
    st = await _state(client, h)
    assert st["status"] == "active", st  # 수정 전에는 cancel_unattributed → blocked(검토 대상)
