"""작업자 활동 상태(working/blocked/stalled)와 긴 대기·정지 의심 알림(herdr 의 상태 모델을 따까리 이벤트에 맞춘 것)."""
from __future__ import annotations

import uuid
from datetime import timedelta

import pytest
from helpers import wait_run
from test_orchestrator import _orch, _session, _wait

from ttakkari.config import get_settings
from ttakkari.db import sessionmaker
from ttakkari.models import AppSetting, Run, RunEvent, RunOrigin, RunStatus, utcnow
from ttakkari.orchestrator.runtime import issue_token
from ttakkari.runs import activity, watchdog

STALL = 1800


def c(**kw):
    base = {"active_status": None, "waiting_limit": False, "waiting_reason": None, "is_stuck": False, "last_progress_type": None,
                "last_progress_age_s": None, "done_status": None, "completion_status": None, "stall_seconds": STALL}
    return activity.classify(**{**base, **kw})


@pytest.mark.parametrize("kw,expected", [
    ({"active_status": "running", "last_progress_age_s": 30, "last_progress_type": "message"}, "working"),
    ({"active_status": "running"}, "working"),
    ({"active_status": "running", "last_progress_age_s": STALL + 1, "last_progress_type": "message"}, "stalled"),
    # 도구 호출이 결과를 기다리는 중이면 stall 기준이 아니라 Run 시간 제한까지 일하는 것으로 본다.
    ({"active_status": "running", "last_progress_age_s": STALL + 600, "last_progress_type": "tool.call"}, "working"),
    ({"active_status": "running", "last_progress_age_s": activity.TOOL_CALL_MAX_SECONDS + 1, "last_progress_type": "tool.call"}, "stalled"),
    ({"active_status": "running", "is_stuck": True}, "stalled"),
    ({"active_status": "queued", "waiting_limit": True, "waiting_reason": "한도 대기"}, "blocked"),
    ({"active_status": "queued"}, "queued"),
    ({"done_status": "succeeded", "completion_status": "완료"}, "done"),
    ({"done_status": "succeeded", "completion_status": "부분"}, "partial"),
    ({"done_status": "succeeded", "completion_status": "막힘"}, "blocked"),
    ({"done_status": "succeeded"}, "done"),
    ({"done_status": "failed"}, "failed"),
    ({"done_status": "cancelled"}, "cancelled"),
    ({}, "idle"),
])
def test_classify(kw, expected):
    assert c(**kw)[0] == expected


async def _worker(client, auth, make_dir):
    from ttakkari.models import ChatSession, SessionKind
    orch = await _session(client, auth, make_dir, kind="orchestrator")
    wk = await _session(client, auth, make_dir, kind="chat", title="정지 시험")
    async with sessionmaker()() as db:  # 작업 세션은 API 로 만들 수 없어 행을 직접 바꾼다(test_agent_stop_all 과 같은 방식)
        row = await db.get(ChatSession, uuid.UUID(wk["id"]))
        row.kind, row.parent_id = SessionKind.worker, uuid.UUID(orch["id"])
        await db.commit()
    return orch, wk


async def _running_run(wk, *, started_ago, event_type=None, event_ago=None):
    async with sessionmaker()() as db:
        run = Run(session_id=uuid.UUID(wk["id"]), workspace_id=uuid.UUID(wk["workspace_id"]), engine="claude",
                  prompt="p", status=RunStatus.running, origin=RunOrigin.orchestrator,
                  started_at=utcnow() - started_ago)
        db.add(run)
        await db.flush()
        if event_type:
            run.last_seq += 1
            db.add(RunEvent(run_id=run.id, seq=run.last_seq, type=event_type, payload={},
                            created_at=utcnow() - event_ago))
        await db.commit()
        return run.id


async def _reports(client, auth, orch_id):
    runs = (await client.get(f"/api/sessions/{orch_id}/runs", headers=auth)).json()
    return [r for r in runs if r["origin"] == "report"]


async def test_scan_notifies_orchestrator_once_for_a_silent_run(client, auth, make_dir):
    orch, wk = await _worker(client, auth, make_dir)
    rid = await _running_run(wk, started_ago=timedelta(hours=2), event_type="message", event_ago=timedelta(hours=1))
    assert await watchdog.scan_stalled_workers() == 1
    reports = await _reports(client, auth, orch["id"])
    assert len(reports) == 1 and "[작업 정지 의심]" in reports[0]["prompt"] and str(rid) in reports[0]["prompt"]
    assert "60분째" in reports[0]["prompt"]
    assert await watchdog.scan_stalled_workers() == 0  # 같은 Run 은 다시 알리지 않는다
    async with sessionmaker()() as db:
        assert await db.get(AppSetting, f"stall:{rid}") is not None
        assert (await db.get(Run, rid)).status == RunStatus.running  # 죽이지 않는다


async def test_scan_ignores_recent_progress_and_pending_tool_call(client, auth, make_dir):
    orch, wk = await _worker(client, auth, make_dir)
    await _running_run(wk, started_ago=timedelta(hours=2), event_type="message", event_ago=timedelta(minutes=5))
    assert await watchdog.scan_stalled_workers() == 0
    orch2, wk2 = await _worker(client, auth, make_dir)
    await _running_run(wk2, started_ago=timedelta(hours=1), event_type="tool.call", event_ago=timedelta(minutes=50))
    assert await watchdog.scan_stalled_workers() == 0
    assert await _reports(client, auth, orch["id"]) == [] and await _reports(client, auth, orch2["id"]) == []


async def test_worker_out_reports_activity(client, auth, make_dir):
    orch, wk = await _worker(client, auth, make_dir)
    await _running_run(wk, started_ago=timedelta(hours=2), event_type="message", event_ago=timedelta(hours=1))
    h = _orch(issue_token(orch["id"]))
    detail = (await client.get(f"/api/orchestrator/workers/{wk['id']}", headers=h)).json()
    # 시험용 Run 은 프로세스 핸들이 없어 is_stuck 사유가 먼저 잡힌다. 어느 쪽이든 stalled 로 보인다.
    assert detail["activity"] == "stalled" and detail["activity_detail"]


async def test_long_quota_wait_notifies_orchestrator(client, auth, make_dir, monkeypatch):
    """한도 대기가 기준보다 길면 Run 이 끝나지 않아도 기획 세션에 한 번 알린다."""
    from ttakkari.runs import manager as mgr
    monkeypatch.setattr(get_settings(), "worker_wait_notice_seconds", 60)
    orch = await _session(client, auth, make_dir, kind="orchestrator")
    r = await client.post("/api/orchestrator/workers", headers=_orch(issue_token(orch["id"])),
                          json={"title": "대기", "prompt": "write report", "engine": "claude"})
    assert r.status_code == 201, r.text
    wk = r.json()
    await wait_run(client, auth, wk["active_run_id"])
    async with sessionmaker()() as db:
        run = await db.get(Run, uuid.UUID(wk["active_run_id"]))
        sess = await db.get(mgr.ChatSession, run.session_id)
        profile = mgr.profiles_mod.Profile(id=uuid.uuid4(), name="가상 계정", engine="claude", model="sonnet")
        import time
        run.status = RunStatus.running
        await mgr.manager._wait_for_reset(db, run, sess, [(profile, time.time() + 7200)])
    reports = [x for x in await _reports(client, auth, orch["id"]) if "[작업 대기]" in x["prompt"]]
    assert len(reports) == 1 and "가상 계정" in reports[0]["prompt"] and "reroute=True" in reports[0]["prompt"]
    assert await _wait(lambda: _has_status(client, auth, wk["worker_id"], "blocked"), what="blocked 상태")


async def _has_status(client, auth, wid, state):
    s = (await client.get(f"/api/sessions/{wid}", headers=auth)).json()
    return s if s.get("waiting_limit") else None
