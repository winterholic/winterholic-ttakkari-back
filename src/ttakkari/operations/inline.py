"""Bounded inline operations: read-only reports and explicitly named guarded library maintenance."""

from __future__ import annotations

import asyncio
import os
import shutil
import statistics
import time
from datetime import UTC, datetime

import psutil
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from ttakkari import backup_status, deployment, generation
from ttakkari.config import get_settings
from ttakkari.models import (
    ChatSession,
    Run,
    RunStatus,
    RuntimeControl,
    RuntimeGeneration,
    RuntimeLease,
    RuntimeProcessGroup,
)
from ttakkari.operations import plans

DB_PROBES = 20
LOOP_PROBES = 20
RECENT_DEPLOYS = 5


async def collection_report(db: AsyncSession) -> dict:
    """Recent owner missions: counts and current-run error codes, never document or error bodies."""
    from ttakkari.collector.control_models import CollectorDelivery
    from ttakkari.collector_models import CollectionMission as Mission
    from ttakkari.collector_models import CollectionMissionJob as Link
    from ttakkari.collector_models import CollectorItem as Item
    from ttakkari.collector_models import CollectorJob as Job

    recent = (await db.execute(select(
        Mission.id, func.substr(Mission.plan["title"].astext, 1, 100).label("title"), Mission.status,
        Mission.created_at, Mission.updated_at, Mission.current_run_seq.label("run_seq"),
        Mission.plan["research"]["stop"].astext.label("stop"),
        Mission.plan["research"]["searches_used"].astext.label("searches_used"),
        *(func.substr(Mission.gaps[i]["reason"].astext, 1, 80).label(f"gap_{i}") for i in range(5)),
    ).where(Mission.tenant_id == "owner").order_by(Mission.created_at.desc(), Mission.id.desc()).limit(20))).all()
    report = {"readonly": True, "limit": 20, "missions": []}
    if not recent:
        return report
    ids = [row.id for row in recent]
    code = func.substr(Job.last_error["code"].astext, 1, 80)
    job_rows = (await db.execute(select(
        Link.mission_id, Link.run_seq, Job.status, Job.stage, code.label("error_code"),
        func.count().label("jobs"), func.sum(Job.attempts).label("attempts"),
        func.min(Job.not_before).label("next_retry"),
    ).join(Job, Job.id == Link.job_id).join(Mission, Mission.id == Link.mission_id).where(
        Link.mission_id.in_(ids), Link.run_seq == Mission.current_run_seq,
        Mission.tenant_id == "owner", Job.tenant_id == "owner",
    ).group_by(Link.mission_id, Link.run_seq, Job.status, Job.stage, code)
        .order_by(Link.mission_id, Job.stage, Job.status, code).limit(201))).all()
    counts = (await db.execute(select(
        Link.mission_id,
        func.count(Item.id.distinct()).filter(Item.raw_sha256.is_not(None)).label("acquired"),
        *(func.count(Item.id.distinct()).filter(Item.status == status).label(status)
          for status in ("accepted", "failed", "blocked")),
        func.count(Item.id.distinct()).filter(CollectorDelivery.outcome == "imported").label("imported"),
    ).join(Item, Item.job_id == Link.job_id).join(Mission, Mission.id == Link.mission_id)
        .outerjoin(CollectorDelivery, CollectorDelivery.item_id == Item.id).where(
            Link.mission_id.in_(ids), Mission.tenant_id == "owner", Item.tenant_id == "owner",
        ).group_by(Link.mission_id))).all()
    by_id = {row.mission_id: {key: getattr(row, key) for key in
                             ("acquired", "accepted", "failed", "blocked", "imported")} for row in counts}
    report["job_groups_truncated"] = len(job_rows) > 200
    for row in recent:
        report["missions"].append({
            "id": str(row.id), "title": row.title, "status": row.status, "run_seq": row.run_seq,
            "created_at": row.created_at.isoformat(), "updated_at": row.updated_at.isoformat(),
            "research": {"stop": row.stop, "searches_used": row.searches_used},
            "gap_codes": [getattr(row, f"gap_{i}") for i in range(5) if getattr(row, f"gap_{i}")],
            "gap_codes_limit": 5,
            "counts": by_id.get(row.id, dict.fromkeys(("acquired", "accepted", "failed", "blocked", "imported"), 0)),
            "jobs": [{"run_seq": job.run_seq, "status": job.status, "stage": job.stage,
                      "error_code": job.error_code, "jobs": job.jobs, "attempts": job.attempts,
                      "next_retry": job.next_retry.isoformat() if job.next_retry else None}
                     for job in job_rows[:200] if job.mission_id == row.id],
        })
    return report


def _ms(values: list[float]) -> dict:
    ordered = sorted(values)
    return {"n": len(ordered), "p50_ms": round(statistics.median(ordered), 2),
            "p95_ms": round(ordered[min(len(ordered) - 1, int(len(ordered) * 0.95))], 2),
            "max_ms": round(ordered[-1], 2)}


async def runtime_report(db: AsyncSession) -> dict:
    """실행 소유권: 활성/실행기 세대, 세대별 lease·프로세스 그룹 수, 최근 배포 예약. 읽기만 한다."""
    control = (await db.execute(select(RuntimeControl))).scalars().first()
    gens = (await db.execute(select(RuntimeGeneration).order_by(RuntimeGeneration.created_at.desc()).limit(8))
            ).scalars().all()
    leases = dict((await db.execute(select(RuntimeLease.generation_id, func.count())
                                    .group_by(RuntimeLease.generation_id))).all())
    groups = dict((await db.execute(select(RuntimeProcessGroup.generation_id, func.count())
                                    .group_by(RuntimeProcessGroup.generation_id))).all())
    running = (await db.execute(select(func.count()).select_from(Run).where(Run.status == RunStatus.running))).scalar()
    now = datetime.now(UTC)
    current = generation.current
    recovery_rows = (await db.execute(select(
        Run.id, Run.session_id, ChatSession.title, ChatSession.pool, Run.status, Run.engine, Run.model,
        Run.resume_after, RuntimeLease.generation_id,
        Run.usage["automatic_recovery"]["attempts"].as_integer().label("recovery_attempts"),
    ).join(ChatSession, ChatSession.id == Run.session_id)
        .outerjoin(RuntimeLease, RuntimeLease.run_id == Run.id)
        .where(ChatSession.archived_at.is_(None), Run.status.in_(("running", "queued", "interrupted")))
        .order_by((Run.status == "running").desc(), Run.created_at.desc()).limit(40))).all()
    try:
        deploys = [{"id": j["id"], "revision": j["revision"], "status": j["status"], "created_at": j["created_at"]}
                   for j in deployment.JobStore(deployment.state_dir()).all()[-RECENT_DEPLOYS:]]
        strategy = deployment.strategy()
    except Exception as exc:  # noqa: BLE001 -- 배포 상태를 못 읽어도 소유권 보고는 계속한다
        deploys, strategy = [], f"unreadable:{type(exc).__name__}"
    return {
        "this_process": {"pid": os.getpid(), "release_revision": get_settings().release_revision,
                         "generation_id": str(current.id) if current else None,
                         "role": getattr(current, "role", None),
                         "execution_mode": getattr(current, "execution_mode", None)},
        "control": {"active_generation": str(control.active_generation) if control and control.active_generation else None,
                    "executor_generation": str(control.executor_generation) if control and control.executor_generation else None,
                    "epoch": control.epoch if control else None},
        "generations": [{"id": str(g.id), "revision": g.revision, "role": g.role, "state": g.state,
                         "execution_mode": g.execution_mode, "pid": g.pid,
                         "pid_alive": psutil.pid_exists(g.pid),
                         "heartbeat_age_s": round((now - g.heartbeat_at.replace(tzinfo=UTC)).total_seconds(), 1),
                         "leases": leases.get(g.id, 0), "process_groups": groups.get(g.id, 0)} for g in gens],
        "runs_running": running,
        "recovery_runs": [{"run_id": str(r.id), "session_id": str(r.session_id), "title": r.title,
                           "pool": r.pool, "status": r.status, "engine": r.engine, "model": r.model,
                           "resume_after": r.resume_after.isoformat() if r.resume_after else None,
                           "lease_owner": str(r.generation_id) if r.generation_id else None,
                           "recovery_attempts": r.recovery_attempts or 0} for r in recovery_rows],
        "deploy_strategy": strategy, "recent_deploys": deploys,
        "collection": await collection_report(db),
    }


async def performance_baseline(db: AsyncSession) -> dict:
    """DB 왕복·이벤트 루프 지연·호스트 압박을 짧게 잰다(수 초). 부하를 만들지 않는다."""
    db_times = []
    for _ in range(DB_PROBES):
        t = time.perf_counter()
        await db.execute(text("SELECT 1"))
        db_times.append((time.perf_counter() - t) * 1000)
    lag = []
    for _ in range(LOOP_PROBES):
        t = time.perf_counter()
        await asyncio.sleep(0.01)
        lag.append(max(0.0, (time.perf_counter() - t - 0.01) * 1000))
    swap, mem, disk = psutil.swap_memory(), psutil.virtual_memory(), shutil.disk_usage(get_settings().home)
    load = os.getloadavg()
    return {"synthetic": True, "readonly": True, "db_roundtrip": _ms(db_times), "event_loop_lag": _ms(lag),
            "host": {"load_avg": [round(x, 2) for x in load], "cores": psutil.cpu_count(),
                     "swap_percent": swap.percent, "memory_percent": mem.percent,
                     "disk_free_gb": round(disk.free / 1e9, 1)}}


def backup_state() -> dict:
    status = backup_status.backup_status()
    try:
        key_path, source = plans.passphrase_file(create=False)
        fp = plans.key_fingerprint(key_path)
    except plans.Refused:
        source = None
        fp = None
    root = plans.backup_dir()
    return {**status, "key_source": source, "key_fingerprint": fp, "backup_dir_exists": root.is_dir(),
            "disk_free_gb": round(shutil.disk_usage(root if root.is_dir() else root.parent).free / 1e9, 1)}


async def run(kind: str, db: AsyncSession) -> dict:
    if kind in {"collection_scope_preview", "collection_scope_apply"}:
        from ttakkari.collector.scope_merge import operate
        return await asyncio.wait_for(operate(db, apply=kind == "collection_scope_apply"), timeout=60)
    if kind == "runtime_report":
        return await runtime_report(db)
    if kind == "performance_baseline":
        return await performance_baseline(db)
    return backup_state()
