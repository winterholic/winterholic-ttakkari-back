"""Fail-closed recovery for quarantined executor leases."""
from __future__ import annotations

import asyncio
import errno
import logging
import os
import signal
import uuid

import psutil
from sqlalchemy import delete, select, text
from sqlalchemy.exc import DBAPIError

from ttakkari import generation
from ttakkari.db import sessionmaker
from ttakkari.generation import _control
from ttakkari.models import (
    Run,
    RunStatus,
    RuntimeCommand,
    RuntimeGeneration,
    RuntimeLease,
    RuntimeProcessGroup,
    utcnow,
)


def disposition(*, owner_dead: bool | None, groups: list[tuple[str, bool]]) -> str:
    if owner_dead is None:
        return "unknown_owner"
    if not owner_dead:
        return "owner_alive"
    if not groups:
        return "missing_process_group_evidence"
    if any(not identity_ok or state == "unknown" for state, identity_ok in groups):
        return "unknown_process_group"
    if any(state == "present" for state, _ in groups):
        return "process_group_live"
    return "safe_to_release"


def _process_dead(pid: int, started_at: float) -> bool | None:
    try:
        process = psutil.Process(pid)
        actual_started_at = process.create_time()
    except psutil.NoSuchProcess:
        return True
    except (psutil.AccessDenied, OSError):
        return None
    return abs(actual_started_at - started_at) > 1.0


def _group_state(record: RuntimeProcessGroup) -> tuple[str, bool]:
    try:
        os.killpg(record.pgid, 0)
    except ProcessLookupError:
        return "absent", True
    except PermissionError:
        return "unknown", False
    except OSError as exc:
        if exc.errno == errno.ESRCH:
            return "absent", True
        return "unknown", False
    try:
        process = psutil.Process(record.pid)
        same_process = abs(process.create_time() - record.process_started_at) <= 1.0
        same_group = os.getpgid(record.pid) == record.pgid
    except psutil.NoSuchProcess:
        return "unknown", False
    except (psutil.AccessDenied, OSError, PermissionError):
        return "unknown", False
    return ("present", True) if same_process and same_group else ("unknown", False)


def _owner_gone(owner: RuntimeGeneration, entry: dict) -> bool:
    """소유 세대가 죽었거나, 호출한 이 프로세스 자신이 소유자(핸들만 잃은 경우)면 True."""
    if entry.get("owner_self") is True:
        return owner.pid == os.getpid() and owner.pid == entry.get("owner_pid")
    return _process_dead(owner.pid, owner.process_started_at) is True


def _group_fence_key(group: RuntimeProcessGroup) -> tuple:
    return (group.generation_id, group.lease_token, group.attempt, group.pid,
            group.process_started_at, group.pgid)


async def _stop_verified_group(record: RuntimeProcessGroup) -> bool:
    state, identity_ok = _group_state(record)
    if state == "absent":
        return True
    if state != "present" or not identity_ok:
        return False
    try:
        os.killpg(record.pgid, signal.SIGTERM)
    except (ProcessLookupError, OSError, PermissionError):
        return _group_state(record)[0] == "absent"
    for _ in range(50):
        await asyncio.sleep(0.1)
        state, identity_ok = _group_state(record)
        if state == "absent":
            return True
        if state != "present" or not identity_ok:
            return False
    # Escalate only while the original group leader still matches its recorded PID/start-time/PGID.
    state, identity_ok = _group_state(record)
    if state != "present" or not identity_ok:
        return False
    try:
        os.killpg(record.pgid, signal.SIGKILL)
    except (ProcessLookupError, OSError, PermissionError):
        return _group_state(record)[0] == "absent"
    for _ in range(50):
        await asyncio.sleep(0.1)
        state, identity_ok = _group_state(record)
        if state == "absent":
            return True
        if state != "present" or not identity_ok:
            return False
    return False


async def _read_orphans(run_id: uuid.UUID | None = None) -> list[dict]:
    async with sessionmaker()() as db:
        query = select(RuntimeLease).order_by(RuntimeLease.run_id)
        if run_id is not None:
            query = query.where(RuntimeLease.run_id == run_id)
        leases = list(await db.scalars(query))
        result = []
        for lease in leases:
            run = await db.get(Run, lease.run_id)
            owner = await db.get(RuntimeGeneration, lease.generation_id)
            groups = list(await db.scalars(select(RuntimeProcessGroup).where(
                RuntimeProcessGroup.run_id == lease.run_id,
                RuntimeProcessGroup.generation_id == lease.generation_id,
                RuntimeProcessGroup.lease_token == lease.token).order_by(RuntimeProcessGroup.attempt)))
            owner_dead = _process_dead(owner.pid, owner.process_started_at) if owner else None
            states = [_group_state(group) for group in groups]
            result.append({"run_id": str(lease.run_id), "owner_id": str(lease.generation_id),
                "run_status": run.status if run else None,
                "owner_revision": owner.revision if owner else None,
                "owner_pid": owner.pid if owner else None,
                "owner_process_started_at": owner.process_started_at if owner else None,
                "owner_dead": owner_dead, "lease_token_matches": bool(owner and owner.id == lease.generation_id),
                "groups": [{"attempt": group.attempt, "pid": group.pid, "pgid": group.pgid,
                            "state": state, "identity_verified": identity_ok}
                           for group, (state, identity_ok) in zip(groups, states, strict=True)],
                "disposition": disposition(owner_dead=owner_dead, groups=states),
                "_lease_token": lease.token, "_group_rows": groups})
        return result


def _sqlstate(exc: DBAPIError) -> str | None:
    original = exc.orig
    return getattr(original, "sqlstate", None) or getattr(original, "pgcode", None)


def _timeout_outcome(exc: DBAPIError) -> str | None:
    """Classify a PostgreSQL error; None means it is not a reconcile timeout.

    lock_timeout is the dedicated SQLSTATE 55P03. statement_timeout shares 57014
    (query_canceled) with pg_cancel_backend and client cancel, which PostgreSQL
    distinguishes only by the server message. Match that message, and treat any
    unrecognized 57014 (including a localized lc_messages text) as a user cancel
    so it propagates instead of being retried.
    """
    sqlstate = _sqlstate(exc)
    if sqlstate == "55P03":
        return "retryable_lock_timeout"
    if sqlstate == "57014" and "due to statement timeout" in str(exc.orig).lower():
        return "retryable_statement_timeout"
    return None


_RETRYABLE_OUTCOMES = {
    "retryable_lock_timeout": "PostgreSQL lock_timeout (55P03); transaction rolled back; retry reconcile",
    "retryable_statement_timeout": "PostgreSQL statement_timeout (57014); transaction rolled back; "
                                   "retry reconcile after checking DB load",
}


async def _apply_one(entry: dict) -> bool | str:
    if entry["disposition"] not in {"process_group_live", "safe_to_release"}:
        return False
    groups: list[RuntimeProcessGroup] = entry["_group_rows"]
    run_id = uuid.UUID(entry["run_id"])
    token = entry["_lease_token"]
    async with sessionmaker()() as db:
        try:
            # Bound lock waits only for this reconcile transaction. The statement
            # timeout also bounds individual SQL statements without changing the
            # connection's session-level defaults.
            await db.execute(text("SET LOCAL lock_timeout = '2000ms'"))
            await db.execute(text("SET LOCAL statement_timeout = '15000ms'"))
            return await _apply_locked(db, entry, run_id, token, groups)
        except DBAPIError as exc:
            # PostgreSQL leaves a transaction aborted after these errors. Roll
            # back explicitly so all held row locks are released before return.
            await db.rollback()
            outcome = _timeout_outcome(exc)
            if outcome is not None:
                return outcome
            raise


async def _apply_locked(db, entry: dict, run_id: uuid.UUID, token: uuid.UUID,
                        groups: list[RuntimeProcessGroup]) -> bool:
    # Hold the same control→lease fence used by claims/releases while signaling.
    # This prevents an old snapshot from killing a group after lease replacement.
    control = await _control(db)
    claim_fence = entry.get("expected_claim_owner_fence")
    if claim_fence is not None:
        claim_owner_id = generation.claim_owner(control) if control else None
        if claim_owner_id != claim_fence["generation_id"] or control.epoch != claim_fence["epoch"]:
            return False
        claim_owner = await db.scalar(select(RuntimeGeneration).where(
            RuntimeGeneration.id == claim_owner_id).with_for_update())
        if (claim_owner is None or claim_owner.revision != claim_fence["revision"]
                or claim_owner.state != "active"):
            return False
    lease = await db.scalar(select(RuntimeLease).where(RuntimeLease.run_id == run_id).with_for_update())
    if lease is None or lease.generation_id != uuid.UUID(entry["owner_id"]) or lease.token != token:
        return False
    owner = await db.scalar(select(RuntimeGeneration).where(
        RuntimeGeneration.id == lease.generation_id).with_for_update())
    if (owner is None or owner.pid != entry.get("owner_pid")
            or owner.process_started_at != entry.get("owner_process_started_at")
            or not _owner_gone(owner, entry)):
        return False
    current_groups = list(await db.scalars(select(RuntimeProcessGroup).where(
        RuntimeProcessGroup.run_id == run_id).order_by(RuntimeProcessGroup.attempt).with_for_update()))
    if {_group_fence_key(x) for x in current_groups} != {_group_fence_key(x) for x in groups}:
        return False
    run = await db.scalar(select(Run).where(Run.id == run_id).with_for_update())
    if run is None:
        return False
    if entry.get("owner_self") is True:
        # 핸들 없는 running Run 만 대상이다. 락을 잡은 뒤에 핸들·하트비트를 다시 확인해
        # 방금 시작했거나 막 핸들을 되찾은 Run 을 건드리지 않는다.
        if run.status != RunStatus.running or run_id in entry["handles"]:
            return False
        heartbeat = run.heartbeat_at or run.started_at or run.created_at
        if (utcnow() - heartbeat).total_seconds() < entry["stale_after"]:
            return False
    cancel_commands = []
    if entry.get("owner_self") is True:
        # 사용자가 이미 누른 중지 요청이 있으면 취소로 마감하고, 없으면 interrupted 로 남겨 기존 재개 규칙을 따른다.
        cancel_commands = list(await db.scalars(select(RuntimeCommand).where(
            RuntimeCommand.run_id == run_id, RuntimeCommand.kind == "cancel",
            RuntimeCommand.generation_id == lease.generation_id,
            RuntimeCommand.acknowledged_at.is_(None)).with_for_update()))
    elif entry.get("require_cancel"):
        # Explicit cancellation may recover its dead owner; ordinary orphan
        # recovery remains an external-operator operation. Verify the durable
        # request under the same fence before signaling anything.
        cancel_commands = list(await db.scalars(select(RuntimeCommand).where(
            RuntimeCommand.run_id == run_id, RuntimeCommand.kind == "cancel",
            RuntimeCommand.generation_id == lease.generation_id,
            RuntimeCommand.acknowledged_at.is_(None)).with_for_update()))
        if not cancel_commands:
            return False
    if not _owner_gone(owner, entry):
        return False
    # Recheck every fenced identity under the locks immediately before any signal.
    for group in current_groups:
        state, identity_ok = _group_state(group)
        if state == "absent":
            continue
        if state != "present" or not identity_ok or not await _stop_verified_group(group):
            return False
        if _group_state(group)[0] != "absent":
            return False
    if any(_group_state(group)[0] != "absent" for group in current_groups):
        return False
    from ttakkari.runs import events
    from ttakkari.runs.manager import _interrupted_handoff
    if run.status == RunStatus.running:
        run.status = RunStatus.cancelled if cancel_commands else RunStatus.interrupted
        run.pid = None
        run.heartbeat_at = utcnow()
        run.finished_at = utcnow()
        if entry.get("owner_self") is True:
            run.error = ("사용자 중지 요청: 실행 핸들 분실, 프로세스 종료 확인 후 취소" if cancel_commands else
                         "실행 핸들을 잃은 Run 정리: 기록된 프로세스 그룹이 모두 종료됨")
        else:
            run.error = ("사용자 중지 요청: 종료된 소유 세대의 프로세스 정리 후 취소" if cancel_commands else
                         "executor orphan reconciled after recorded process groups exited")
        if cancel_commands:
            run.resume_after = None
            for command in cancel_commands:
                command.acknowledged_at = utcnow()
        else:
            run.resume_handoff = await _interrupted_handoff(db, run)
        events.append(db, run, "run.finished", {"status": run.status, "error": run.error,
                                                  "result_text": None, "cost_usd": None, "exit_code": None})
    else:
        # The owner may die after saving a wait/cancel/final result but before
        # releasing its lease. Preserve that state; only discard proven dead ownership.
        if cancel_commands:
            for command in cancel_commands:
                command.acknowledged_at = utcnow()
        events.append(db, run, "run.recovery_lease_released", {"status": run.status,
                                                              "owner_id": entry["owner_id"]})
    await db.execute(delete(RuntimeProcessGroup).where(
        RuntimeProcessGroup.run_id == run_id, RuntimeProcessGroup.lease_token == token))
    await db.delete(lease)
    await db.commit()
    return True


async def reap_handleless_runs(*, generation_id: uuid.UUID, handles, stale_after: float = 120.0) -> int:
    """이 세대가 소유한 lease 중 실행 핸들이 없는 running Run 을 정리하고 정리한 수를 돌려준다.

    fd 고갈 등으로 핸들만 사라지면 소유 세대가 살아 있어 기존 정리(소유자 사망 전제)가 막고, 취소 명령도
    핸들이 있어야 처리돼 Run 이 영원히 running 으로 남는다(2026-10-29). 기록된 프로세스 그룹이 모두 사라졌고
    하트비트가 오래된 Run 만 대상이다. 그룹이 살아 있거나 판정 불명이면 건드리지 않으며 시그널은 보내지 않는다.
    """
    handled = set(handles)
    done = 0
    for entry in await _read_orphans():
        if (entry["owner_id"] != str(generation_id) or entry["owner_pid"] != os.getpid()
                or entry["run_status"] != RunStatus.running or uuid.UUID(entry["run_id"]) in handled
                or not entry["groups"]
                or any(g["state"] != "absent" or not g["identity_verified"] for g in entry["groups"])):
            continue
        entry.update(owner_self=True, handles=handled, stale_after=stale_after, disposition="safe_to_release")
        try:
            if await _apply_one(entry) is True:
                done += 1
        except Exception:  # 다음 주기에 다시 시도한다. 한 건의 실패가 코디네이터를 멈추면 안 된다.
            logging.getLogger(__name__).exception("handleless run reap failed for %s", entry["run_id"])
    return done


async def cancel_orphan(run_id: uuid.UUID) -> bool:
    """Finish only this explicitly cancelled Run after fenced OS verification.

    Live/unknown owners and missing/unknown process evidence stay quarantined.
    This never resumes work or scans/mutates another Run's lease.
    """
    entries = await _read_orphans(run_id)
    if not entries:
        return False
    entry = entries[0]
    entry["require_cancel"] = True
    return await _apply_one(entry) is True


async def reconcile_one(run_id: uuid.UUID, *, expected_owner_generation_id: uuid.UUID,
                        expected_owner_revision: str, expected_run_status: str,
                        expected_claim_owner_generation_id: uuid.UUID,
                        expected_claim_owner_revision: str, expected_claim_owner_epoch: int) -> dict:
    """Reconcile one explicitly named orphan after matching its persisted identity fence.

    The response deliberately excludes PID, process-group and lease-token material.
    `_apply_one` rechecks generation PID/start-time, lease token and every recorded
    process-group identity under the same control/lease locks before changing state.
    """
    entries = await _read_orphans(run_id)
    if len(entries) != 1:
        return {"run_id": str(run_id), "reconciled": False, "action": "fence_mismatch"}
    entry = entries[0]
    if (entry["owner_id"] != str(expected_owner_generation_id)
            or entry["owner_revision"] != expected_owner_revision
            or entry["run_status"] != expected_run_status):
        return {"run_id": str(run_id), "reconciled": False, "action": "fence_mismatch"}
    entry["expected_claim_owner_fence"] = {
        "generation_id": expected_claim_owner_generation_id,
        "revision": expected_claim_owner_revision,
        "epoch": expected_claim_owner_epoch,
    }
    outcome = await _apply_one(entry)
    changed = outcome is True
    if changed:
        action = "interrupted_and_released" if expected_run_status == RunStatus.running else "lease_released"
    elif isinstance(outcome, str):
        action = outcome
    else:
        action = "blocked"
    return {"run_id": str(run_id), "reconciled": changed, "action": action}


async def reconcile_orphans(*, apply: bool = False,
                            expected_claim_owner_fence: dict | None = None) -> dict:
    entries = await _read_orphans()
    results = []
    for entry in entries:
        apply_entry = {**entry, "expected_claim_owner_fence": expected_claim_owner_fence} \
            if apply and expected_claim_owner_fence is not None else entry
        outcome = await _apply_one(apply_entry) if apply else False
        changed = outcome is True
        retryable_timeout = outcome if outcome in _RETRYABLE_OUTCOMES else None
        results.append({"run_id": entry["run_id"], "owner_id": entry["owner_id"],
            "disposition": entry["disposition"], "groups": entry["groups"], "reconciled": changed,
            "action": ("interrupted_and_released" if entry.get("run_status", "running") == "running"
                       else "lease_released") if changed else (retryable_timeout or (
                "would_reconcile" if entry["disposition"] in
                {"safe_to_release", "process_group_live"} else "blocked")),
            **({"reason": _RETRYABLE_OUTCOMES[retryable_timeout]} if retryable_timeout else {})})
    return {"mode": "apply" if apply else "dry-run", "leases": results,
            "reconciled": sum(row["reconciled"] for row in results)}
