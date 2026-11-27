"""기획 세션이 자기에게 건 예약 메시지(schedule_action send_message)는 목표 자동 이어가기의 소유자다."""
from __future__ import annotations

from datetime import timedelta
from uuid import UUID

import pytest
from goal_helpers import (
    continuations,
    contract,
    finish,
    lead,
    make_worker,
    new_run,
    put,
    queued,
    raw,
)
from sqlalchemy import select

from ttakkari.db import sessionmaker
from ttakkari.models import Run, RunStatus, ScheduledAction, utcnow
from ttakkari.orchestrator import work
from ttakkari.schedules import service


@pytest.fixture(autouse=True)
def no_dispatch(monkeypatch):
    from ttakkari.runs.manager import manager

    async def _noop(*args, **kwargs):
        return None
    monkeypatch.setattr(manager, "dispatch", _noop)


async def _schedule(sid, *, hours=1.0, status="pending", by="self", **extra) -> UUID:
    """send_message 예약. by="self" 는 기획이 자기 세션에 건 예약(생성 API 가 찍는 표식), None 은 사용자·다른 세션이 건 개인 예약."""
    payload = {"session_id": str(sid), "message": "1시간 뒤 확인"}
    if by is not None:
        payload[work.SELF_SCHEDULE_MARK] = str(sid if by == "self" else by)
    async with sessionmaker()() as db:
        item = ScheduledAction(action_type="send_message", payload=payload,
                               status=status, due_at=utcnow() + timedelta(hours=hours), **extra)
        db.add(item)
        await db.commit()
        return item.id


async def _active_goal(client, auth, make_dir):
    sess, h = await lead(client, auth, make_dir)
    sid = UUID(sess["id"])
    assert (await put(client, h, contract())).status_code == 200
    return sid, h


async def _end_turn(sid) -> None:
    await finish(await new_run(sid), RunStatus.succeeded, result_text="예약을 걸고 턴을 끝낸다")


async def _summary(sid) -> dict:
    async with sessionmaker()() as db:
        return await work.summary(db, sid)


async def test_pending_self_schedule_owns_the_next_step_and_shows_waiting(client, auth, make_dir):
    sid, _ = await _active_goal(client, auth, make_dir)
    schedule_id = await _schedule(sid, hours=1)
    await _end_turn(sid)
    assert continuations(await queued(sid)) == []  # 예약이 있는데 쓸모없는 후속 Run 을 만들지 않는다.
    stored = await raw(sid)
    assert stored.get("unchanged_continuations", 0) == 0  # 무진전 예산도 쓰지 않았다.
    summary = await _summary(sid)
    async with sessionmaker()() as db:
        due = (await db.get(ScheduledAction, schedule_id)).due_at
    assert summary["phase"] == "waiting" and summary["waiting_until"] == due


async def test_leased_self_schedule_is_an_owner_even_when_due_far_away(client, auth, make_dir):
    sid, _ = await _active_goal(client, auth, make_dir)
    await _schedule(sid, hours=-0.1, status="leased", owner_id="poller", attempts=1)
    await _end_turn(sid)
    assert continuations(await queued(sid)) == []


async def test_schedule_beyond_the_horizon_does_not_hold_the_goal_forever(client, auth, make_dir):
    sid, _ = await _active_goal(client, auth, make_dir)
    await _schedule(sid, hours=work.SELF_SCHEDULE_HORIZON.total_seconds() / 3600 + 6)
    await _end_turn(sid)
    assert len(continuations(await queued(sid))) == 1  # 24시간 넘게 먼 예약은 소유자로 보지 않는다.


async def test_personal_schedules_are_not_owners_and_their_loss_revives_nothing(client, auth, make_dir):
    sid, _ = await _active_goal(client, auth, make_dir)
    other, _ = await lead(client, auth, make_dir)
    personal = await _schedule(sid, hours=1, by=None)  # 사용자가 건 개인 예약
    from_other = await _schedule(sid, hours=1, by=other["id"])  # 다른 세션이 이 세션에 건 예약
    await _end_turn(sid)
    assert len(continuations(await queued(sid))) == 1  # 개인 예약은 후속을 막지 못한다.
    async with sessionmaker()() as db:
        for run in continuations(await queued(sid)):
            (await db.get(Run, run.id)).status = RunStatus.cancelled
        await db.commit()
    async with sessionmaker()() as db:
        assert await service.cancel_schedule(db, personal) and await service.cancel_schedule(db, from_other)
    assert continuations(await queued(sid)) == []  # 개인 예약이 사라져도 목표 후속을 세우지 않는다.


async def test_schedule_api_stamps_only_self_schedules_and_ignores_client_marks(client, auth, make_dir):
    sess, h = await lead(client, auth, make_dir)
    other, _ = await lead(client, auth, make_dir)
    sid = sess["id"]
    mine = await client.post("/api/orchestrator/schedules", headers=h, json={
        "action_type": "send_message", "due_at": "+1h", "payload": {"session_id": sid, "message": "m", work.SELF_SCHEDULE_MARK: other["id"]}})
    assert mine.status_code == 201 and mine.json()["payload"][work.SELF_SCHEDULE_MARK] == sid
    to_other = await client.post("/api/orchestrator/schedules", headers=h, json={
        "action_type": "send_message", "due_at": "+1h", "payload": {"session_id": other["id"], "message": "m", work.SELF_SCHEDULE_MARK: other["id"]}})
    assert to_other.status_code == 201 and work.SELF_SCHEDULE_MARK not in to_other.json()["payload"]


async def test_schedule_for_another_session_and_finished_schedules_are_not_owners(client, auth, make_dir):
    sid, _ = await _active_goal(client, auth, make_dir)
    other, _ = await lead(client, auth, make_dir)
    await _schedule(UUID(other["id"]), hours=1)
    for status in ("completed", "cancelled", "failed", "expired"):
        await _schedule(sid, hours=1, status=status)
    await _end_turn(sid)
    assert len(continuations(await queued(sid))) == 1


async def test_cancelled_self_schedule_restores_the_automatic_continuation(client, auth, make_dir):
    sid, _ = await _active_goal(client, auth, make_dir)
    schedule_id = await _schedule(sid, hours=1)
    await _end_turn(sid)
    assert continuations(await queued(sid)) == []
    async with sessionmaker()() as db:
        assert await service.cancel_schedule(db, schedule_id) is True
    runs = continuations(await queued(sid))
    assert len(runs) == 1 and runs[0].usage["work_continuation"]["schedule_id"] == str(schedule_id)
    assert "cancelled" in runs[0].prompt
    assert (await raw(sid))["unchanged_continuations"] == 1  # 무진전 예산 안에서만 이어간다.


async def test_failed_and_expired_schedules_restore_the_continuation(client, auth, make_dir):
    sid, _ = await _active_goal(client, auth, make_dir)
    failing = await _schedule(sid, hours=-0.1, status="leased", owner_id="p", attempts=3, max_attempts=3)
    await _end_turn(sid)
    assert continuations(await queued(sid)) == []
    async with sessionmaker()() as db:
        assert await service.fail_schedule(db, failing, "session not found", owner_id="p", attempt=3)
        await db.commit()
    assert len(continuations(await queued(sid))) == 1

    # 만료도 같다. 먼저 있던 후속은 지워 새로 세워지는지 본다.
    async with sessionmaker()() as db:
        for run in continuations(await queued(sid)):
            (await db.get(Run, run.id)).status = RunStatus.cancelled
        await db.commit()
    expiring = await _schedule(sid, hours=-0.1, status="leased", owner_id="p", attempts=1)
    async with sessionmaker()() as db:
        assert await service.complete_schedule(db, expiring, {"status": "expired"}, owner_id="p", attempt=1)
        await db.commit()
    assert len(continuations(await queued(sid))) == 1


async def test_lost_schedule_does_not_double_up_when_another_owner_exists(client, auth, make_dir):
    sid, _ = await _active_goal(client, auth, make_dir)
    first = await _schedule(sid, hours=1)
    await _schedule(sid, hours=2)  # 다른 예약이 남아 있다.
    await _end_turn(sid)
    async with sessionmaker()() as db:
        await service.cancel_schedule(db, first)
    assert continuations(await queued(sid)) == []
    # 실행 중인 작업자 Run 이 있어도 소유자다.
    third = await _schedule(sid, hours=1)
    worker = await make_worker(sid)
    await new_run(sid, session_id=worker, status=RunStatus.running)
    async with sessionmaker()() as db:
        await service.cancel_schedule(db, third)
    assert continuations(await queued(sid)) == []


async def test_schedule_loss_is_ignored_when_goal_is_not_active(client, auth, make_dir):
    sid, h = await _active_goal(client, auth, make_dir)
    schedule_id = await _schedule(sid, hours=1)
    cur = (await client.post("/api/orchestrator/work", headers=h, json={"operation": "get"})).json()
    paused = {**cur["state"], "status": "paused", "blocker": "사용자 중지 요청"}
    r = await client.post("/api/orchestrator/work", headers=h,
                          json={"operation": "checkpoint", "revision": cur["revision"], "state": paused})
    assert r.status_code == 200, r.text
    async with sessionmaker()() as db:
        assert await service.cancel_schedule(db, schedule_id) is True
    assert continuations(await queued(sid)) == []
    assert (await db_runs(sid)) == 0


async def db_runs(sid) -> int:
    async with sessionmaker()() as db:
        return len(list(await db.scalars(select(Run.id).where(Run.session_id == sid, Run.status == RunStatus.queued))))
