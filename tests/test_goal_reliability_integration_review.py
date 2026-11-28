"""stab2-de 통합 리뷰(Track 경계)에서 찾은 결함의 재현 시험. 수정 전 코드에서는 실패해야 한다."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from uuid import UUID

import pytest
from goal_helpers import contract, get, lead, put, queued, raw
from sqlalchemy import func, select

from ttakkari.db import sessionmaker
from ttakkari.models import AppSetting, ChatSession, RunEvent, utcnow
from ttakkari.orchestrator import work


@pytest.fixture(autouse=True)
def no_dispatch(monkeypatch):
    from ttakkari.runs.manager import manager

    async def _noop(*args, **kwargs):
        return None

    monkeypatch.setattr(manager, "dispatch", _noop)


@pytest.mark.asyncio
async def test_agent_declared_wait_timeout_is_capped(client, auth, make_dir):
    """에이전트가 아주 먼 timeout_at 을 적어 대기 선언만으로 무진전 예산을 사실상 무기한 보호하지 못한다."""
    _, h = await lead(client, auth, make_dir)
    far = (utcnow() + timedelta(days=3650)).isoformat()
    res = await put(client, h, contract(reqs=[
        {"id": "R1", "text": "대기", "status": "waiting", "wait": {"target": "approval:x", "timeout_at": far}}]))
    assert res.status_code == 200, res.text
    stamped = (await get(client, h))["state"]["requirements"][0]["wait"]["timeout_at"]
    assert datetime.fromisoformat(stamped) <= utcnow() + timedelta(days=7, minutes=1)
    # 상한 안의 명시 시각은 그대로 둔다
    soon = (utcnow() + timedelta(hours=3)).isoformat()
    cur = await get(client, h)
    again = {**cur["state"], "requirements": [
        {"id": "R1", "text": "대기", "status": "waiting", "wait": {"target": "approval:x", "timeout_at": soon}}]}
    assert (await put(client, h, again, revision=cur["revision"])).status_code == 200
    assert (await get(client, h))["state"]["requirements"][0]["wait"]["timeout_at"] == datetime.fromisoformat(soon).isoformat()


@pytest.mark.asyncio
async def test_dependency_event_does_not_overwrite_a_pause_committing_concurrently(client, auth, make_dir):
    """사용자 중지로 after_run 이 세션 잠금을 쥐고 계약을 paused 로 쓰는 중에 의존성 충족 이벤트가 들어온다. 이벤트 쪽이 낡은
    active 상태로 계약을 다시 써 중지를 덮거나 후속 Run 을 만들면 사용자 취소가 무시된다."""
    sess, h = await lead(client, auth, make_dir)
    sid = UUID(sess["id"])
    assert (await put(client, h, contract(reqs=[
        {"id": "R1", "text": "대기", "status": "waiting", "wait": {"target": "deploy:race1"}}]))).status_code == 200
    locked, release = asyncio.Event(), asyncio.Event()

    async def pauser():
        async with sessionmaker()() as db:
            await db.scalar(select(ChatSession.id).where(ChatSession.id == sid).with_for_update())  # after_run 과 같은 잠금
            row = await db.get(AppSetting, work.key(sid))
            value = dict(row.value)
            value["state"] = {**value["state"], "status": "paused", "blocker": "사용자 중지"}
            row.value = value
            await db.flush()
            locked.set()
            await release.wait()
            await db.commit()

    async def resolver():
        await locked.wait()
        async with sessionmaker()() as db:
            out = await work.resolve_dependency(db, "deploy:race1", result={"status": "succeeded"}, source="t")
            await db.commit()
            return out

    task = asyncio.create_task(pauser())
    resolving = asyncio.create_task(resolver())
    await locked.wait()
    await asyncio.sleep(0.5)  # 이벤트 쪽이 낡은 상태를 읽고 쓰기까지 진행할 시간
    release.set()
    await task
    assert await asyncio.wait_for(resolving, 10) == []
    assert await queued(sid) == []
    stored = await raw(sid)
    assert stored["state"]["status"] == "paused"
    assert stored["state"]["requirements"][0]["wait"]["satisfied"] is True


@pytest.mark.asyncio
async def test_observation_report_counts_existing_events_read_only(client, auth, make_dir):
    """§12 집계 스크립트가 기존 RunEvent·AuditLog·Run.usage 에서 값을 세고 아무것도 쓰지 않는다."""
    import importlib.util
    from pathlib import Path

    from goal_helpers import finish, new_run

    from ttakkari.models import RunStatus

    spec = importlib.util.spec_from_file_location("goal_reliability_report", Path(__file__).parents[1] / "scripts" / "goal_reliability_report.py")
    report = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(report)

    sess, h = await lead(client, auth, make_dir)
    sid = UUID(sess["id"])
    assert (await put(client, h, contract())).status_code == 200
    for _ in range(2):  # 첫 턴은 체크포인트 진전이라 후속 1개를 만든다. 둘째 턴은 무진전이고 그 후속이 소유자라 더 만들지 않는다
        await finish(await new_run(sid), RunStatus.succeeded, result_text="턴 끝")
    async with sessionmaker()() as db:
        before = await db.scalar(select(func.count()).select_from(RunEvent))
        data = await report.collect(db, utcnow() - timedelta(hours=1))
        after = await db.scalar(select(func.count()).select_from(RunEvent))
    assert before == after
    assert data["events"]["work.continued"] == 1 and data["events"]["work.no_progress"] == 1
    assert data["incomplete_runs"]["ended_with_unfinished_goal(work.continued)"] == 1
    assert data["goals"]["by_status"] == {"active": 1}
    assert data["automation_spend"]["continuation_and_closure_runs"]["runs"] == 1
    assert data["cancellation"]["cancelled"] == 0


@pytest.mark.asyncio
async def test_commit_claim_check_has_a_git_process_budget(monkeypatch, tmp_path):
    """완료 보고에 커밋 해시를 수백 개 늘어놓아도 git 프로세스를 그만큼 띄우지 않는다. 상한을 넘는 주장은 모순이 아니라 미확인이다."""
    from ttakkari.orchestrator import observed

    calls: list[str] = []

    async def fake_is_git(_root):
        return True

    async def fake_exists(_root, sha):
        calls.append(sha)
        return False

    monkeypatch.setattr(observed, "_is_git", fake_is_git)
    monkeypatch.setattr(observed, "_commit_exists", fake_exists)
    text = "\n".join(f"커밋 {i:07x}a 반영" for i in range(60))
    result = await observed.check_claims(text, observed.merge_pool([]), [tmp_path])
    assert len(calls) == observed.MAX_GIT_CHECKS
    assert all(c["code"] == "commit_not_found" for c in result["contradictions"])


@pytest.mark.asyncio
async def test_unknown_cancel_while_waiting_keeps_goal_and_spends_no_budget(client, auth, make_dir):
    """Track C(원인 불명 종료)와 Track D(대기 보호)가 after_run 에서 겹친다. 모든 미완료 요구가 외부 의존성 대기이면
    원인 미확인 취소 뒤에도 목표는 active 로 남고, 후속 Run·blocked·복구 예산 소모가 없어야 한다."""
    from goal_helpers import finish, new_run

    from ttakkari.models import RunStatus

    sess, h = await lead(client, auth, make_dir)
    sid = UUID(sess["id"])
    assert (await put(client, h, contract(reqs=[
        {"id": "R1", "text": "대기", "status": "waiting", "wait": {"target": "deploy:probe-a"}}]))).status_code == 200
    rid = await new_run(sid, events=[("error", {"message": "interrupted"})])
    await finish(rid, RunStatus.cancelled)  # 취소 요청 기록 없음: engine_interrupted(복구 가능)
    stored = await raw(sid)
    assert stored["state"]["status"] == "active"
    assert await queued(sid) == []
    assert not stored.get("transient_retries") and not stored.get("transient_total")
    assert stored.get("unchanged_continuations", 0) == 0


@pytest.mark.asyncio
async def test_unknown_cancel_with_independent_pending_requirement_still_continues_once(client, auth, make_dir):
    """한 요구가 외부 대기여도 독립 요구가 남아 있으면 원인 미확인 취소 뒤 제한적 복구(후속 1개)로 이어간다."""
    from goal_helpers import finish, new_run

    from ttakkari.models import RunStatus

    sess, h = await lead(client, auth, make_dir)
    sid = UUID(sess["id"])
    assert (await put(client, h, contract(reqs=[
        {"id": "R1", "text": "대기", "status": "waiting", "wait": {"target": "deploy:probe-b"}},
        {"id": "R2", "text": "독립 작업", "status": "pending"}]))).status_code == 200
    await finish(await new_run(sid), RunStatus.cancelled)
    assert len(await queued(sid)) == 1
    assert (await raw(sid))["state"]["status"] == "active"
