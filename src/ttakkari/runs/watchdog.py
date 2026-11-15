"""오래 조용한 작업자 Run 과 시작도 못 한 채 오래 queued 인 Run 을 찾아 알린다.

Run 을 죽이지 않는다. 긴 테스트처럼 일부러 조용한 작업과 좀비를 이벤트만으로는 확실히 구분할 수 없어서,
판단은 알림을 받은 오케스트레이터(또는 사용자)에게 맡긴다. 실행 중인 Run 의 이벤트는 그 Run 을 도는 태스크만 쓰므로
(runs/events.py) 여기서는 Run 에 아무것도 쓰지 않고, 알림 여부만 AppSetting 에 남긴다.

알림은 두 단계다. 1단계는 부모 오케스트레이터에게 보고 Run 으로 알린다. 부모가 없는 Run(접수·기획·일반 대화)은
알릴 상위가 없으므로 곧바로 사용자 푸시다. 2단계는 `stall_escalate_seconds` 뒤에도 같은 Run 이 그대로면 사용자에게 푸시 한 번이다.
알림은 Run 당 단계마다 한 번이라 재시작해도 중복되지 않는다(AppSetting 키가 상태다)."""

from __future__ import annotations

import logging
import uuid
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
from ttakkari.push.service import PushKind, notify
from ttakkari.runs.activity import TOOL_CALL_MAX_SECONDS

log = logging.getLogger("ttakkari.watchdog")
PROGRESS_TYPES = ("tool.result", "tool.call", "message", "run.diff", "checkpoint")
STALL_KEY = "stall:"  # 실행 중 정지 의심. 뒤에 run id
QUEUED_KEY = "qstall:"  # 시작 지연 의심. 뒤에 run id
ESCALATED_LOOKBACK = timedelta(days=2)  # 이보다 오래된 알림 행은 에스컬레이션 대상으로 다시 보지 않는다.


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
            key = f"{STALL_KEY}{run.id}"
            if await db.get(AppSetting, key) is not None:
                continue
            db.add(AppSetting(key=key, value={"age_s": int(age), "tool_pending": tool_pending, "esc": False}))
            parent_id = await _enqueue_report(db, sess, run, notice=stall_notice(sess, run, age, tool_pending))
            if parent_id is not None:
                notified.append(parent_id)
        await db.commit()
    for pid in set(notified):
        await manager.dispatch(pid)
    if notified:
        log.warning("stalled worker notices: %d", len(notified))
    return len(notified)


def queued_notice(sess: ChatSession, run: Run, age_s: float) -> str:
    return (f"[작업 시작 지연 의심] 작업 세션 '{sess.title}' (worker_id={sess.id})의 Run {run.id} 가 시작하지 못하고 "
            f"{int(age_s // 60)}분째 queued 다. 한도 대기가 아니다. account_usage 로 사용 가능한 계정이 있는지, 실행 자리가 찼는지 확인하라. "
            "계정이 없으면 같은 지시를 기다리게 두지 말고 사용자에게 알려라. 원인이 풀리면 아무것도 하지 않아도 된다.")


async def _session_busy(db, session_id) -> bool:
    """같은 세션에 실행 중인 Run 이 있으면 뒤 Run 의 queued 는 정상 대기다."""
    return await db.scalar(select(Run.id).where(Run.session_id == session_id, Run.status == RunStatus.running).limit(1)) is not None


async def _queued_since(db, run: Run):
    """queued 나이의 기준 시각. 재개로 다시 queued 가 된 Run 은 마지막 이벤트가 기준이다."""
    last = await db.scalar(select(RunEvent.created_at).where(RunEvent.run_id == run.id)
                           .order_by(RunEvent.seq.desc()).limit(1))
    return max(run.created_at, last) if last is not None else run.created_at


async def _still_queued_stalled(db, run: Run, threshold_s: int, now) -> bool:
    if run.status != RunStatus.queued or run.resume_after is not None or await _session_busy(db, run.session_id):
        return False
    return (now - await _queued_since(db, run)).total_seconds() >= threshold_s


async def scan_queued_runs() -> int:
    """시작 지연을 알린 개수. 한도 대기(resume_after)와 같은 세션의 앞선 Run 을 기다리는 Run 은 제외한다.

    작업자는 부모 오케스트레이터에게 알리고, 부모가 없는 Run 은 사용자 푸시로 바로 올린다(알릴 상위가 없고,
    접수·기획이 계정 부족으로 서 있으면 보고 Run 을 받아 줄 쪽도 같이 서 있다)."""
    from ttakkari.runs.manager import _enqueue_report, manager
    s = get_settings()
    now = utcnow()
    notified: list = []
    pushes: list[tuple[Run, ChatSession, float]] = []
    async with sessionmaker()() as db:
        if manager.runtime:
            from ttakkari.generation import _control
            if generation.claim_owner(await _control(db)) != manager.runtime.id:
                return 0
        rows = (await db.execute(
            select(Run, ChatSession).join(ChatSession, ChatSession.id == Run.session_id)
            .where(Run.status == RunStatus.queued, Run.resume_after.is_(None),
                   Run.created_at < now - timedelta(seconds=s.queued_stall_seconds))
        )).all()
        for run, sess in rows:
            if await _session_busy(db, run.session_id):
                continue
            age = (now - await _queued_since(db, run)).total_seconds()
            if age < s.queued_stall_seconds:
                continue
            key = f"{QUEUED_KEY}{run.id}"
            if await db.get(AppSetting, key) is not None:
                continue
            has_parent = sess.kind == SessionKind.worker and sess.parent_id is not None
            db.add(AppSetting(key=key, value={"age_s": int(age), "esc": not has_parent}))
            if has_parent:
                parent_id = await _enqueue_report(db, sess, run, notice=queued_notice(sess, run, age))
                if parent_id is not None:
                    notified.append(parent_id)
            else:
                pushes.append((run, sess, age))
        await db.commit()
    for pid in set(notified):
        await manager.dispatch(pid)
    for run, sess, age in pushes:
        await _push_stall(run, sess, f"시작하지 못하고 {int(age // 60)}분째 대기 중이다. 사용 가능한 계정이나 실행 자리를 확인하라.")
    if notified or pushes:
        log.warning("queued stall notices: %d, pushes: %d", len(notified), len(pushes))
    return len(notified) + len(pushes)


async def _push_stall(run: Run, sess: ChatSession, detail: str) -> None:
    try:
        name = (sess.title or "새 세션")[:60]
        await notify(PushKind.stall, "작업 정지·대기 정체", f"{name} · {detail}", url=f"/chat/{sess.id}",
                     tag=f"{sess.id}-stall", target_id=run.id, dedupe_key=f"stall-esc:{run.id}")
    except Exception:
        log.exception("stall push failed for run %s", run.id)


async def escalate_stalls() -> int:
    """부모 알림 뒤에도 같은 Run 이 그대로면 사용자에게 푸시 한 번. `stall_escalate_seconds` 가 0 이면 끈다.

    해소된 Run(끝났거나 진행 이벤트가 다시 생겼거나 시작함)은 푸시 없이 닫는다. 다시 검사하지 않도록 esc 를 켠다."""
    from ttakkari.runs.manager import manager
    s = get_settings()
    if s.stall_escalate_seconds <= 0:
        return 0
    now = utcnow()
    pushes: list[tuple[Run, ChatSession, str]] = []
    async with sessionmaker()() as db:
        if manager.runtime:
            from ttakkari.generation import _control
            if generation.claim_owner(await _control(db)) != manager.runtime.id:
                return 0
        rows = list(await db.scalars(
            select(AppSetting).where(
                (AppSetting.key.like(f"{STALL_KEY}%") | AppSetting.key.like(f"{QUEUED_KEY}%")),
                AppSetting.updated_at < now - timedelta(seconds=s.stall_escalate_seconds),
                AppSetting.updated_at > now - ESCALATED_LOOKBACK)))
        for row in rows:
            if row.value.get("esc"):
                continue
            queued = row.key.startswith(QUEUED_KEY)
            try:
                run = await db.get(Run, uuid.UUID(row.key.split(":", 1)[1]))
            except ValueError:
                continue
            row.value = {**row.value, "esc": True}  # 같은 값 재할당으로는 JSON 변경이 감지되지 않아 새 dict 를 쓴다.
            if run is None:
                continue
            sess = await db.get(ChatSession, run.session_id)
            if sess is None:
                continue
            if queued:
                if await _still_queued_stalled(db, run, s.queued_stall_seconds, now):
                    pushes.append((run, sess, "알림 뒤에도 시작하지 못하고 queued 에 머물러 있다."))
                continue
            if run.status != RunStatus.running:
                continue
            last = await db.scalar(select(RunEvent).where(RunEvent.run_id == run.id, RunEvent.type.in_(PROGRESS_TYPES))
                                   .order_by(RunEvent.seq.desc()).limit(1))
            since = last.created_at if last is not None else run.started_at
            pending = last is not None and last.type == "tool.call"
            limit = TOOL_CALL_MAX_SECONDS if pending else s.worker_stall_seconds
            if since is not None and (now - since).total_seconds() >= limit:
                pushes.append((run, sess, f"알림 뒤에도 진행 이벤트가 {int((now - since).total_seconds() // 60)}분째 없다."))
        await db.commit()
    for run, sess, detail in pushes:
        await _push_stall(run, sess, detail)
    if pushes:
        log.warning("stall escalations: %d", len(pushes))
    return len(pushes)
