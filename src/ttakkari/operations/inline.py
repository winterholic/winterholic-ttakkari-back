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
        "deploy_strategy": strategy, "recent_deploys": deploys,
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
