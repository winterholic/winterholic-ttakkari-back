"""세션 소유권과 배포 세대를 넘어 동작하는 에이전트 전역/관리 중지 서비스 (ADR 0067).

- 일반 stop_worker 의 직속 소유권 제한은 그대로 두고, 사용자 지시를 받은 전역/관리 중지만 이 모듈을 쓴다.
  소유 오케스트레이터(AIP 기획 세션 등)와 배포 세대(draining 구세대)를 넘어 queued, running,
  한도 대기(queued + resume_after), 재시작 재개 대기(interrupted)를 모두 멈춘다.
- 다른 세대가 소유한 running Run 은 기존 `cancel` RuntimeCommand 로 소유 세대에 넘긴다.
  이 명령은 구세대도 이미 이해하므로 교차 세대 전달 자체는 새 프로토콜 필드가 필요 없다.
- 취소된 작업자가 부모 오케스트레이터에게 새 보고 Run 을 만들어 작업을 되살리는 경로는 두 겹으로 막는다.
  1) Run 단위 억제 표식(RuntimeCommand kind=suppress_report, 어느 세대도 소비하지 않는 영속 표식)을
     취소 전에 남기고 `_enqueue_report` 가 이를 확인한다.
  2) 원본 ID가 있는 보고만 그 Run 의 표식과 정확히 매칭해 제거한다. 원본 ID가 없는 구세대 취소 보고는
     보존하고 자동 dispatch 를 보류하며, 중지 결과에도 미완료로 드러낸다.
- 회사(work)는 같은 워크스페이스, 개인(personal)은 개인 풀로 범위를 제한한다.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import re
import signal
import subprocess
import time
import uuid
from typing import Any

from sqlalchemy import and_, or_, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from ttakkari.audit import record
from ttakkari.db import sessionmaker
from ttakkari.models import (
    AgentStopFence,
    AgentStopReceipt,
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
_REPORT_CANCELLED = re.compile(r"\(worker_id=([0-9a-fA-F-]{36})\) 취소\.")
_REPORT_SOURCE = re.compile(r"stop_source_run_id=([0-9a-fA-F-]{36})")
_REPORT_START = re.compile(r"(?m)(?=^\[작업 보고\])")


def fence_keys(pool: str, workspace_id: uuid.UUID | None, session_id: uuid.UUID | None = None) -> list[str]:
    """Return stable locks shared by stop, enqueue and dispatch for the same authorization scope."""
    keys: list[str] = []
    if pool == "all":
        keys.append("pool:all")
    else:
        keys.append(f"pool:{pool}")
        if pool == "work":
            if workspace_id is None:
                raise ValueError("work scope requires workspace_id")
            keys.append(f"workspace:{workspace_id}")
    if session_id is not None:
        keys.append(f"session:{session_id}")
    return sorted(set(keys))


def run_fence_is_current(run_epochs: dict[str, int] | None, current_epochs: dict[str, int]) -> bool:
    """Legacy rows are valid before any stop; after a stop only exact epoch snapshots may dispatch."""
    if not any(current_epochs.values()):
        return True
    return (run_epochs or {}) == current_epochs


async def lock_fences(db: AsyncSession, keys: list[str]) -> list[AgentStopFence]:
    """Upsert then row-lock all relevant fence rows in stable order inside the caller transaction."""
    rows = []
    for key in sorted(set(keys)):
        statement = insert(AgentStopFence).values(scope_key=key, epoch=0, paused=False, stopping=False,
                                                   updated_at=utcnow())
        await db.execute(statement.on_conflict_do_nothing(index_elements=[AgentStopFence.scope_key]))
        row = await db.scalar(select(AgentStopFence).where(AgentStopFence.scope_key == key).with_for_update())
        rows.append(row)
    return rows


async def dispatch_is_fenced(db: AsyncSession, session_id: uuid.UUID) -> bool:
    sess = await db.get(ChatSession, session_id)
    if sess is None:
        return True
    keys = fence_keys(sess.pool, sess.workspace_id, session_id) + ["pool:all"]
    rows = await lock_fences(db, keys)
    return any(row.paused for row in rows)


async def _process_identity(pid: int) -> tuple[int, int, str] | None:
    """Read only a process identity tuple; command text stays in memory and is never logged."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "ps", "-o", "lstart=", "-o", "pgid=", "-o", "command=", "-p", str(pid),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        )
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=3)
        if proc.returncode != 0:
            return None
        fields = out.decode(errors="replace").strip().split(None, 7)
        if len(fields) < 8:
            return None
        started = time.mktime(time.strptime(" ".join(fields[:6]), "%a %b %d %H:%M:%S %Y"))
        return int(started), int(fields[6]), fields[7]
    except (OSError, ValueError, TimeoutError, subprocess.SubprocessError):
        return None


async def _terminate_identified_run(run: Run, manager) -> tuple[bool, str]:
    """Signal only a process group whose PID birth time, group ID and engine command match this Run."""
    if run.pid is None or run.pid_started_at is None:
        return False, "lease가 없고 PID 소유·생존 식별 정보가 없습니다."
    identity = await _process_identity(run.pid)
    if identity is None:
        return False, "프로세스 식별 조회가 불가능해 안전하게 종료하지 않았습니다."
    started, pgid, command = identity
    if started != int(run.pid_started_at) or pgid != run.pid or not _looks_like_run_engine(run, command):
        return False, "PID 재사용 또는 소유 불일치를 배제하지 못해 안전하게 종료하지 않았습니다."
    try:
        os.killpg(run.pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError, OSError) as exc:
        return False, f"확인된 프로세스 그룹에 종료 신호를 보내지 못했습니다: {exc}"
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        await asyncio.sleep(0.1)
        current = await _process_identity(run.pid)
        if current is not None and current[0] == started and current[1] == pgid:
            continue
        try:
            os.killpg(run.pid, 0)
        except ProcessLookupError:
            return True, ""
        except PermissionError:
            continue
        except OSError:
            continue
    return False, "종료 신호 뒤에도 확인된 프로세스가 살아 있습니다."


def _looks_like_run_engine(run: Run, command: str) -> bool:
    cmd = command.lower()
    tokens = {
        "claude": ("claude",), "codex": ("codex",), "antigravity": ("agy", "antigravity"),
        "muse": ("muse",), "qwen-local": ("qwen",), "auto": ("claude", "codex", "agy", "antigravity", "muse", "qwen"),
    }.get(run.engine, ())
    return any(token in cmd for token in tokens)


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


def report_chunks(prompt: str | None) -> list[str]:
    """Split concatenated queued reports without splitting their content paragraphs."""
    text = prompt or ""
    chunks = [chunk for chunk in _REPORT_START.split(text) if chunk.strip()]
    return chunks or ([text] if text else [])


def has_unlinked_legacy_cancel_report(prompt: str | None) -> bool:
    """Legacy cancellation reports without source IDs are ambiguous and must not auto-dispatch."""
    return any(_REPORT_CANCELLED.search(chunk) and not _REPORT_SOURCE.search(chunk)
               for chunk in report_chunks(prompt))


def _record_target_outcome(result: dict[str, Any], run_id: uuid.UUID, session_id: uuid.UUID,
                           active: set[uuid.UUID], reported: set[str]) -> None:
    """Do not report a preserved queued Run as stopped just because it has no process."""
    rid = str(run_id)
    if run_id not in active:
        result["stopped"].append(rid)
        return
    result["remaining"].append(rid)
    if rid not in reported:
        result["unstopped"].append({"run_id": rid, "session_id": str(session_id),
                                    "reason": "종료를 확인하지 못해 Run이 아직 활성 상태입니다."})


async def drop_suppressed_reports(db: AsyncSession, rows: list[Run]) -> list[Run]:
    """Remove only report chunks proven to refer to stopped Run IDs; preserve unknown legacy reports."""
    kept: list[Run] = []
    for run in rows:
        if run.status != RunStatus.queued or run.origin != RunOrigin.report:
            kept.append(run)
            continue
        surviving: list[str] = []
        removed = False
        for chunk in report_chunks(run.prompt):
            source = _REPORT_SOURCE.search(chunk)
            marker = None
            if source:
                try:
                    source_run_id = uuid.UUID(source.group(1))
                except ValueError:
                    source_run_id = None
                if source_run_id is not None:
                    marker = await db.scalar(select(RuntimeCommand.id).where(
                        RuntimeCommand.key == _suppress_key(source_run_id)).limit(1))
            if marker is None:
                # Old generations persist no source Run ID or report/source relation.
                # Worker/time proximity cannot prove which cancellation this is.
                surviving.append(chunk)
                continue
            removed = True
        if not removed:
            kept.append(run)
            continue
        if surviving:
            run.prompt = "\n\n".join(surviving)
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


async def _stop_all_agents_impl(
    db: AsyncSession,
    *,
    pool: str = "personal",
    workspace_id: uuid.UUID | None = None,
    session_id: uuid.UUID | None = None,
    reason: str = "사용자 전역 중지 지시",
    actor_id: str | None = None,
    wait_timeout: float = 3.0,
    receipt_id: uuid.UUID | None = None,
    actor_session_id: uuid.UUID | None = None,
    claim_existing_receipt: bool = False,
) -> dict[str, Any]:
    """범위 안의 활성·대기 Run 을 소유 세션·세대와 무관하게 멈추고, 멈춘 것과 못 멈춘 것을 이유와 함께 돌려준다.

    순서가 중요하다. 대기 중인 Run 부터 지워야 실행 중인 Run 이 끝나는 순간 다음 Run 이 시작되지 않는다.
    실행 중인 Run 은 로컬 프로세스면 직접 종료하고, 다른 세대 소유면 그 세대에 cancel 명령을 보내
    소유 세대가 자기 하트비트 주기(runtime_poll_seconds)에 집행한다. 세대가 이미 사라져 lease 도 프로세스도
    없는 running Run 도 PID 소유·생존 확인 없이 DB 에서 취소로 마감하지 않는다.
    """
    from ttakkari.runs.manager import manager

    reason = reason[:1000]
    started = time.monotonic()
    keys = [f"session:{session_id}"] if session_id is not None else fence_keys(pool, workspace_id)
    receipt_id = receipt_id or uuid.uuid4()
    if actor_session_id is not None:
        existing = await db.scalar(select(AgentStopReceipt).where(
            AgentStopReceipt.id == receipt_id).with_for_update())
        if existing is not None:
            if existing.actor_session_id != actor_session_id:
                raise ValueError("stop receipt request ID belongs to another caller")
            if existing.result is not None:
                return {**existing.result, "receipt_id": str(receipt_id)}
            if existing.status == "running" or (existing.status == "claiming" and not claim_existing_receipt):
                await db.rollback()
                return {"status": existing.status, "receipt_id": str(receipt_id), "stopped": [],
                        "remaining": [], "unstopped": [], "total": 0}
            existing.status = "running"
            existing.pool = pool
            existing.workspace_id = workspace_id
            existing.session_id = session_id
            existing.updated_at = utcnow()
        else:
            db.add(AgentStopReceipt(id=receipt_id, actor_session_id=actor_session_id, pool=pool,
                                    workspace_id=workspace_id, session_id=session_id, status="running"))
    fences = await lock_fences(db, keys)
    for fence in fences:
        fence.paused = True
        fence.stopping = True
        fence.epoch += 1
        fence.request_id = receipt_id
        fence.updated_at = utcnow()
    await db.commit()  # enqueue/dispatch observe this fence before the stop snapshot

    rows = await _targets(db, pool, workspace_id, session_id)
    result: dict[str, Any] = {"status": "stopping", "stopped": [], "remaining": [], "unstopped": [],
                              "total": len(rows)}
    if actor_session_id is not None:
        result["receipt_id"] = str(receipt_id)
    if not rows:
        result["status"] = "stopped"
        result["message"] = "중지 대상 Run이 없습니다."
        result["swept_reports"] = 0
        result["elapsed_seconds"] = round(time.monotonic() - started, 2)
        await _audit(db, pool, session_id, actor_id, reason, result)
        await _complete_stop(db, keys, receipt_id, result, actor_session_id)
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
        if run.origin == RunOrigin.report and has_unlinked_legacy_cancel_report(run.prompt):
            result["unstopped"].append({"run_id": str(run.id), "session_id": str(run.session_id),
                                        "reason": "원본 Run ID가 없는 구세대 취소 보고라 보존하며 자동 dispatch를 보류합니다."})
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
            if run.status != RunStatus.running:
                continue
            terminated, why = await _terminate_identified_run(run, manager)
            await db.refresh(run)
            if terminated and run.status == RunStatus.running:
                await manager._finish(db, run, RunStatus.cancelled, error=reason)
                await db.commit()
            elif not terminated:
                result["unstopped"].append({"run_id": str(run.id), "session_id": str(run.session_id),
                                            "reason": why})
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

    # 신세대가 집행하지 못한 구세대 보고 중 식별 가능한 대기 보고를 쓸어 취소한다.
    swept = await _sweep_reports(pool, workspace_id, session_id, reason)

    reported = {u["run_id"] for u in result["unstopped"]}
    for run, _ in rows:
        rid = str(run.id)
        _record_target_outcome(result, run.id, run.session_id, active, reported)
        if run.id in active and rid not in reported:
            gen = leases.get(run.id)
            result["unstopped"][-1]["reason"] = (
                f"세대 {gen} 에 cancel 명령을 전달했고 소유 세대의 집행을 기다리는 중입니다. 해당 세대가 종료하면 취소로 마감됩니다."
                if gen and not _is_local(manager, run.id) else
                "종료 신호를 보냈지만 제한 시간 안에 프로세스가 끝나지 않았습니다."
            )
    result["swept_reports"] = swept
    result["elapsed_seconds"] = round(time.monotonic() - started, 2)
    result["status"] = "partial" if result["remaining"] or result["unstopped"] else "stopped"
    result["message"] = ("일부 Run이 남아 있습니다. remaining과 unstopped 사유를 확인하세요."
                         if result["status"] == "partial" else "대상 Run이 모두 중지 상태입니다.")
    await _audit(db, pool, session_id, actor_id, reason, result)
    await _complete_stop(db, keys, receipt_id, result, actor_session_id)
    return result


async def stop_all_agents(
    db: AsyncSession,
    *,
    pool: str = "personal",
    workspace_id: uuid.UUID | None = None,
    session_id: uuid.UUID | None = None,
    reason: str = "사용자 전역 중지 지시",
    actor_id: str | None = None,
    wait_timeout: float = 3.0,
    receipt_id: uuid.UUID | None = None,
    actor_session_id: uuid.UUID | None = None,
    claim_existing_receipt: bool = False,
) -> dict[str, Any]:
    rid = receipt_id or uuid.uuid4()
    try:
        return await _stop_all_agents_impl(
            db, pool=pool, workspace_id=workspace_id, session_id=session_id, reason=reason,
            actor_id=actor_id, wait_timeout=wait_timeout, receipt_id=rid,
            actor_session_id=actor_session_id, claim_existing_receipt=claim_existing_receipt,
        )
    except Exception as exc:
        log.exception("stop-all failed for request %s", rid)
        with contextlib.suppress(Exception):
            await db.rollback()
            keys = [f"session:{session_id}"] if session_id is not None else fence_keys(pool, workspace_id)
            failure = {"status": "failed", "stopped": [], "remaining": [],
                       "unstopped": [], "total": 0, "error": str(exc)[:1000]}
            if actor_session_id is not None:
                failure["receipt_id"] = str(rid)
            await _complete_stop(db, keys, rid, failure, actor_session_id)
        return {"status": "failed", "receipt_id": str(rid), "stopped": [], "remaining": [],
                "unstopped": [], "total": 0, "error": str(exc)[:1000]}


async def _complete_stop(db: AsyncSession, keys: list[str], receipt_id: uuid.UUID,
                         result: dict[str, Any], actor_session_id: uuid.UUID | None) -> None:
    for fence in await lock_fences(db, keys):
        fence.stopping = False
        fence.updated_at = utcnow()
    receipt = await db.get(AgentStopReceipt, receipt_id) if actor_session_id is not None else None
    if receipt is not None and receipt.actor_session_id == actor_session_id:
        receipt.result = {k: v for k, v in result.items() if k != "receipt_id"}
        receipt.status = result.get("status", "partial")
        receipt.updated_at = utcnow()
    await db.commit()


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
    """중지 중 생긴 식별 가능한 대기 보고를 취소한다; 출처 불명 legacy 취소 보고는 보존한다."""
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
        reports = [run for run in await db.scalars(stmt)
                   if not has_unlinked_legacy_cancel_report(run.prompt)]
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
                "status": result.get("status"),
                "stopped": len(result["stopped"]), "remaining": len(result["remaining"]),
                "unstopped": result["unstopped"][:50]},
    )
    await db.commit()
