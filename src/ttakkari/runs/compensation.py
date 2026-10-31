"""Bounded compensation under the existing Run, lease and process identity fences."""
from __future__ import annotations

import logging
import re
from datetime import timedelta

from sqlalchemy import func, or_, select
from sqlalchemy.orm import aliased

from ttakkari.db import sessionmaker
from ttakkari.models import (
    AppSetting,
    ChatSession,
    Run,
    RunStatus,
    RuntimeCommand,
    RuntimeLease,
    utcnow,
)

log = logging.getLogger(__name__)
MAX_EXECUTION_RECOVERY_ATTEMPTS = 3
RESTART_ERRORS = {"서버 종료로 중단", "executor orphan reconciled after recorded process groups exited"}
RETRYABLE_MARKERS = ("queuepool", "connectionreseterror", "connection reset by peer", "connection refused",
                     "connection terminated", "server disconnected", "remoteprotocolerror", "readtimeout",
                     "connecttimeout", "temporarily unavailable", "overloaded", "시간 초과(",
                     "connection reached eof", "unexpected eof", "broken pipe",
                     "exit code -9", "exit code 137")


def retryable_execution_error(error: str) -> bool:
    text = error.lower()
    # Permission/model/tool/configuration failures require a correction, not a replay.
    if any(word in text for word in ("permission", "denied", "forbidden", "unauthorized", "sandbox",
                                    "unknown tool", "invalid model", "authentication", "비상 정지", "취소")):
        return False
    return (any(word in text for word in RETRYABLE_MARKERS)
            or bool(re.search(r"\b(?:http\s*)?(?:502|503|504)\b", text)))


async def restore_failure_contract(db, run) -> bool:
    from ttakkari.orchestrator.work import key
    row = await db.get(AppSetting, key(run.session_id))
    value = dict(row.value) if row else {}
    state = value.get("state") or {}
    if state.get("status") in {"paused", "complete"}:
        return False
    if state.get("status") == "blocked":
        # Only undo the backend's automatic block for this exact failed Run.
        if f"Run {run.id} failed:" not in state.get("blocker", ""):
            return False
        row.value = {**value, "revision": value.get("revision", 0) + 1,
                     "state": {**state, "status": "active", "blocker": ""}}
    return True


async def compensate_runs() -> dict:
    from ttakkari import executor_reconcile, generation
    from ttakkari.models import RuntimeGeneration, RuntimeProcessGroup
    from ttakkari.runs.manager import _inspect_run_process, manager

    async def runtime_owns_active_generation(db) -> dict | None:
        runtime = manager.runtime
        if runtime is None:
            return None
        if manager.remote or getattr(runtime, "draining", False):
            return None
        control = await generation._control(db)
        owner_id = generation.claim_owner(control)
        if owner_id != runtime.id:
            return None
        owner = await db.get(RuntimeGeneration, owner_id)
        if owner is None or owner.state != "active":
            return None
        return {"generation_id": owner_id, "revision": owner.revision, "epoch": control.epoch}

    # Standby/draining processes must never recover work on behalf of an owner.
    async with sessionmaker()() as db:
        claim_fence = await runtime_owns_active_generation(db)
        if claim_fence is None:
            return {"requeued": 0, "orphans": 0}
        await db.commit()
    reconciled = await executor_reconcile.reconcile_orphans(
        apply=True, expected_claim_owner_fence=claim_fence or None)
    count = 0
    waiting = []
    async with sessionmaker()() as db:
        if await runtime_owns_active_generation(db) is None:
            return {"requeued": 0, "orphans": reconciled["reconciled"]}
        newer = aliased(Run)
        rows = list(await db.scalars(select(Run).join(ChatSession, ChatSession.id == Run.session_id).where(
            ChatSession.archived_at.is_(None),
            ChatSession.kind != "desk",
            Run.status.in_((RunStatus.failed, RunStatus.interrupted, RunStatus.running)),
            func.coalesce(Run.usage["automatic_recovery"]["attempts"].as_integer(), 0)
                < MAX_EXECUTION_RECOVERY_ATTEMPTS,
            or_(Run.status == RunStatus.running, Run.error.in_(RESTART_ERRORS),
                Run.error.like("사용량 한도 또는 모델 혼잡 때문에%"),
                *(Run.error.ilike("%" + marker + "%") for marker in RETRYABLE_MARKERS),
                Run.error.op("~*")(r"\m(?:http\s*)?(?:502|503|504)\M")),
            ~select(newer.id).where(newer.session_id == Run.session_id,
                                   or_(newer.created_at > Run.created_at,
                                       (newer.created_at == Run.created_at) & (newer.id > Run.id))).exists(),
            ~select(RuntimeLease.run_id).where(RuntimeLease.run_id == Run.id).exists(),
            ~select(RuntimeCommand.id).where(RuntimeCommand.run_id == Run.id,
                                             RuntimeCommand.kind == "cancel").exists(),
        ).order_by(Run.created_at.desc()).limit(100).with_for_update(of=Run, skip_locked=True)))
        for run in rows:
            if run.id in manager._handles:
                continue
            sess = await db.get(ChatSession, run.session_id)
            if sess.parent_id:
                parent = await db.get(ChatSession, sess.parent_id)
                if parent is None or parent.archived_at is not None:
                    continue
                from ttakkari.orchestrator.work import key
                parent_contract = await db.get(AppSetting, key(sess.parent_id))
                parent_state = (parent_contract.value or {}).get("state") if parent_contract else None
                if parent_state and parent_state.get("status") in {"complete", "paused"}:
                    continue
            groups = list(await db.scalars(select(RuntimeProcessGroup).where(RuntimeProcessGroup.run_id == run.id)))
            if any(executor_reconcile._group_state(group)[0] != "absent" for group in groups):
                continue
            if run.pid and _inspect_run_process(run.pid) != "absent":
                continue
            error = run.error or ""
            if run.status == RunStatus.running:
                # A missing handle is not death. Require no lease, no live process/group,
                # and an old heartbeat; this also covers a crash before child launch.
                heartbeat = run.heartbeat_at or run.started_at or run.created_at
                if heartbeat > utcnow() - timedelta(minutes=2):
                    continue
                error = "서버 종료로 중단"
            quota_wait = error.startswith("사용량 한도 또는 모델 혼잡 때문에")
            if quota_wait:
                from ttakkari.runs.manager import _available_profiles
                sess = await db.get(ChatSession, run.session_id)
                ready, _ = await _available_profiles(db, sess, run, False)
                if not ready:
                    continue
            if error not in RESTART_ERRORS and not quota_wait and not retryable_execution_error(error):
                continue
            if not await restore_failure_contract(db, run):
                continue
            if await manager._retry_execution_failure(db, run, error, source="compensation",
                                                      interrupted=error in RESTART_ERRORS or quota_wait, persist=False):
                count += 1
                waiting.append((run.id, run.session_id, run.resume_after.timestamp()))
        if count and manager.runtime:
            await generation.wake(db)
        await db.commit()
    for run_id, session_id, until in waiting:
        manager._arm(run_id, session_id, until)
    if count:
        log.warning("Run compensation requeued %d runs after ownership/process checks", count)
    return {"requeued": count, "orphans": reconciled["reconciled"]}
