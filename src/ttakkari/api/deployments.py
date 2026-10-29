"""에이전트는 예약만 제출한다. 외부 감독이 세대 전환 또는 legacy idle 재시작을 맡는다."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, Field

from ttakkari import deployment, generation
from ttakkari import generation_deployment as gd
from ttakkari.api.agent import _session
from ttakkari.api.deps import Db
from ttakkari.audit import record
from ttakkari.generation import PORTS
from ttakkari.models import ChatSession, RuntimeControl, RuntimeGeneration, SessionKind
from ttakkari.operations import inline
from ttakkari.orchestrator.runtime import OrchestratorSession as AgentSession

router = APIRouter(prefix="/api/agent/deploy", tags=["agent"])


class DeployIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    revision: str = Field(pattern=r"^[0-9a-f]{40}$")


class RecoverDrainIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_revision: str = Field(pattern=r"^[0-9a-f]{40}$")
    expected_previous_generation: uuid.UUID
    expected_previous_revision: str = Field(pattern=r"^[0-9a-f]{40}$")
    expected_previous_slot: int = Field(ge=0, lt=len(PORTS), strict=True)
    expected_candidate_generation: uuid.UUID
    expected_active_generation: uuid.UUID
    expected_active_revision: str = Field(pattern=r"^[0-9a-f]{40}$")
    expected_epoch: int = Field(ge=0, strict=True)
    idempotency_key: str = Field(min_length=1, max_length=160)
    apply: bool = False


async def personal(sid: uuid.UUID, db: Db) -> None:
    await _session(db, sid)


async def _personal_orchestrator(sid: uuid.UUID, db: Db) -> ChatSession:
    session = await db.get(ChatSession, sid)
    if (
        session is None
        or session.archived_at is not None
        or session.pool != "personal"
        or session.kind != SessionKind.orchestrator
    ):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "개인 오케스트레이터 세션만 사용할 수 있습니다.")
    return session


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


@router.get("/diagnostics")
async def deploy_diagnostics_endpoint(sid: AgentSession, db: Db) -> dict:
    await _personal_orchestrator(sid, db)
    root = deployment.state_dir()
    store = deployment.JobStore(root)
    report = await inline.runtime_report(db)
    return {
        "deploy_strategy": deployment.strategy(),
        "supervisor": deployment.supervisor_diagnostics(root),
        "deploy_queue": deployment.queue_diagnostics(store),
        "control": report.get("control"),
        "generations": report.get("generations"),
        "slots": report.get("slots"),
    }


@router.post("/{job_id}/recover-drain")
async def recover_drain_endpoint(
    job_id: uuid.UUID, body: RecoverDrainIn, sid: AgentSession, db: Db
) -> dict:
    """컷오버 이후 drain recovery_pending 배포만 고정 스키마·owner fence 아래 복구한다.

    임의 shell·경로 입력을 받지 않으며, 현재 요청을 처리하는 API 프로세스 자신이
    expected_active_generation·expected_epoch의 활성 소유자일 때만 실행한다.
    """
    await _personal_orchestrator(sid, db)
    expected_key = f"recover-drain:{job_id}:{body.expected_previous_generation}:{body.expected_epoch}"
    if body.idempotency_key != expected_key:
        raise HTTPException(
            422,
            "idempotency_key가 대상 배포·세대·epoch와 일치하지 않습니다.",
        )

    control = await db.get(RuntimeControl, 1)
    owner_id = (
        getattr(control, "executor_generation", None) or getattr(control, "active_generation", None)
        if control
        else None
    )
    owner = await db.get(RuntimeGeneration, owner_id) if owner_id else None
    current_runtime = generation.current
    if (
        control is None
        or owner_id != body.expected_active_generation
        or control.epoch != body.expected_epoch
        or owner is None
        or owner.revision != body.expected_active_revision
        or owner.state != "active"
        or current_runtime is None
        or current_runtime.id != body.expected_active_generation
        or getattr(current_runtime, "draining", False)
    ):
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "현재 API 세대가 요청된 active owner fence와 일치하지 않습니다.",
        )

    store = deployment.JobStore(deployment.state_dir())
    controller = gd.GenerationController(store)
    try:
        result = await controller.recover_drain(
            job_id=str(job_id),
            expected_revision=body.expected_revision,
            expected_previous_generation=str(body.expected_previous_generation),
            expected_previous_revision=body.expected_previous_revision,
            expected_previous_slot=body.expected_previous_slot,
            expected_candidate_generation=str(body.expected_candidate_generation),
            expected_active_generation=str(body.expected_active_generation),
            expected_active_revision=body.expected_active_revision,
            expected_epoch=body.expected_epoch,
            apply=body.apply,
        )
    except FileNotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "배포 예약이 없습니다.") from exc
    except (ValueError, RuntimeError) as exc:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "배포 drain 복구 fence 또는 상태 조건을 만족하지 않습니다.",
        ) from exc

    record(
        db,
        actor="agent",
        action="deployment.recover_drain",
        target_type="deployment",
        target_id=job_id,
        detail={
            "job_id": str(job_id),
            "apply": body.apply,
            "applied": result.get("applied"),
            "idempotent": result.get("idempotent"),
            "action": result.get("action"),
            "expected_epoch": body.expected_epoch,
            "session_id": str(sid),
        },
    )
    await db.commit()
    return result


@router.get("/{job_id}")
async def deploy_status(job_id: uuid.UUID, sid: AgentSession, db: Db) -> dict:
    await personal(sid, db)
    try:
        return deployment.JobStore(deployment.state_dir()).get(str(job_id))
    except FileNotFoundError as exc:
        raise HTTPException(404, "배포 예약이 없습니다.") from exc
