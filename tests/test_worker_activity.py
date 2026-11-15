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


async def _queued_run(sid, wsid, *, created_ago, resume_after=None):
    async with sessionmaker()() as db:
        run = Run(session_id=uuid.UUID(sid), workspace_id=uuid.UUID(wsid), engine="claude", prompt="p",
                  status=RunStatus.queued, origin=RunOrigin.orchestrator, created_at=utcnow() - created_ago,
                  resume_after=resume_after)
        db.add(run)
        await db.commit()
        return run.id


@pytest.fixture
def pushes(monkeypatch):
    sent: list[dict] = []

    async def fake(kind, title, body="", **kw):
        sent.append({"kind": kind, "title": title, "body": body, **kw})
        return {"sent": 1}

    monkeypatch.setattr(watchdog, "notify", fake)
    return sent


async def test_queued_worker_notifies_parent_once(client, auth, make_dir, pushes):
    orch, wk = await _worker(client, auth, make_dir)
    rid = await _queued_run(wk["id"], wk["workspace_id"], created_ago=timedelta(minutes=30))
    assert await watchdog.scan_queued_runs() == 1
    reports = await _reports(client, auth, orch["id"])
    assert len(reports) == 1 and "[작업 시작 지연 의심]" in reports[0]["prompt"] and str(rid) in reports[0]["prompt"]
    assert await watchdog.scan_queued_runs() == 0  # 같은 Run 은 다시 알리지 않는다
    assert pushes == []  # 부모가 있으면 사용자 푸시는 에스컬레이션 단계에서만 나간다


async def test_queued_scan_ignores_recent_limit_wait_and_busy_session(client, auth, make_dir, pushes):
    orch, wk = await _worker(client, auth, make_dir)
    await _queued_run(wk["id"], wk["workspace_id"], created_ago=timedelta(minutes=2))  # 아직 짧다
    orch2, wk2 = await _worker(client, auth, make_dir)
    await _queued_run(wk2["id"], wk2["workspace_id"], created_ago=timedelta(hours=1),
                      resume_after=utcnow() + timedelta(hours=1))  # 한도 대기는 별도 알림이 있다
    orch3, wk3 = await _worker(client, auth, make_dir)
    await _running_run(wk3, started_ago=timedelta(minutes=5), event_type="message", event_ago=timedelta(minutes=1))
    await _queued_run(wk3["id"], wk3["workspace_id"], created_ago=timedelta(hours=1))  # 앞선 Run 을 기다리는 정상 대기
    assert await watchdog.scan_queued_runs() == 0
    for o in (orch, orch2, orch3):
        assert await _reports(client, auth, o["id"]) == []
    assert pushes == []


async def test_queued_run_without_parent_pushes_user_directly(client, auth, make_dir, pushes):
    orch = await _session(client, auth, make_dir, kind="orchestrator")
    rid = await _queued_run(orch["id"], orch["workspace_id"], created_ago=timedelta(minutes=30))
    assert await watchdog.scan_queued_runs() == 1
    assert len(pushes) == 1 and pushes[0]["kind"] == watchdog.PushKind.stall and pushes[0]["target_id"] == rid
    assert pushes[0].get("source_session_id") is None  # 하위 알림으로 바뀌어 기본 꺼짐이 되지 않게 한다
    assert await watchdog.scan_queued_runs() == 0
    assert await watchdog.escalate_stalls() == 0  # 이미 사용자에게 갔으므로 다시 올리지 않는다


async def _age_notice(key, seconds):
    async with sessionmaker()() as db:
        row = await db.get(AppSetting, key)
        row.updated_at = utcnow() - timedelta(seconds=seconds)
        await db.commit()


async def test_stalled_worker_escalates_to_user_once(client, auth, make_dir, pushes, monkeypatch):
    monkeypatch.setattr(get_settings(), "stall_escalate_seconds", 600)
    _orch, wk = await _worker(client, auth, make_dir)
    rid = await _running_run(wk, started_ago=timedelta(hours=2), event_type="message", event_ago=timedelta(hours=1))
    assert await watchdog.scan_stalled_workers() == 1
    assert await watchdog.escalate_stalls() == 0  # 알림 직후에는 기다린다
    await _age_notice(f"stall:{rid}", 700)
    assert await watchdog.escalate_stalls() == 1
    assert len(pushes) == 1 and pushes[0]["dedupe_key"] == f"stall-esc:{rid}"
    assert await watchdog.escalate_stalls() == 0


async def test_escalation_is_dropped_when_run_recovered(client, auth, make_dir, pushes, monkeypatch):
    monkeypatch.setattr(get_settings(), "stall_escalate_seconds", 600)
    _orch, wk = await _worker(client, auth, make_dir)
    rid = await _running_run(wk, started_ago=timedelta(hours=2), event_type="message", event_ago=timedelta(hours=1))
    assert await watchdog.scan_stalled_workers() == 1
    async with sessionmaker()() as db:  # 알림 뒤에 진행 이벤트가 다시 생겼다
        run = await db.get(Run, rid)
        run.last_seq += 1
        db.add(RunEvent(run_id=rid, seq=run.last_seq, type="message", payload={}, created_at=utcnow()))
        await db.commit()
    await _age_notice(f"stall:{rid}", 700)
    assert await watchdog.escalate_stalls() == 0 and pushes == []


async def test_queued_escalation_pushes_when_still_queued(client, auth, make_dir, pushes, monkeypatch):
    monkeypatch.setattr(get_settings(), "stall_escalate_seconds", 600)
    _orch, wk = await _worker(client, auth, make_dir)
    rid = await _queued_run(wk["id"], wk["workspace_id"], created_ago=timedelta(minutes=30))
    assert await watchdog.scan_queued_runs() == 1
    await _age_notice(f"qstall:{rid}", 700)
    assert await watchdog.escalate_stalls() == 1 and "queued" in pushes[0]["body"]
