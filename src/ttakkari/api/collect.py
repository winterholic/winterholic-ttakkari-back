"""Collector intake API (CP-S2-02). 사용자(`/api/assistant/collect`)와 에이전트(`/api/agent/collect`)가 같은 핸들러를 쓴다.

tenant/scope/principal 은 요청 본문이 아니라 인증 주체에서 산출한다(intake-binding.md).
- 사용자(owner JWT): tenant "owner", principal "owner", scope 는 `?scope=personal|work`(기본 personal). owner 는 두 scope 의
  주체이므로 선택은 권한 상승이 아니다. shared 는 이 API 로 만들지 않는다(승격은 별도 사용자 권한 API).
- 에이전트(세션 토큰): scope 는 세션의 pool(work 이면 work, 아니면 personal). 쿼리로 바꿀 수 없다.
envelope.access 가 산출값과 다르면 403 scope_binding_mismatch 이고 job·outbox·멱등 조회가 일어나지 않는다.
다른 scope/tenant 의 job 은 존재하지 않는 것과 같은 404 로 답해 존재 여부를 드러내지 않는다.
오류 본문은 {code, message}. 403/409 메시지는 고정 문구라 기존 hash·ID·산출 identity 를 싣지 않는다.
"""

# NOTE: from __future__ import annotations 를 쓰지 않는다. 두 라우터가 공유하는 핸들러가 클로저의 `access` 로 Depends 를
# 만들기 때문에 주석을 문자열로 두면 FastAPI 가 의존성을 해석하지 못한다.
import json
import uuid
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, FastAPI, Header, Query, Request, status
from fastapi.responses import JSONResponse

from ttakkari.api.deps import Db
from ttakkari.collector import jobs
from ttakkari.collector.contracts import (
    AuthContext,
    CollectorConflictError,
    CollectorContractError,
    CollectorError,
    ScopeBindingMismatchError,
)
from ttakkari.collector.intake import submit_envelope
from ttakkari.collector_models import CollectorJob
from ttakkari.models import ChatSession
from ttakkari.orchestrator.runtime import OrchestratorSession
from ttakkari.security.auth import User

OWNER_TENANT = "owner"
MAX_BODY_BYTES = 8 * 1024 * 1024

_FIXED_MESSAGES = {
    ScopeBindingMismatchError: "envelope.access does not match the authenticated principal",
    CollectorConflictError: "the submission conflicts with an existing registration",
}


async def user_access(
    _: User, scope: Annotated[Literal["personal", "work"], Query()] = "personal"
) -> AuthContext:
    return AuthContext(tenant_id=OWNER_TENANT, scope=scope, principal_id="owner", kind="owner")


async def agent_access(sid: OrchestratorSession, db: Db) -> AuthContext:
    session_row = await db.get(ChatSession, sid)
    if session_row is None:
        return await _no_session()
    scope = "work" if session_row.pool == "work" else "personal"
    return AuthContext(tenant_id=OWNER_TENANT, scope=scope, principal_id="owner", kind="session")


async def _no_session() -> AuthContext:
    from fastapi import HTTPException

    raise HTTPException(status.HTTP_403_FORBIDDEN, "세션이 없습니다.")


def _public_message(exc: CollectorError) -> str:
    for cls, message in _FIXED_MESSAGES.items():
        if isinstance(exc, cls):
            return message
    return str(exc)


def _handler(request: Request, exc: CollectorError) -> JSONResponse:
    return JSONResponse({"code": exc.error_code, "message": _public_message(exc)}, status_code=exc.http_status)


def install(app: FastAPI) -> None:
    app.add_exception_handler(CollectorError, _handler)  # type: ignore[arg-type]


def _no_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in pairs:
        if key in out:
            raise CollectorContractError(f"duplicate object key '{key}'")
        out[key] = value
    return out


async def _read_envelope(request: Request) -> dict[str, Any]:
    raw = await request.body()
    if len(raw) > MAX_BODY_BYTES:
        raise CollectorContractError(f"request body larger than {MAX_BODY_BYTES} bytes")
    try:
        parsed = json.loads(raw, object_pairs_hook=_no_duplicate_keys)
    except CollectorContractError:
        raise
    except (ValueError, RecursionError) as e:
        raise CollectorContractError("request body is not valid JSON") from e
    if not isinstance(parsed, dict):
        raise CollectorContractError("request body must be a JSON object")
    return parsed


def _view(job: CollectorJob) -> dict[str, Any]:
    """공개 표현. envelope 본문·attempt token 은 싣지 않는다."""
    return {
        "job_id": str(job.id),
        "status": job.status,
        "stage": job.stage,
        "source_key": job.source_key,
        "request_key": job.request_key,
        "envelope_id": str(job.envelope_id),
        "payload_hash": job.payload_hash,
        "attempts": job.attempts,
        "max_attempts": job.max_attempts,
        "not_before": job.not_before.isoformat() if job.not_before else None,
        "owner_generation": job.owner_generation,
        "cursor": job.cursor,
        "last_error": job.last_error,
        "cancel_requested": job.cancel_requested,
        "created_at": job.created_at.isoformat(),
        "updated_at": job.updated_at.isoformat(),
    }


def _routes(r: APIRouter, access: Any) -> None:
    @r.post("/jobs", status_code=status.HTTP_202_ACCEPTED)
    async def create_job(
        request: Request,
        ctx: Annotated[AuthContext, Depends(access)],
        db: Db,
        idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
    ) -> dict[str, Any]:
        """envelope 를 등록한다. 같은 키·같은 payload 는 기존 job 을 돌려주고(replay) 다르면 409 다."""
        if idempotency_key is None:
            raise CollectorContractError("Idempotency-Key header is required")
        envelope = await _read_envelope(request)
        result = await submit_envelope(db, ctx, idempotency_key, envelope)
        await db.commit()
        return {
            "job_id": str(result.job_id),
            "result": result.status,
            "payload_hash": result.payload_hash,
            "envelope_id": str(result.envelope_id),
            "request_key": result.request_key,
        }

    @r.get("/jobs")
    async def list_jobs(
        ctx: Annotated[AuthContext, Depends(access)],
        db: Db,
        cursor: Annotated[str | None, Query(max_length=300)] = None,
        limit: Annotated[int, Query()] = 20,
    ) -> dict[str, Any]:
        rows, next_cursor = await jobs.list_jobs(db, ctx.tenant_id, ctx.scope, limit=limit, cursor=cursor)
        return {"jobs": [_view(j) for j in rows], "next_cursor": next_cursor}

    @r.get("/jobs/{job_id}")
    async def get_job(job_id: uuid.UUID, ctx: Annotated[AuthContext, Depends(access)], db: Db) -> dict[str, Any]:
        return _view(await jobs.get_job(db, ctx.tenant_id, ctx.scope, job_id))

    @r.post("/jobs/{job_id}/cancel")
    async def cancel_job(job_id: uuid.UUID, ctx: Annotated[AuthContext, Depends(access)], db: Db) -> dict[str, Any]:
        outcome = await jobs.cancel(db, ctx.tenant_id, ctx.scope, job_id)
        await db.commit()
        return {"job_id": str(job_id), "status": outcome}


router = APIRouter(prefix="/api/assistant/collect", tags=["collect"])
agent_router = APIRouter(prefix="/api/agent/collect", tags=["collect"])
_routes(router, user_access)
_routes(agent_router, agent_access)
