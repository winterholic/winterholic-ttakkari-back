"""GitHub 탐색 API(소유자 전용, 읽기 전용). 설계: docs/adr/0042-github-explorer.md. 에이전트/MCP 라우터는 두지 않는다."""
from __future__ import annotations

from fastapi import APIRouter, Depends, FastAPI, Request
from fastapi.responses import JSONResponse

from ttakkari.github import (
    routes_actions,
    routes_code,
    routes_identity,
    routes_issues,
    routes_pulls,
)
from ttakkari.github.errors import GithubError
from ttakkari.security.auth import require_user

router = APIRouter(prefix="/api/github", tags=["github"], dependencies=[Depends(require_user)])
for _r in (routes_identity.router, routes_code.router, routes_pulls.router, routes_issues.router, routes_actions.router):
    router.include_router(_r)


async def _github_error(_: Request, exc: GithubError) -> JSONResponse:
    """기본 HTTPException 핸들러는 code·message 만 남기므로 reason·reset·retry_after 를 살리려고 별도로 응답한다."""
    return JSONResponse(exc.detail(), status_code=exc.status, headers=exc.headers())


def install(app: FastAPI) -> None:
    app.add_exception_handler(GithubError, _github_error)
