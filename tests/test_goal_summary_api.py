"""GET /api/sessions/{id} 의 goal_summary(목표 상태 요약). 사용자 인증으로 읽고, 조회는 계약을 바꾸지 않는다."""
from __future__ import annotations

from datetime import timedelta
from uuid import UUID

import pytest
from goal_helpers import (
    contract,
    finish,
    lead,
    make_worker,
    new_run,
    put,
    queued,
    raw,
    request,
)
from sqlalchemy import func, select

from ttakkari.db import sessionmaker
from ttakkari.models import Run, RunStatus, utcnow
from ttakkari.runs.manager import manager

SUMMARY_KEYS = {
    "status", "phase", "goal", "revision", "completed", "pending", "completed_count", "pending_count", "running_tasks",
    "next_action", "waiting_until", "stop_reason", "blocker_kind", "auto_resumable", "user_action",
    "user_intervention_needed", "additional_request_count", "updated_at",
    "wait_type", "waiting_dependencies", "has_partial_failure", "has_owner",
}
NUMBERED4 = ("다음 4단계 백엔드 작업을 진행해 주세요.\n1. 데이터베이스 마이그레이션 스키마 작성 및 적용\n"
             "2. 신규 API 엔드포인트 구현 및 입력 유효성 검사\n3. 단위 테스트 및 통합 테스트 작성 후 통과 확인\n"
             "4. main 브랜치 푸시 및 백엔드 배포 예약\n")


@pytest.fixture(autouse=True)
def no_dispatch(monkeypatch):
    async def _noop(*args, **kwargs):
        return None
    monkeypatch.setattr(manager, "dispatch", _noop)


async def summary_of(client, auth, sid) -> dict | None:
    r = await client.get(f"/api/sessions/{sid}", headers=auth)
    assert r.status_code == 200, r.text
    return r.json()["goal_summary"]


async def test_requires_user_authentication(client, auth, make_dir):
    sess, h = await lead(client, auth, make_dir)
    assert (await put(client, h, contract())).status_code == 200
    assert (await client.get(f"/api/sessions/{sess['id']}")).status_code == 401
    # 목표 관리 전용 토큰은 사용자 API 의 인증이 아니다.
    assert (await client.get(f"/api/sessions/{sess['id']}", headers=h)).status_code in (401, 403)


async def test_no_contract_and_non_orchestrator_sessions_have_null_summary(client, auth, make_dir):
    sess, _ = await lead(client, auth, make_dir)
    assert await summary_of(client, auth, sess["id"]) is None
    r = await client.get(f"/api/sessions/{sess['id']}", headers=auth)
    assert "goal_summary" in r.json() and r.json()["goal_summary"] is None
    for kind in ("chat", "desk"):
        other, _ = await lead(client, auth, make_dir, kind=kind)
        assert await summary_of(client, auth, other["id"]) is None
    # 계약이 있어도 기획이 아닌 세션 목록·상세에는 요약이 없다.
    listing = (await client.get("/api/sessions", headers=auth)).json()
    assert listing and all(item["goal_summary"] is None for item in listing)


async def test_active_contract_summary_shape_and_progress(client, auth, make_dir):
    sess, h = await lead(client, auth, make_dir)
    sid = UUID(sess["id"])
    reqs = [{"id": "A", "text": "첫 요구", "status": "done", "evidence": "pytest -q: 3 passed"},
            {"id": "B", "text": "둘째 요구", "status": "pending"}]
    assert (await put(client, h, contract("목표 문장", reqs=reqs, next_action="B 구현"))).status_code == 200
    w = await make_worker(sid, "작업자 하나")
    await new_run(sid, session_id=w, status=RunStatus.running)
    got = await summary_of(client, auth, sid)
    assert set(got) == SUMMARY_KEYS
    assert (got["status"], got["phase"], got["goal"], got["next_action"]) == ("active", "running", "목표 문장", "B 구현")
    assert [r["id"] for r in got["completed"]] == ["A"] and got["completed"][0]["evidence"] == "pytest -q: 3 passed"
    assert [r["id"] for r in got["pending"]] == ["B"] and (got["completed_count"], got["pending_count"]) == (1, 1)
    assert got["running_tasks"] == ["작업자 하나"]
    assert (got["auto_resumable"], got["user_action"], got["user_intervention_needed"], got["waiting_until"]) == (True, "none", False, None)
    # 응답에 계약 내부(원본 요청·이력·추가 요청 본문·금지 조건)는 없다.
    body = (await client.get(f"/api/sessions/{sid}", headers=auth)).text
    for leaked in ("original_request", "history", "additional_requests", "original_constraints", "decisions", "strategy"):
        assert leaked not in body


async def test_run_finish_alone_never_completes_the_goal(client, auth, make_dir):
    sess, _ = await lead(client, auth, make_dir)
    sid = UUID(sess["id"])
    run = await request(sid, NUMBERED4)  # 접수로 열린 자동 계약
    await finish(run.id, RunStatus.succeeded, result_text="모두 끝났습니다")
    got = await summary_of(client, auth, sid)
    assert got["status"] == "active" and got["phase"] == "running" and got["pending_count"] == 4 and got["completed_count"] == 0


async def test_waiting_usage_limit_is_distinguished_from_stop_and_block(client, auth, make_dir):
    sess, h = await lead(client, auth, make_dir)
    sid = UUID(sess["id"])
    assert (await put(client, h, contract())).status_code == 200
    await finish(await new_run(sid), RunStatus.failed, error="You've hit your usage limit. Try again in 3 hours.")
    got = await summary_of(client, auth, sid)
    assert (got["status"], got["phase"]) == ("active", "waiting")
    assert got["waiting_until"] is not None and got["auto_resumable"] is True
    assert (got["user_action"], got["user_intervention_needed"], got["stop_reason"]) == ("none", False, "")
    from datetime import datetime
    until = datetime.fromisoformat(got["waiting_until"])
    assert timedelta(hours=2, minutes=50) < until - utcnow() < timedelta(hours=3, minutes=1)
    # 대기 시각이 지나면 더는 대기가 아니다.
    async with sessionmaker()() as db:
        from ttakkari.models import AppSetting
        from ttakkari.orchestrator import work
        row = await db.get(AppSetting, work.key(sid))
        row.value = {**row.value, "waiting_until": (utcnow() - timedelta(minutes=1)).isoformat()}
        await db.commit()
    got = await summary_of(client, auth, sid)
    assert got["phase"] == "running" and got["waiting_until"] is None


async def test_blocked_paused_and_complete_summaries(client, auth, make_dir):
    # blocked: 영구 실패 → 사용자 조치 필요
    sess, h = await lead(client, auth, make_dir)
    sid = UUID(sess["id"])
    assert (await put(client, h, contract())).status_code == 200
    await finish(await new_run(sid), RunStatus.failed, error="authentication failed 401")
    got = await summary_of(client, auth, sid)
    assert (got["status"], got["phase"], got["user_action"]) == ("blocked", "blocked", "intervene")
    assert got["user_intervention_needed"] is True and got["auto_resumable"] is False
    assert got["blocker_kind"] == "permanent_failure" and "authentication failed" in got["stop_reason"]
    # paused: 사용자 중지 → 조치가 필요한 막힘이 아니라 중지
    sess, h = await lead(client, auth, make_dir)
    sid = UUID(sess["id"])
    assert (await put(client, h, contract())).status_code == 200
    r_id = await new_run(sid)
    from ttakkari.runs.termination import CancelRequest, record_request
    async with sessionmaker()() as db:
        await record_request(db, await db.get(Run, r_id), CancelRequest(actor="user", source="api.run_cancel", reason="사용자 중단", user_intent=True, scope="run"))
        await db.commit()
    await finish(r_id, RunStatus.cancelled, error="사용자가 중단했습니다.")
    got = await summary_of(client, auth, sid)
    assert (got["status"], got["phase"], got["user_action"]) == ("paused", "paused", "resume")
    assert got["user_intervention_needed"] is False and got["auto_resumable"] is False
    assert "cancelled" in got["stop_reason"] and got["blocker_kind"] is None
    # complete
    sess, h = await lead(client, auth, make_dir)
    sid = UUID(sess["id"])
    done = [{"id": "R1", "text": "일", "status": "done", "evidence": "pytest -q: 1 passed"}]
    assert (await put(client, h, contract(reqs=done, status="complete", next_action=""))).status_code == 200
    got = await summary_of(client, auth, sid)
    assert (got["status"], got["phase"], got["pending_count"], got["auto_resumable"]) == ("complete", "complete", 0, False)


async def test_get_never_changes_the_contract_or_creates_runs(client, auth, make_dir):
    sess, h = await lead(client, auth, make_dir)
    sid = UUID(sess["id"])
    assert (await put(client, h, contract())).status_code == 200
    await finish(await new_run(sid, status=RunStatus.running), RunStatus.failed, error="connection reset by peer")
    before, runs_before = await raw(sid), len(await queued(sid))
    async with sessionmaker()() as db:
        count_before = await db.scalar(select(func.count()).select_from(Run).where(Run.session_id == sid))
    for _ in range(3):
        assert await summary_of(client, auth, sid) is not None
    assert await raw(sid) == before and len(await queued(sid)) == runs_before
    async with sessionmaker()() as db:
        assert await db.scalar(select(func.count()).select_from(Run).where(Run.session_id == sid)) == count_before


async def test_summary_failure_does_not_break_the_session_read(client, auth, make_dir, monkeypatch):
    from ttakkari.orchestrator import work
    sess, h = await lead(client, auth, make_dir)
    assert (await put(client, h, contract())).status_code == 200

    async def boom(db, sid):
        raise RuntimeError("boom")

    monkeypatch.setattr(work, "summary", boom)
    r = await client.get(f"/api/sessions/{sess['id']}", headers=auth)
    assert r.status_code == 200 and r.json()["goal_summary"] is None and r.json()["id"] == sess["id"]


async def test_existing_session_fields_are_unchanged(client, auth, make_dir):
    sess, _ = await lead(client, auth, make_dir)
    got = (await client.get(f"/api/sessions/{sess['id']}", headers=auth)).json()
    for key in ("id", "workspace_id", "title", "engine", "kind", "parent_id", "pool", "active_run_id", "last_run_status",
                "waiting_limit", "is_stuck", "last_result"):
        assert key in got
