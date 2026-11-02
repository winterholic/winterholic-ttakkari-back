"""오래 조용한 작업자 Run 을 찾아 오케스트레이터에게 한 번 알린다.

Run 을 죽이지 않는다. 긴 테스트처럼 일부러 조용한 작업과 좀비를 이벤트만으로는 확실히 구분할 수 없어서,
판단은 알림을 받은 오케스트레이터(또는 사용자)에게 맡긴다. 실행 중인 Run 의 이벤트는 그 Run 을 도는 태스크만 쓰므로
(runs/events.py) 여기서는 Run 에 아무것도 쓰지 않고, 알림 여부만 AppSetting 에 남긴다."""

from __future__ import annotations

import logging
from datetime import timedelta

from sqlalchemy import select

from ttakkari import generation
from ttakkari.config import get_settings
from ttakkari.db import sessionmaker
from ttakkari.models import (
    AppSetting,
    ChatSession,
    Run,
    RunEvent,
    RunStatus,
    SessionKind,
    utcnow,
)
from ttakkari.runs.activity import TOOL_CALL_MAX_SECONDS

log = logging.getLogger("ttakkari.watchdog")
PROGRESS_TYPES = ("tool.result", "tool.call", "message", "run.diff", "checkpoint")


def stall_notice(sess: ChatSession, run: Run, age_s: float, tool_pending: bool) -> str:
    what = "도구 호출이 결과를 못 받은 채" if tool_pending else "진행 이벤트 없이"
    return (f"[작업 정지 의심] 작업 세션 '{sess.title}' (worker_id={sess.id})가 {what} {int(age_s // 60)}분째 실행 중이다 "
            f"(Run {run.id}). 긴 테스트처럼 정상일 수 있으니 get_worker 로 last_progress 를 확인하라. 멈춘 것이 확실하면 "
            "stop_worker 로 끊고 남은 지시를 좁혀 새로 보내거나, 다른 엔진·fresh_context 로 재배정하라. 정상이면 아무것도 하지 않는다.")


async def scan_stalled_workers() -> int:
    """조건을 넘긴 실행 중 작업자를 알린 개수. 이미 알린 Run 은 다시 알리지 않는다."""
    from ttakkari.runs.manager import _enqueue_report, manager
    s = get_settings()
    now = utcnow()
    notified: list = []
    async with sessionmaker()() as db:
        if manager.runtime:
            from ttakkari.generation import _control
            if generation.claim_owner(await _control(db)) != manager.runtime.id:
                return 0
        rows = (await db.execute(
            select(Run, ChatSession).join(ChatSession, ChatSession.id == Run.session_id)
            .where(Run.status == RunStatus.running, ChatSession.kind == SessionKind.worker,
                   ChatSession.parent_id.is_not(None), Run.started_at < now - timedelta(seconds=s.worker_stall_seconds))
        )).all()
        for run, sess in rows:
            last = await db.scalar(select(RunEvent).where(RunEvent.run_id == run.id, RunEvent.type.in_(PROGRESS_TYPES))
                                   .order_by(RunEvent.seq.desc()).limit(1))
            since = last.created_at if last is not None else run.started_at
            age = (now - since).total_seconds()
            tool_pending = last is not None and last.type == "tool.call"
            if age < (TOOL_CALL_MAX_SECONDS if tool_pending else s.worker_stall_seconds):
                continue
            key = f"stall:{run.id}"
            if await db.get(AppSetting, key) is not None:
                continue
            db.add(AppSetting(key=key, value={"age_s": int(age), "tool_pending": tool_pending}))
            parent_id = await _enqueue_report(db, sess, run, notice=stall_notice(sess, run, age, tool_pending))
            if parent_id is not None:
                notified.append(parent_id)
        await db.commit()
    for pid in set(notified):
        await manager.dispatch(pid)
    if notified:
        log.warning("stalled worker notices: %d", len(notified))
    return len(notified)
