from __future__ import annotations

import asyncio
import os
import shutil
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, HTTPException, Query, Request, Response, status
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, ConfigDict, StrictBool
from sqlalchemy import select, text

from ttakkari import sysinfo
from ttakkari.api.deps import Db, get_workspace
from ttakkari.artifacts.preview import soffice_path
from ttakkari.audit import record
from ttakkari.config import get_settings
from ttakkari.models import AppSetting, Run, RunStatus, SystemResourceSample, utcnow
from ttakkari.runs.manager import manager
from ttakkari.security.auth import User, client_ip

router = APIRouter(prefix="/api", tags=["system"])
DIFF_LIMIT = 400_000


class QwenWorkerActivation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: StrictBool


@router.get("/system/qwen-worker")
async def qwen_worker_status(_: User, db: Db, response: Response) -> dict:
    from ttakkari.runs import qwen_activation
    response.headers["Cache-Control"] = "no-store"
    return await qwen_activation.status(db)


@router.put("/system/qwen-worker")
async def set_qwen_worker(body: QwenWorkerActivation, _: User, db: Db, request: Request) -> dict:
    from ttakkari.agents.qwen_local import QwenLocalAdapter
    from ttakkari.runs import qwen_activation
    if qwen_activation.config_locked():
        raise HTTPException(409, detail={"code": "qwen_config_locked",
                                        "message": "운영 설정에 고정되어 있어 앱에서 변경할 수 없습니다."})
    if body.enabled:
        settings = get_settings()
        candidate = QwenLocalAdapter(worker_path=settings.qwen_local_worker_path, enabled=True,
                                     turns=settings.qwen_local_turns)
        ready, reason = await asyncio.to_thread(candidate.readiness)
        if not ready:
            raise HTTPException(409, detail={"code": "qwen_not_ready",
                                            "message": f"Qwen 준비 검사를 통과하지 못했습니다. ({reason})"})
    from sqlalchemy.dialects.postgresql import insert
    statement = insert(AppSetting).values(key=qwen_activation.KEY, value={"enabled": body.enabled})
    await db.execute(statement.on_conflict_do_update(
        index_elements=[AppSetting.key], set_={"value": statement.excluded.value, "updated_at": utcnow()}))
    record(db, action="qwen_worker.activation", detail={"enabled": body.enabled}, ip=client_ip(request))
    await db.commit()
    return {"enabled": body.enabled, "config_locked": False,
            "ready": body.enabled, "reason": "ready" if body.enabled else "disabled"}


@router.get("/bootstrap-ready")
async def bootstrap_readiness(request: Request) -> dict:
    if not getattr(request.app.state, "bootstrap_hold", False):
        raise HTTPException(404, "Bootstrap holding mode is disabled")
    return {"holding": True, "revision": request.app.state.revision, "pid": os.getpid()}


@router.get("/ready")
async def readiness() -> dict:
    from ttakkari import generation
    from ttakkari.generation_service import ready
    if generation.current is None:
        raise HTTPException(404, "Generation mode is disabled")
    value = await ready(generation.current)
    if not value["ready"]:
        raise HTTPException(503, "Generation listener is reconnecting")
    return value


@router.get("/health")
async def health(db: Db) -> dict:
    await db.execute(text("select 1"))
    return {"ok": True}


@router.get("/qwen-status")
async def qwen_status(response: Response) -> dict:
    """접수 로컬 Qwen 서버 상태(인증 없음, 포트·pid·경로는 내지 않는다). 안 떠 있어도 접수는 CLI 로 동작한다."""
    from ttakkari.runs.desk_qwen_service import service as qwen_service
    response.headers["Cache-Control"] = "no-store"
    return qwen_service.status()


@router.get("/version")
async def version(request: Request, response: Response) -> dict:
    response.headers["Cache-Control"] = "no-store"
    return {"revision": request.app.state.revision, "pid": os.getpid()}


@router.get("/system/info")
async def info(_: User) -> dict:
    s = get_settings()
    return {
        "engines": {
            "claude": shutil.which(s.claude_bin) is not None,
            "codex": shutil.which(s.codex_bin) is not None,
            "antigravity": shutil.which(s.antigravity_bin) is not None,
            "muse": shutil.which(s.muse_bin) is not None,
        },
        "engine_readiness": {"muse": {"execution_enabled": s.muse_enabled}},
        "preview_converter": soffice_path() is not None,
        "allowed_roots": [str(p) for p in s.allowed_roots],
        "max_concurrent": {"desk": s.max_concurrent_desk, "leads": s.max_concurrent_leads,
                           "workers": s.max_concurrent_workers},
    }


@router.get("/system/resources")
async def resources(_: User) -> dict:
    """이 맥의 CPU·GPU·메모리·디스크·네트워크와 상위 프로세스. 명령줄은 내지 않는다."""
    return await sysinfo.snapshot()


@router.get("/system/backup-status")
async def backup_status(_: User, response: Response) -> dict:
    """SEC2-OPS-01: 마지막 백업·스냅샷 성공 시각(비밀 없음. 개수·해시만)."""
    from ttakkari import backup_status as status_reader
    response.headers["Cache-Control"] = "no-store"
    return status_reader.backup_status()


@router.get("/system/resources/history")
async def resource_history(
    _: User,
    db: Db,
    response: Response,
    range: Literal["1h", "24h", "7d"] = Query(default="1h"),
) -> dict:
    """GPU history: raw samples for 1h, minute means for 24h, 10-minute means for 7d."""
    response.headers["Cache-Control"] = "no-store"
    window = {"1h": timedelta(hours=1), "24h": timedelta(hours=24), "7d": timedelta(days=7)}[range]
    bucket_seconds = {"1h": 30, "24h": 60, "7d": 600}[range]
    cutoff = datetime.now(UTC) - window
    if range == "1h":
        rows = list(await db.scalars(
            select(SystemResourceSample).where(SystemResourceSample.sampled_at >= cutoff)
            .order_by(SystemResourceSample.sampled_at)
        ))
        points = [{
            "timestamp": row.sampled_at.isoformat(),
            "gpu_percent": row.gpu_percent,
            "gpu_memory_used_bytes": row.gpu_memory_used_bytes,
            "cpu_percent": row.cpu_percent,
            "memory_used_bytes": row.memory_used_bytes,
            "gpu_status": row.gpu_status,
            "collection_status": row.collection_status,
            "sample_count": 1,
            "failed_count": int(row.gpu_percent is None),
        } for row in rows]
    else:
        bucket_seconds = 60 if range == "24h" else 600
        result = await db.execute(text(f"""
            SELECT to_timestamp(floor(extract(epoch FROM sampled_at) / {bucket_seconds}) * {bucket_seconds}) AS bucket,
                   avg(gpu_percent) AS gpu_percent,
                   min(gpu_percent) AS gpu_min_percent,
                   max(gpu_percent) AS gpu_max_percent,
                   avg(gpu_memory_used_bytes) AS gpu_memory_used_bytes,
                   avg(cpu_percent) AS cpu_percent,
                   avg(memory_used_bytes) AS memory_used_bytes,
                   count(*) AS sample_count,
                   count(*) FILTER (WHERE gpu_percent IS NULL) AS failed_count
            FROM system_resource_samples
            WHERE sampled_at >= :cutoff
            GROUP BY bucket
            ORDER BY bucket
        """), {"cutoff": cutoff})
        points = [{
            "timestamp": row.bucket.isoformat(),
            "gpu_percent": float(row.gpu_percent) if row.gpu_percent is not None else None,
            "gpu_min_percent": float(row.gpu_min_percent) if row.gpu_min_percent is not None else None,
            "gpu_max_percent": float(row.gpu_max_percent) if row.gpu_max_percent is not None else None,
            "gpu_memory_used_bytes": int(row.gpu_memory_used_bytes) if row.gpu_memory_used_bytes is not None else None,
            "cpu_percent": float(row.cpu_percent) if row.cpu_percent is not None else None,
            "memory_used_bytes": int(row.memory_used_bytes) if row.memory_used_bytes is not None else None,
            "sample_count": row.sample_count,
            "failed_count": row.failed_count,
        } for row in result]
    return {"range": range, "from": cutoff.isoformat(), "to": datetime.now(UTC).isoformat(),
            "bucket_seconds": bucket_seconds, "points": points}


@router.post("/system/stop-all")
async def stop_all(_: User, db: Db, request: Request) -> dict:
    """비상 정지. 실행 중인 모든 에이전트 프로세스를 끝낸다."""
    rows = list(await db.scalars(select(Run.id).where(Run.status.in_([RunStatus.queued, RunStatus.running]))))
    stopped = [str(r) for r in rows if await manager.cancel(r)]
    record(db, action="system.stop_all", detail={"stopped": stopped}, ip=client_ip(request))
    await db.commit()
    return {"stopped": stopped}


@router.get("/runs/{run_id}/diff", response_class=PlainTextResponse)
async def run_diff(run_id: uuid.UUID, _: User, db: Db) -> str:
    """Run 시작 시점 체크포인트 대비 현재 워크스페이스의 변경 내용(unified diff)."""
    run = await db.get(Run, run_id)
    if run is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Run 이 없습니다.")
    if not run.checkpoint_ref:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "git 체크포인트가 없는 Run 입니다.")
    ws = await get_workspace(db, run.workspace_id)
    proc = await asyncio.create_subprocess_exec(
        "git", "diff", "--no-color", "--no-ext-diff", run.checkpoint_ref, cwd=str(Path(ws.root_path)),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, stdin=asyncio.subprocess.DEVNULL,
    )
    out, err = await proc.communicate()
    if proc.returncode != 0:
        raise HTTPException(status.HTTP_409_CONFLICT, err.decode(errors="replace")[:500])
    body = out.decode("utf-8", errors="replace")
    if len(body) > DIFF_LIMIT:
        body = body[:DIFF_LIMIT] + f"\n… diff 가 커서 {DIFF_LIMIT}자에서 잘랐습니다."
    return body
