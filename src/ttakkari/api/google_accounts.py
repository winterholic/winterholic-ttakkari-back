"""Google 계정 REST(ADR 0051). 사용자(`/api/google-accounts`)와 에이전트 MCP(`/api/agent/google-accounts`, 개인 세션만)가 같은 서비스를 쓴다.

에이전트 쪽은 상태 조회만 노출한다. 로그인 창·세션 삭제·재개는 사람이 해야 하는 일이다.
모든 오류는 {code, message[, retry_after]} 로 내려가고 쿠키·토큰·상류 응답 원문은 담기지 않는다.
NotebookLM 의 기존 /api/notebooklm/auth/* 경로는 그대로 두고 같은 서비스로 위임한다.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from ttakkari.api.deps import Db
from ttakkari.google_account import errors, redact
from ttakkari.google_account.service import service
from ttakkari.models import ChatSession
from ttakkari.orchestrator.runtime import OrchestratorSession
from ttakkari.security.auth import User


async def user_access(_: User) -> None:
    pass


async def agent_access(sid: OrchestratorSession, db: Db) -> None:
    session_row = await db.get(ChatSession, sid)
    if session_row is None:
        raise HTTPException(403, "세션이 없습니다.")
    if session_row.pool == "work":
        raise HTTPException(403, "개인 세션에서만 Google 계정 상태를 볼 수 있습니다.")


class LoginIn(BaseModel):
    # 로그인 창이 열 앱 화면을 정하는 기능 ID. 비우면 등록된 첫 기능의 화면을 연다.
    feature: str | None = Field(default=None, max_length=32)


def error_response(request: Request, exc: errors.GoogleAccountError) -> JSONResponse:
    body: dict[str, Any] = {"code": exc.code, "message": redact.mask(exc.message)}
    headers = {}
    if exc.retry_after:
        body["retry_after"] = exc.retry_after
        headers["Retry-After"] = str(exc.retry_after)
    if exc.outcome_unknown:
        body["outcome_unknown"] = True
    return JSONResponse(body, status_code=exc.status, headers=headers)


def install(app: FastAPI) -> None:
    """code·retry_after·outcome_unknown 을 살리는 오류 핸들러. NotebookLM 오류도 같은 뿌리라 함께 처리한다."""
    app.add_exception_handler(errors.GoogleAccountError, error_response)  # type: ignore[arg-type]


def _routes(r: APIRouter, *, agent: bool) -> None:
    @r.get("")
    async def list_accounts() -> dict[str, Any]:
        """계정 목록과 각 계정의 상태·기능별 상태. 브라우저를 띄우거나 Google 에 요청하지 않는다."""
        return {"accounts": await service.list_accounts()}

    @r.get("/{account_id}")
    async def get_account(account_id: str) -> dict[str, Any]:
        return await service.status(account_id)

    if agent:
        return

    @r.post("/{account_id}/login", status_code=status.HTTP_202_ACCEPTED)
    async def login_start(account_id: str, body: LoginIn | None = None) -> dict[str, Any]:
        """Mac 화면에 그 계정 전용 프로필 Chrome 창을 연다. 사람이 그 창에서 Google 로그인을 한 번 한다."""
        return await service.start_login(account_id, feature=body.feature if body else None)

    @r.post("/{account_id}/login/complete")
    async def login_complete(account_id: str) -> dict[str, Any]:
        """로그인 창을 닫고 2단계로 세션 쿠키를 수집·저장한다."""
        return await service.complete_login(account_id)

    @r.post("/{account_id}/login/cancel")
    async def login_cancel(account_id: str) -> dict[str, Any]:
        return await service.cancel_login(account_id)

    @r.delete("/{account_id}/session")
    async def delete_session(account_id: str) -> dict[str, Any]:
        """저장된 세션 파일을 지운다. Google 계정에서 로그아웃하는 것은 아니다."""
        return await service.delete_session(account_id)

    @r.post("/{account_id}/features/{feature}/verify")
    async def feature_verify(account_id: str, feature: str) -> dict[str, Any]:
        """그 기능으로 접속을 한 번 확인한다(Google 요청 1회, 기능별 간격 제한)."""
        return await service.verify(account_id, feature)

    @r.post("/{account_id}/features/{feature}/resume")
    async def feature_resume(account_id: str, feature: str) -> dict[str, Any]:
        """보호 신호로 멈춘 기능(blocked)을 사람이 직접 확인한 뒤 풀어 준다. 다른 기능에는 영향이 없다."""
        return await service.resume(account_id, feature)


router = APIRouter(prefix="/api/google-accounts", tags=["google-accounts"], dependencies=[Depends(user_access)])
agent_router = APIRouter(prefix="/api/agent/google-accounts", tags=["google-accounts"],
                         dependencies=[Depends(agent_access)])
_routes(router, agent=False)
_routes(agent_router, agent=True)
