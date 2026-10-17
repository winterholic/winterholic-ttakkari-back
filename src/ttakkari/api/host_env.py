"""모든 Run 의 MCP 도구가 부르는 실행 환경 능력 보고와 호스트 GPU 시험 경로(ADR 0037). 세션 한정 토큰."""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from ttakkari import host_gpu
from ttakkari.api.agent import _session
from ttakkari.api.deps import Db
from ttakkari.audit import record
from ttakkari.config import get_settings
from ttakkari.models import Run
from ttakkari.orchestrator.runtime import OrchestratorSession as AgentSession
from ttakkari.runs import capabilities

router = APIRouter(prefix="/api/agent", tags=["agent"])


class ReportIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    report: dict[str, Any]


class BenchIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    prompt: str = Field(min_length=1, max_length=8000)
    n_predict: int = Field(default=128, ge=1, le=512)
    runs: int = Field(default=3, ge=1, le=5)


async def _personal(sid, db: Db) -> None:
    if (await _session(db, sid)).pool == "work":
        raise HTTPException(403, "회사 세션에서는 개인 호스트 GPU 경로를 쓸 수 없습니다.")


def _host() -> host_gpu.HostGpu:
    return host_gpu.HostGpu(get_settings())


async def _call(fn, *args):
    try:
        return await asyncio.to_thread(fn, *args)
    except host_gpu.GpuError as exc:
        raise HTTPException(exc.status_code, str(exc)) from exc


@router.get("/execution-capabilities")
async def execution_capabilities(sid: AgentSession, db: Db) -> dict:
    await _session(db, sid)
    try:
        await asyncio.to_thread(capabilities.refresh_host)  # 호스트 기준선은 1시간 넘으면 새로 잰다
    except (capabilities.ReportError, OSError, ValueError):
        pass  # 갱신 실패는 마지막 값(또는 unknown)을 그대로 보여 준다
    return {"engines": capabilities.snapshot(), "probe_script": str(capabilities.probe_script())}


@router.post("/execution-capabilities/report")
async def report_execution_capabilities(body: ReportIn, sid: AgentSession, db: Db) -> dict:
    """세션이 자기 환경에서 돌린 프로브 결과를 받는다. 엔진은 보고 내용이 아니라 이 세션의 최근 Run 으로 정한다."""
    await _session(db, sid)
    row = (await db.execute(select(Run.id, Run.engine).where(Run.session_id == sid)
                            .order_by(Run.created_at.desc()).limit(1))).first()
    if row is None:
        raise HTTPException(409, "이 세션에는 아직 Run 이 없어 엔진을 알 수 없습니다.")
    run_id, engine = row
    try:
        view = capabilities.record(engine, body.report, source="session_probe", session_id=str(sid), run_id=str(run_id))
    except capabilities.ReportError as exc:
        raise HTTPException(422, str(exc)) from exc
    record(db, actor="agent", action="execution_capabilities.report",
           detail={"session_id": str(sid), "engine": engine})
    await db.commit()
    return view


@router.post("/host-gpu/start", status_code=202)
async def host_gpu_start(body: host_gpu.StartRequest, sid: AgentSession, db: Db) -> dict:
    await _personal(sid, db)
    out = await _call(_host().start, body)
    record(db, actor="agent", action="host_gpu.start",
           detail={"session_id": str(sid), "request": body.model_dump()})
    await db.commit()
    return out


@router.get("/host-gpu/status")
async def host_gpu_status(sid: AgentSession, db: Db) -> dict:
    await _personal(sid, db)
    return await _call(_host().status)


@router.post("/host-gpu/bench")
async def host_gpu_bench(body: BenchIn, sid: AgentSession, db: Db) -> dict:
    await _personal(sid, db)
    return await _call(_host().bench, body.prompt, body.n_predict, body.runs)


@router.post("/host-gpu/stop")
async def host_gpu_stop(sid: AgentSession, db: Db) -> dict:
    await _personal(sid, db)
    out = await _call(_host().stop)
    record(db, actor="agent", action="host_gpu.stop", detail={"session_id": str(sid)})
    await db.commit()
    return out
