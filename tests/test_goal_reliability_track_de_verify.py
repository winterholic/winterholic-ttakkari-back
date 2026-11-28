"""Track D+E 독립 검증에서 찾은 결함의 재현 시험. 수정 전 코드에서는 모두 실패해야 한다."""
from __future__ import annotations

import copy
from datetime import datetime, timedelta
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
from sqlalchemy import select

from ttakkari.db import sessionmaker
from ttakkari.models import (
    AppSetting,
    Run,
    RunStatus,
    ScheduledAction,
    utcnow,
)
from ttakkari.orchestrator import goal_signals, work
from ttakkari.schedules import service as sched_service


@pytest.fixture(autouse=True)
def no_dispatch(monkeypatch):
    from ttakkari.runs.manager import manager

    async def _noop(*args, **kwargs):
        return None

    monkeypatch.setattr(manager, "dispatch", _noop)


def _wait(target: str, **extra) -> dict:
    return {"target": target, **extra}


async def _goal(client, auth, make_dir, reqs):
    sess, h = await lead(client, auth, make_dir)
    res = await put(client, h, contract(reqs=reqs))
    assert res.status_code == 200, res.text
    return UUID(sess["id"]), h


async def _end_turn(sid: UUID) -> None:
    await finish(await new_run(sid), RunStatus.succeeded, result_text="턴 완료")


async def _watchdogs(sid: UUID) -> list[ScheduledAction]:
    async with sessionmaker()() as db:
        rows = (await db.scalars(select(ScheduledAction).where(ScheduledAction.action_type == "send_message"))).all()
    return [r for r in rows if (r.payload or {}).get(work.WAIT_EXPIRY_MARK) and r.payload.get("session_id") == str(sid)]


# ---- D-2: 대기 표현 검증 -------------------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_waiting_requirement_must_name_what_it_waits_for(client, auth, make_dir):
    """대상 없이 status=waiting 만 선언하면 영원히 깨어날 수 없는 대기가 된다. 등록 자체를 거절한다."""
    _sess, h = await lead(client, auth, make_dir)
    res = await put(client, h, contract(reqs=[{"id": "R1", "text": "기다림", "status": "waiting"}]))
    assert res.status_code == 422, res.text


@pytest.mark.asyncio
async def test_wait_time_must_be_iso(client, auth, make_dir):
    """timeout_at 이 아무 문자열이면 summary 의 대기 시각 파싱이 깨진다(500). 형식을 등록 때 거른다."""
    _sess, h = await lead(client, auth, make_dir)
    res = await put(client, h, contract(reqs=[
        {"id": "R1", "text": "기다림", "status": "waiting", "wait": _wait("worker:x", timeout_at="내일 오전")}]))
    assert res.status_code == 422, res.text


# ---- D-3: 만료 정책 -------------------------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_wait_without_timeout_gets_default_expiry_and_redeclare_cannot_extend(client, auth, make_dir):
    _sid, h = await _goal(client, auth, make_dir, [{"id": "R1", "text": "대기", "status": "waiting", "wait": _wait("worker:abc")}])
    cur = await get(client, h)
    stamped = cur["state"]["requirements"][0]["wait"]["timeout_at"]
    assert stamped, "만료 시각 없는 대기는 서버가 상한을 붙여야 한다"
    assert utcnow() + timedelta(hours=23) < datetime.fromisoformat(stamped) < utcnow() + timedelta(hours=25)
    # 같은 대상을 시각 없이 다시 선언해도 이전 만료 시각이 유지된다
    again = {**cur["state"], "requirements": [{"id": "R1", "text": "대기", "status": "waiting", "wait": _wait("worker:abc")}]}
    assert (await put(client, h, again, revision=cur["revision"])).status_code == 200
    assert (await get(client, h))["state"]["requirements"][0]["wait"]["timeout_at"] == stamped


@pytest.mark.asyncio
async def test_overdue_wait_blocks_goal_and_tells_user_instead_of_waiting_forever(client, auth, make_dir):
    past = (utcnow() - timedelta(minutes=5)).isoformat()
    sid, _h = await _goal(client, auth, make_dir, [
        {"id": "R1", "text": "승인 대기", "status": "waiting", "wait": _wait("approval:lead", timeout_at=past)}])
    await _end_turn(sid)
    stored = await raw(sid)
    assert stored["state"]["status"] == "blocked", "만료된 대기는 blocked 로 멈춰 사용자에게 보여야 한다"
    assert "만료" in stored["state"]["blocker"]
    assert stored["state"]["requirements"][0]["status"] == "blocked"
    async with sessionmaker()() as db:
        summary = await work.summary(db, sid)
    assert summary["phase"] == "blocked" and summary["user_action"] == "intervene"
    assert summary["wait_type"] is None, "멈춘 목표를 기다리는 중으로 표시하면 안 된다"


@pytest.mark.asyncio
async def test_overdue_wait_does_not_stop_independent_requirement(client, auth, make_dir):
    past = (utcnow() - timedelta(minutes=5)).isoformat()
    sid, _h = await _goal(client, auth, make_dir, [
        {"id": "R1", "text": "승인 대기", "status": "waiting", "wait": _wait("approval:lead", timeout_at=past)},
        {"id": "R2", "text": "독립 작업", "status": "pending"}])
    await _end_turn(sid)
    assert len(continuations(await queued(sid))) == 1
    stored = await raw(sid)
    assert stored["state"]["status"] == "active"
    assert [r["status"] for r in stored["state"]["requirements"]] == ["blocked", "pending"]


@pytest.mark.asyncio
async def test_expiry_watchdog_blocks_goal_without_any_agent_run(client, auth, make_dir):
    """턴이 끝난 뒤 아무도 깨우지 않아도 만료 시각에 서버가 대기를 정리한다(예약 연결, Run 생성 없음)."""
    soon = (utcnow() + timedelta(hours=1)).isoformat()
    sid, _h = await _goal(client, auth, make_dir, [
        {"id": "R1", "text": "배포 대기", "status": "waiting", "wait": _wait("deploy:x1", timeout_at=soon)}])
    await _end_turn(sid)
    dogs = await _watchdogs(sid)
    assert len(dogs) == 1 and abs((dogs[0].due_at - datetime.fromisoformat(soon)).total_seconds()) < 1
    await _end_turn(sid)
    assert len(await _watchdogs(sid)) == 1, "턴마다 예약이 쌓이면 안 된다"

    # 만료 시각을 지난 것으로 만들고 예약 실행 경로(execute_action)를 그대로 부른다
    async with sessionmaker()() as db:
        row = await db.get(AppSetting, work.key(sid))
        rec = copy.deepcopy(row.value)
        rec["state"]["requirements"][0]["wait"]["timeout_at"] = (utcnow() - timedelta(seconds=1)).isoformat()
        row.value = rec
        await db.commit()
    result = await sched_service.execute_action(dogs[0])
    assert result["expired_waits"] == ["R1(deploy:x1)"]
    stored = await raw(sid)
    assert stored["state"]["status"] == "blocked" and "만료" in stored["state"]["blocker"]
    assert await queued(sid) == []


@pytest.mark.asyncio
async def test_expiry_watchdog_is_noop_when_dependency_arrived_and_rearms_when_extended(client, auth, make_dir):
    soon = (utcnow() + timedelta(hours=1)).isoformat()
    sid, _h = await _goal(client, auth, make_dir, [
        {"id": "R1", "text": "배포 대기", "status": "waiting", "wait": _wait("deploy:x2", timeout_at=soon)},
        {"id": "R2", "text": "다른 배포 대기", "status": "waiting", "wait": _wait("deploy:x3", timeout_at=soon)}])
    await _end_turn(sid)
    dog = (await _watchdogs(sid))[0]
    async with sessionmaker()() as db:
        await work.resolve_dependency(db, "deploy:x2", result={"status": "succeeded"}, source="t")
        await db.commit()
    # 아직 만료 전: 예약이 일찍 깨어나도 아무것도 하지 않고 다음 만료에 다시 건다
    async with sessionmaker()() as db:
        row = await db.get(AppSetting, work.key(sid))
        rec = copy.deepcopy(row.value)
        rec["state"]["requirements"][1]["wait"]["timeout_at"] = (utcnow() + timedelta(hours=5)).isoformat()
        row.value = rec
        dog_row = await db.get(ScheduledAction, dog.id)
        dog_row.status = "leased"
        await db.commit()
    assert (await sched_service.execute_action(dog))["expired_waits"] == []
    pending = [d for d in await _watchdogs(sid) if d.status == "pending"]
    assert len(pending) == 1 and pending[0].due_at > utcnow() + timedelta(hours=4)
    assert (await raw(sid))["state"]["status"] == "active"


# ---- D-3: 충족 이벤트 유실 방지 ---------------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_dependency_met_while_paused_is_recorded_so_later_resume_does_not_wait_forever(client, auth, make_dir):
    """사용자 중지 중에 의존성이 충족됐다. 재개는 막되 충족은 기록해야 사용자가 나중에 재개했을 때 지나간 이벤트를 기다리지 않는다."""
    sid, h = await _goal(client, auth, make_dir, [{"id": "R1", "text": "대기", "status": "waiting", "wait": _wait("worker:zz")}])
    cur = await get(client, h)
    paused = {**cur["state"], "status": "paused", "blocker": "사용자 중지 요청"}
    assert (await put(client, h, paused, revision=cur["revision"])).status_code == 200
    async with sessionmaker()() as db:
        assert await work.resolve_dependency(db, "worker:zz", result={"status": "succeeded"}, source="t") == []
        await db.commit()
    assert await queued(sid) == []
    stored = await raw(sid)
    assert stored["state"]["status"] == "paused"
    req = stored["state"]["requirements"][0]
    assert req["status"] == "pending" and req["wait"]["satisfied"] is True


@pytest.mark.asyncio
async def test_worker_finish_path_resolves_dependency_once(client, auth, make_dir):
    """테스트가 resolve 함수를 직접 부르는 것이 아니라 실제 작업자 종료 경로(manager._finish)에서 호출되는지."""
    sess, h = await lead(client, auth, make_dir)
    sid = UUID(sess["id"])
    wid = await make_worker(sid)
    assert (await put(client, h, contract(reqs=[
        {"id": "R1", "text": "작업자 결과", "status": "waiting", "wait": _wait(f"worker:{wid}")}]))).status_code == 200
    await _end_turn(sid)
    assert await queued(sid) == []
    await finish(await new_run(wid, session_id=wid, prompt="work"), RunStatus.succeeded, result_text="끝")
    stored = await raw(sid)
    req = stored["state"]["requirements"][0]
    assert req["status"] == "pending" and req["wait"]["satisfied"] is True
    # 작업 보고 Run 이 이미 기획을 깨우므로 후속을 하나 더 만들지 않는다
    assert len(await queued(sid)) == 1


@pytest.mark.asyncio
async def test_stopped_worker_records_satisfaction_without_waking_planner(client, auth, make_dir):
    """기획·사용자가 직접 멈춘 작업자는 보고도 억제된다. 의존성 충족 기록만 하고 기획을 다시 깨우지 않는다."""
    from ttakkari.runs.agent_control import mark_report_suppressed
    sess, h = await lead(client, auth, make_dir)
    sid = UUID(sess["id"])
    wid = await make_worker(sid)
    assert (await put(client, h, contract(reqs=[
        {"id": "R1", "text": "작업자 결과", "status": "waiting", "wait": _wait(f"worker:{wid}")}]))).status_code == 200
    await _end_turn(sid)
    rid = await new_run(wid, session_id=wid, prompt="work")
    async with sessionmaker()() as db:
        await mark_report_suppressed(db, await db.get(Run, rid))
        await db.commit()
    await finish(rid, RunStatus.cancelled)
    assert await queued(sid) == []
    req = (await raw(sid))["state"]["requirements"][0]
    assert req["status"] == "pending" and req["wait"]["satisfied"] is True


@pytest.mark.asyncio
async def test_satisfied_state_level_wait_does_not_stall_goal_silently(client, auth, make_dir):
    """충족된 waiting_dependencies 가 남아 있다는 이유로 후속 없이 조용히 멈추면 안 된다(완료 확인 후속은 만들어야 한다)."""
    sess, h = await lead(client, auth, make_dir)
    sid = UUID(sess["id"])
    state = contract(reqs=[{"id": "R1", "text": "끝난 요구", "status": "done", "evidence": "pytest tests/test_x.py: 1 passed in 0.1s"}],
                     waiting_dependencies=[{"target": "worker:old", "satisfied": True, "result": {"status": "succeeded"}}])
    assert (await put(client, h, state)).status_code == 200
    await _end_turn(sid)
    assert len(continuations(await queued(sid))) == 1


# ---- summary 필드 ----------------------------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_summary_wait_fields_for_waiting_goal(client, auth, make_dir):
    sid, _h = await _goal(client, auth, make_dir, [{"id": "R1", "text": "대기", "status": "waiting", "wait": _wait("worker:q")}])
    async with sessionmaker()() as db:
        summary = await work.summary(db, sid)
    assert summary["phase"] == "waiting" and summary["wait_type"] == "external_dependency"
    assert summary["has_owner"] is True, "만료 시각이 있는 대기는 책임자가 있는 대기다"
    assert summary["waiting_dependencies"][0]["timeout_at"]


# ---- E-3: 재개 판정 --------------------------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_status_question_does_not_resume_user_paused_goal(client, auth, make_dir):
    sess, h = await lead(client, auth, make_dir)
    c = contract(status="paused", blocker="사용자 일시중지 요청")
    assert (await put(client, h, c)).status_code == 200
    r = await client.post(f"/api/sessions/{sess['id']}/runs", headers=auth, json={"prompt": "왜 멈췄어? 계속 진행 상황 알려줘"})
    assert r.status_code == 202
    assert (await get(client, h))["state"]["status"] == "paused"


@pytest.mark.asyncio
async def test_explicit_resume_gives_fresh_budget(client, auth, make_dir):
    """checkpoint 로 재개하면 무진전 예산이 초기화된다. 사용자 요청으로 재개해도 같아야 한다(바로 다시 blocked 되면 안 된다)."""
    sess, h = await lead(client, auth, make_dir)
    sid = UUID(sess["id"])
    assert (await put(client, h, contract(status="blocked", blocker="무진전"))).status_code == 200
    async with sessionmaker()() as db:
        row = await db.get(AppSetting, work.key(sid))
        row.value = {**row.value, "unchanged_continuations": 2, "usage_waits": 3}
        await db.commit()
    r = await client.post(f"/api/sessions/{sess['id']}/runs", headers=auth, json={"prompt": "다시 재개해줘"})
    assert r.status_code == 202
    stored = await raw(sid)
    assert stored["state"]["status"] == "active"
    assert stored.get("unchanged_continuations", 0) == 0 and "usage_waits" not in stored


# ---- E-1: 번호만 붙은 완료 조건 제목 ----------------------------------------------------------------------------------

_SPEC = """이번 작업을 모두 수행해라.
{h11}
1. 백엔드 수정해라
2. 테스트 작성해라
3. 배포해라
{h15}
1. 모든 테스트 통과해야 한다
2. 보고서 제출해야 한다
3. 배포 검증해야 한다
{h16}
1. 새 LLM 호출 추가 금지
2. 기존 테스트 삭제 금지
"""


@pytest.mark.parametrize("h11,h15,h16", [
    ("11. 작업 범위", "15. 최종 완료 조건", "16. 금지 사항"),
    ("11. 작업 범위:", "15. 최종 완료 조건:", "16. 금지 사항:"),
    ("**11. 작업 범위**", "**15. 최종 완료 조건**", "**16. 금지 사항**"),
    ("§11 작업 범위", "§15 최종 완료 조건", "§16 금지 사항"),
])
def test_completion_section_with_plain_numbered_heading(h11, h15, h16):
    text = _SPEC.format(h11=h11, h15=h15, h16=h16)
    items = goal_signals.extract_items(text)
    assert [i["text"] for i in items] == ["모든 테스트 통과해야 한다", "보고서 제출해야 한다", "배포 검증해야 한다"], items
    assert goal_signals.is_complex(text) is True


@pytest.mark.asyncio
async def test_plain_numbered_completion_section_builds_contract_through_request_path(client, auth, make_dir):
    sess, h = await lead(client, auth, make_dir)
    text = _SPEC.format(h11="11. 작업 범위", h15="15. 최종 완료 조건", h16="16. 금지 사항")
    assert (await client.post(f"/api/sessions/{sess['id']}/runs", headers=auth, json={"prompt": text})).status_code == 202
    texts = [r["text"] for r in (await get(client, h))["state"]["requirements"]]
    assert texts == ["모든 테스트 통과해야 한다", "보고서 제출해야 한다", "배포 검증해야 한다"]
    assert not any(t.startswith(("15", "11", "작업 범위", "최종")) for t in texts)


def test_status_and_report_messages_do_not_create_contracts():
    for text in ["현재 상태 알려줘", "어제 보고서 요약해줘", "로그 분석해서 원인만 알려줘", "왜 멈췄어? 계속 진행 상황 알려줘"]:
        assert goal_signals.is_complex(text) is False, text


# ---- 운영 사례 C: 실제 흐름(after_run 반복 → 작업자 종료 → 재개) ---------------------------------------------------

@pytest.mark.asyncio
async def test_case_c_flow_eight_turns_then_worker_finish_resumes_without_blocking(client, auth, make_dir):
    """사례 C 는 승인·러너 확보를 기다리며 자동 이어가기 8회를 쓰고 blocked 됐다. 같은 8턴을 실제 종료 경로로 돌려도 후속·예산 소모가
    없고, 의존한 작업자가 실제 종료 경로로 끝나면 기획이 한 번 이어지며 blocked 되지 않는다."""
    sess, h = await lead(client, auth, make_dir)
    sid = UUID(sess["id"])
    wid = await make_worker(sid)
    assert (await put(client, h, contract(reqs=[
        {"id": "R1", "text": "러너 확보 후 배포", "status": "waiting", "wait": _wait(f"worker:{wid}", reason="승인·러너 확보 대기")},
        {"id": "R2", "text": "완료 요구", "status": "done", "evidence": "pytest tests/test_x.py: 1 passed in 0.1s"}]))).status_code == 200
    for _ in range(8):
        await _end_turn(sid)
    stored = await raw(sid)
    assert await queued(sid) == [] and stored["state"]["status"] == "active"
    assert stored.get("unchanged_continuations", 0) == 0 and stored.get("auto_continuations", 0) == 0
    await finish(await new_run(wid, session_id=wid, prompt="runner"), RunStatus.succeeded, result_text="러너 확보 완료")
    assert len(await queued(sid)) == 1  # 작업 보고 Run 한 개가 기획을 깨운다(후속 중복 없음)
    req = (await raw(sid))["state"]["requirements"][0]
    assert req["status"] == "pending" and req["wait"]["satisfied"] is True
    assert (await raw(sid))["state"]["status"] == "active"
