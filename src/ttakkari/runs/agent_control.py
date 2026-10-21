"""세션 소유권과 배포 세대를 넘어 동작하는 에이전트 전역/관리 중지 서비스 (ADR 0067).

- 일반 stop_worker 의 직속 소유권 제한은 그대로 두고, 사용자 지시를 받은 전역/관리 중지만 이 모듈을 쓴다.
  소유 오케스트레이터(AIP 기획 세션 등)와 배포 세대(draining 구세대)를 넘어 queued, running,
  한도 대기(queued + resume_after), 재시작 재개 대기(interrupted)를 모두 멈춘다.
- 다른 세대가 소유한 running Run 은 기존 `cancel` RuntimeCommand 로 소유 세대에 넘긴다.
  구세대도 이미 이해하는 명령이라 새 필드나 마이그레이션이 필요 없다.
- 취소된 작업자가 부모 오케스트레이터에게 새 보고 Run 을 만들어 작업을 되살리는 경로는 두 겹으로 막는다.
  1) Run 단위 억제 표식(RuntimeCommand kind=suppress_report, 어느 세대도 소비하지 않는 영속 표식)을
     취소 전에 남기고 `_enqueue_report` 가 이를 확인한다.
  2) 표식을 모르는 구세대가 보고 Run 을 이미 만들었다면 신세대의 dispatch 가 그 보고를 버리고,
     중지 마지막에도 범위 안의 대기 보고 Run 을 한 번 더 쓸어 취소한다.
- 회사(work)는 같은 워크스페이스, 개인(personal)은 개인 풀로 범위를 제한한다.
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
import uuid
from datetime import timedelta
from typing import Any

from sqlalchemy import and_, or_, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from ttakkari.audit import record
from ttakkari.db import sessionmaker
from ttakkari.models import (
    ChatSession,
    Run,
    RunOrigin,
    RunStatus,
    RuntimeCommand,
    RuntimeLease,
    SessionKind,
    utcnow,
)
from ttakkari.runs import events

log = logging.getLogger(__name__)

SUPPRESS_KIND = "suppress_report"
RESTART_INTERRUPTION = "서버 종료로 중단"
# 구세대가 표식을 모른 채 만든 보고 Run 을 알아보는 창. 이보다 오래된 표식은 무시한다.
SUPPRESS_WINDOW_SECONDS = 3600
_REPORT_CANCELLED = re.compile(r"\(worker_id=([0-9a-fA-F-]{36})\) 취소\.")


def scope_filters(pool: str, workspace_id: uuid.UUID | None = None) -> tuple:
    """work 는 같은 워크스페이스, personal 은 개인 풀로 한정. all 은 소유자 전용 시스템 비상 정지다."""
    if pool == "work":
        if workspace_id is None:
            raise ValueError("work 범위 중지에는 workspace_id 가 필요합니다.")
        return (ChatSession.pool == "work", ChatSession.workspace_id == workspace_id)
    if pool == "personal":
        return (ChatSession.pool == "personal",)
    if pool == "all":
        return ()
    raise ValueError(f"알 수 없는 중지 범위: {pool}")


def _suppress_key(run_id: uuid.UUID) -> str:
    return f"suppress-report:{run_id}"


async def mark_report_suppressed(db: AsyncSession, run: Run) -> None:
    """이 Run 의 취소는 부모에게 보고하지 않는다는 영속 표식. generation_id 가 없어 어느 세대도 명령으로 읽지 않는다."""
    await db.execute(
        insert(RuntimeCommand)
        .values(id=uuid.uuid4(), key=_suppress_key(run.id), kind=SUPPRESS_KIND, run_id=run.id,
                session_id=run.session_id, generation_id=None, created_at=utcnow(), acknowledged_at=utcnow())
        .on_conflict_do_nothing(index_elements=[RuntimeCommand.key])
    )


async def should_suppress_report(db: AsyncSession, run: Run) -> bool:
    if run.status != RunStatus.cancelled:
        return False
    return await db.scalar(
        select(RuntimeCommand.id).where(RuntimeCommand.key == _suppress_key(run.id)).limit(1)
    ) is not None


async def drop_suppressed_reports(db: AsyncSession, rows: list[Run]) -> list[Run]:
    """구세대가 표식을 모르고 만든 '취소 보고' Run 을 시작 전에 버린다. 남은 Run 을 돌려준다."""
    kept: list[Run] = []
    for run in rows:
        if run.status != RunStatus.queued or run.origin != RunOrigin.report:
            kept.append(run)
            continue
        m = _REPORT_CANCELLED.search(run.prompt or "")
        marker = None
        if m:
            try:
                worker_id = uuid.UUID(m.group(1))
            except ValueError:
                worker_id = None
            if worker_id is not None:
                marker = await db.scalar(
                    select(RuntimeCommand.id).where(
                        RuntimeCommand.kind == SUPPRESS_KIND,
                        RuntimeCommand.session_id == worker_id,
                        RuntimeCommand.created_at <= run.created_at,
                        RuntimeCommand.created_at >= run.created_at - timedelta(seconds=SUPPRESS_WINDOW_SECONDS),
                    ).limit(1)
                )
        if marker is None:
            kept.append(run)
            continue
        _cancel_in_place(db, run, "전역 중지로 취소된 작업의 보고라 버렸습니다.")
    return kept


def _cancel_in_place(db: AsyncSession, run: Run, reason: str) -> None:
    """프로세스가 없는 보고 Run 을 곧바로 취소 상태로 만든다(이 Run 은 다시 보고하지 않는다)."""
    run.status = RunStatus.cancelled
    run.finished_at = utcnow()
    run.resume_after = None
    run.error = reason
    events.append(db, run, "run.finished", {"status": RunStatus.cancelled, "error": reason})


async def _targets(db: AsyncSession, pool: str, workspace_id: uuid.UUID | None,
                   session_id: uuid.UUID | None) -> list[tuple[Run, ChatSession]]:
    pending_resume = and_(Run.status == RunStatus.interrupted, Run.error == RESTART_INTERRUPTION)
    stmt = (
        select(Run, ChatSession)
        .join(ChatSession, ChatSession.id == Run.session_id)
        .where(or_(Run.status.in_([RunStatus.queued, RunStatus.running]), pending_resume),
               *scope_filters(pool, workspace_id))
        .order_by(Run.created_at)
        .execution_options(populate_existing=True)
    )
    if session_id is not None:
        stmt = stmt.where(or_(Run.session_id == session_id, ChatSession.parent_id == session_id))
    return [(r, s) for r, s in (await db.execute(stmt)).all()]


async def stop_all_agents(
    db: AsyncSession,
    *,
    pool: str = "personal",
    workspace_id: uuid.UUID | None = None,
    session_id: uuid.UUID | None = None,
    reason: str = "사용자 전역 중지 지시",
    actor_id: str | None = None,
    wait_timeout: float = 3.0,
) -> dict[str, Any]:
    """범위 안의 활성·대기 Run 을 소유 세션·세대와 무관하게 멈추고, 멈춘 것과 못 멈춘 것을 이유와 함께 돌려준다.

    순서가 중요하다. 대기 중인 Run 부터 지워야 실행 중인 Run 이 끝나는 순간 다음 Run 이 시작되지 않는다.
    실행 중인 Run 은 로컬 프로세스면 직접 종료하고, 다른 세대 소유면 그 세대에 cancel 명령을 보내
    소유 세대가 자기 하트비트 주기(runtime_poll_seconds)에 집행한다. 세대가 이미 사라져 lease 도 프로세스도
    없는 running Run 은 소유자가 없으므로 DB 에서 취소로 마감한다.
    """
    from ttakkari.runs.manager import manager

    reason = reason[:1000]
    started = time.monotonic()
    rows = await _targets(db, pool, workspace_id, session_id)
    result: dict[str, Any] = {"stopped": [], "remaining": [], "unstopped": [], "total": len(rows)}
    if not rows:
        await _audit(db, pool, session_id, actor_id, reason, result)
        return result

    for run, sess in rows:
        # 취소 전에 표식을 남긴다. 구세대가 취소를 마감해도 새 보고가 새 작업을 만들지 못하게 한다.
        if sess.kind == SessionKind.worker and sess.parent_id is not None:
            await mark_report_suppressed(db, run)
    await db.commit()

    owned: dict[uuid.UUID, bool] = {}
    leases: dict[uuid.UUID, uuid.UUID] = {}
    for run, _ in rows:
        owned[run.id] = await manager.is_owned(run.id)
        if lease := await db.get(RuntimeLease, run.id):
            leases[run.id] = lease.generation_id

    # 1단계: 프로세스가 없는 대기 Run(queued, 한도 대기, 재개 대기)
    for run, _ in rows:
        if owned[run.id] or run.status == RunStatus.running:
            continue
        run.error = reason
        run.resume_after = None  # 한도 대기 타이머가 취소된 Run 을 다시 깨우지 못하게 한다
        if run.status == RunStatus.interrupted:
            await manager._finish(db, run, RunStatus.cancelled, error=reason)
        else:
            await manager.cancel_queued(db, run)
        await db.commit()

    # 2단계: 실행 중인 Run
    for run, _ in rows:
        if run.status != RunStatus.running and not owned[run.id]:
            continue
        if not owned[run.id]:
            # lease 도 프로세스도 없는 고아 running Run. 소유자가 없어 누구도 마감하지 않는다.
            await db.refresh(run)
            if run.status == RunStatus.running:
                await manager._finish(db, run, RunStatus.cancelled, error=f"{reason} (소유 프로세스 없음)")
            continue
        try:
            if not await manager.cancel(run.id):
                result["unstopped"].append({"run_id": str(run.id), "session_id": str(run.session_id),
                                            "reason": "취소 요청을 전달하지 못했습니다."})
        except Exception as exc:
            log.exception("stop-all: cancel failed for run %s", run.id)
            result["unstopped"].append({"run_id": str(run.id), "session_id": str(run.session_id),
                                        "reason": f"취소 요청 실패: {exc}"})

    ids = [r.id for r, _ in rows]
    active = await _wait_inactive(ids, wait_timeout)

    # 구세대가 표식 없이 만들었을 수 있는 대기 보고 Run 을 쓸어 취소한다.
    swept = await _sweep_reports(pool, workspace_id, session_id, reason)

    reported = {u["run_id"] for u in result["unstopped"]}
    for run, _ in rows:
        rid = str(run.id)
        if run.id not in active:
            result["stopped"].append(rid)
            continue
        result["remaining"].append(rid)
        if rid in reported:
            continue
        gen = leases.get(run.id)
        why = (f"세대 {gen} 에 cancel 명령을 전달했고 소유 세대의 집행을 기다리는 중입니다. 해당 세대가 종료하면 취소로 마감됩니다."
               if gen and not _is_local(manager, run.id) else
               "종료 신호를 보냈지만 제한 시간 안에 프로세스가 끝나지 않았습니다.")
        result["unstopped"].append({"run_id": rid, "session_id": str(run.session_id), "reason": why})
    result["swept_reports"] = swept
    result["elapsed_seconds"] = round(time.monotonic() - started, 2)
    await _audit(db, pool, session_id, actor_id, reason, result)
    return result


def _is_local(manager, run_id: uuid.UUID) -> bool:
    return run_id in manager._handles


async def _wait_inactive(ids: list[uuid.UUID], timeout: float) -> set[uuid.UUID]:
    deadline = time.monotonic() + timeout
    while True:
        async with sessionmaker()() as check:
            active = set(await check.scalars(select(Run.id).where(
                Run.id.in_(ids), Run.status.in_([RunStatus.queued, RunStatus.running]))))
        if not active or time.monotonic() >= deadline:
            return active
        await asyncio.sleep(0.1)


async def _sweep_reports(pool: str, workspace_id: uuid.UUID | None, session_id: uuid.UUID | None,
                         reason: str) -> int:
    """중지 중에 생긴 대기 보고 Run(부모 오케스트레이터를 깨워 새 작업을 만드는 경로)을 취소한다."""
    async with sessionmaker()() as db:
        stmt = (
            select(Run)
            .join(ChatSession, ChatSession.id == Run.session_id)
            .where(Run.status == RunStatus.queued, Run.origin == RunOrigin.report,
                   ~select(RuntimeLease.run_id).where(RuntimeLease.run_id == Run.id).exists(),
                   *scope_filters(pool, workspace_id))
            .with_for_update(skip_locked=True)
        )
        if session_id is not None:
            stmt = stmt.where(or_(Run.session_id == session_id, ChatSession.parent_id == session_id))
        reports = list(await db.scalars(stmt))
        for run in reports:
            _cancel_in_place(db, run, f"전역 중지로 보고 취소: {reason}")
        await db.commit()
        return len(reports)


async def _audit(db: AsyncSession, pool: str, session_id: uuid.UUID | None, actor_id: str | None,
                 reason: str, result: dict[str, Any]) -> None:
    record(
        db,
        actor="agent" if actor_id else "system",
        action="agent_control.stop_all",
        target_type="session" if session_id else "pool",
        target_id=session_id or pool,
        detail={"pool": pool, "actor_session": actor_id, "reason": reason, "total": result["total"],
                "stopped": len(result["stopped"]), "remaining": len(result["remaining"]),
                "unstopped": result["unstopped"][:50]},
    )
    await db.commit()
