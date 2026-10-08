from __future__ import annotations

import asyncio
import shutil
import uuid
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request, status
from fastapi.responses import PlainTextResponse
from sqlalchemy import select, text

from ttakkari.api.deps import Db, get_workspace
from ttakkari.artifacts.preview import soffice_path
from ttakkari.audit import record
from ttakkari.config import get_settings
from ttakkari.models import Run, RunStatus
from ttakkari.runs.manager import manager
from ttakkari.security.auth import User, client_ip

router = APIRouter(prefix="/api", tags=["system"])
DIFF_LIMIT = 400_000


@router.get("/health")
async def health(db: Db) -> dict:
    await db.execute(text("select 1"))
    return {"ok": True}


@router.get("/system/info")
async def info(_: User) -> dict:
    s = get_settings()
    return {
        "engines": {
            "claude": shutil.which(s.claude_bin) is not None,
            "codex": shutil.which(s.codex_bin) is not None,
        },
        "preview_converter": soffice_path() is not None,
        "allowed_roots": [str(p) for p in s.allowed_roots],
        "max_concurrent_runs": s.max_concurrent_runs,
    }


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
