"""에이전트는 예약만 제출한다. 서비스 권한과 작업 종료 감시는 외부 감독이 맡는다."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from ttakkari import deployment
from ttakkari.api.agent import _session
from ttakkari.api.deps import Db
from ttakkari.audit import record
from ttakkari.orchestrator.runtime import OrchestratorSession as AgentSession

router = APIRouter(prefix="/api/agent/deploy", tags=["agent"])


class DeployIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    revision: str = Field(pattern=r"^[0-9a-f]{40}$")


async def personal(sid: uuid.UUID, db: Db) -> None:
    if (await _session(db, sid)).pool == "work":
        raise HTTPException(403, "회사 세션에서는 개인 서버를 배포할 수 없습니다.")


@router.post("", status_code=202)
async def request_deploy(
    body: DeployIn, sid: AgentSession, db: Db, request: Request
) -> dict:
    await personal(sid, db)
    try:
        deployment.ensure_revision(body.revision)
        job = deployment.JobStore(deployment.state_dir()).request(body.revision)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    record(
        db,
        actor="agent",
        action="deployment.request",
        detail={"job_id": job["id"], "revision": body.revision, "session_id": str(sid)},
    )
    await db.commit()
    return job


@router.get("/{job_id}")
async def deploy_status(job_id: uuid.UUID, sid: AgentSession, db: Db) -> dict:
    await personal(sid, db)
    try:
        return deployment.JobStore(deployment.state_dir()).get(str(job_id))
    except FileNotFoundError as exc:
        raise HTTPException(404, "배포 예약이 없습니다.") from exc
