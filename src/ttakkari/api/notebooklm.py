"""NotebookLM REST(ADR 0045). 사용자(`/api/notebooklm`)와 에이전트 MCP(`/api/agent/notebooklm`, 개인 세션만)가 같은 서비스를 쓴다.

두 라우터는 같은 핸들러를 공유하고, 에이전트 쪽은 로그인 창·세션 삭제처럼 사람이 해야 하는 일을 노출하지 않는다.
모든 오류는 {code, message[, retry_after]} 로 내려가고 쿠키·토큰·상류 응답 원문은 담기지 않는다.
"""

from __future__ import annotations

import hashlib
import uuid
from pathlib import Path
from typing import Annotated, Any

from fastapi import (
    APIRouter,
    Depends,
    FastAPI,
    File,
    Form,
    HTTPException,
    Request,
    Response,
    UploadFile,
    status,
)
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from ttakkari.api.deps import Db
from ttakkari.config import get_settings
from ttakkari.models import ChatSession
from ttakkari.notebooklm import errors, redact, session
from ttakkari.notebooklm.export import build_ledger_export
from ttakkari.notebooklm.service import DEFAULT_ACCOUNT, service
from ttakkari.orchestrator.runtime import OrchestratorSession
from ttakkari.security.auth import User


async def user_access(_: User) -> None:
    pass


async def agent_access(sid: OrchestratorSession, db: Db) -> None:
    session_row = await db.get(ChatSession, sid)
    if session_row is None:
        raise HTTPException(403, "세션이 없습니다.")
    if session_row.pool == "work":
        raise HTTPException(403, "개인 세션에서만 NotebookLM을 쓸 수 있습니다.")


class TitleIn(BaseModel):
    title: str = Field(min_length=1, max_length=200)


class UrlSourceIn(BaseModel):
    url: str = Field(min_length=1, max_length=2000)
    title: str | None = Field(default=None, max_length=200)
    force: bool = False


class TextSourceIn(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    text: str = Field(min_length=1, max_length=500_000)
    force: bool = False


class QueryIn(BaseModel):
    question: str = Field(min_length=1, max_length=8000)
    conversation_id: str | None = Field(default=None, max_length=100)
    source_ids: list[str] | None = Field(default=None, max_length=100)
    export_for_ledger: bool = False


def _handler(request: Request, exc: errors.NotebookLMError) -> JSONResponse:
    body: dict[str, Any] = {"code": exc.code, "message": redact.mask(exc.message)}
    headers = {}
    if exc.retry_after:
        body["retry_after"] = exc.retry_after
        headers["Retry-After"] = str(exc.retry_after)
    if exc.outcome_unknown:
        body["outcome_unknown"] = True
    return JSONResponse(body, status_code=exc.status, headers=headers)


def install(app: FastAPI) -> None:
    app.add_exception_handler(errors.NotebookLMError, _handler)  # type: ignore[arg-type]


def _routes(r: APIRouter, *, agent: bool) -> None:
    @r.get("/auth/status")
    async def auth_status() -> dict[str, Any]:
        """마지막으로 알려진 연결 상태. 브라우저를 띄우거나 Google 에 요청하지 않는다."""
        return await service.status(DEFAULT_ACCOUNT)

    if not agent:
        @r.post("/auth/login-window", status_code=status.HTTP_202_ACCEPTED)
        async def login_window() -> dict[str, Any]:
            """Mac 화면에 전용 프로필 Chrome 창을 연다. 사람이 그 창에서 Google 로그인을 한 번 한다."""
            return await service.start_login(DEFAULT_ACCOUNT)

        @r.post("/auth/login-window/cancel")
        async def login_cancel() -> dict[str, Any]:
            return await service.cancel_login(DEFAULT_ACCOUNT)

        @r.post("/auth/resume")
        async def auth_resume() -> dict[str, Any]:
            """보호 신호로 멈춘 상태(blocked)를 사람이 직접 확인한 뒤 풀어 준다."""
            return await service.resume(DEFAULT_ACCOUNT)

        @r.delete("/auth/session")
        async def auth_delete_session() -> dict[str, Any]:
            """저장된 세션 파일을 지운다. Google 계정에서 로그아웃하는 것은 아니다."""
            return await service.delete_session(DEFAULT_ACCOUNT)

    @r.post("/auth/verify")
    async def auth_verify() -> dict[str, Any]:
        """저장된 세션으로 NotebookLM 접속을 한 번 확인한다(Google 요청 1회, 짧은 간격 제한)."""
        return await service.verify(DEFAULT_ACCOUNT)

    @r.get("/notebooks")
    async def notebooks_list() -> list[dict[str, Any]]:
        return [n.model_dump() for n in await service.list_notebooks()]

    @r.post("/notebooks", status_code=status.HTTP_201_CREATED)
    async def notebooks_create(body: TitleIn) -> dict[str, Any]:
        return (await service.create_notebook(body.title)).model_dump()

    @r.get("/notebooks/{notebook_id}")
    async def notebooks_get(notebook_id: str) -> dict[str, Any]:
        return (await service.get_notebook(notebook_id)).model_dump()

    @r.patch("/notebooks/{notebook_id}")
    async def notebooks_rename(notebook_id: str, body: TitleIn) -> dict[str, Any]:
        return (await service.rename_notebook(notebook_id, body.title)).model_dump()

    @r.delete("/notebooks/{notebook_id}", status_code=status.HTTP_204_NO_CONTENT)
    async def notebooks_delete(notebook_id: str) -> Response:
        await service.delete_notebook(notebook_id)
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    @r.get("/notebooks/{notebook_id}/sources")
    async def sources_list(notebook_id: str) -> list[dict[str, Any]]:
        return [s.model_dump() for s in await service.list_sources(notebook_id)]

    def _added(out: dict[str, Any], response: Response) -> dict[str, Any]:
        response.status_code = status.HTTP_202_ACCEPTED if out.get("job") else status.HTTP_200_OK
        return out

    @r.post("/notebooks/{notebook_id}/sources/url")
    async def sources_add_url(notebook_id: str, body: UrlSourceIn, response: Response) -> dict[str, Any]:
        """웹페이지·YouTube URL. 등록과 처리 대기는 job 으로 진행한다. 이미 등록된 URL 이면 job 없이 duplicate=true."""
        return _added(await service.add_source(notebook_id, "url", url=body.url, title=body.title, force=body.force),
                      response)

    @r.post("/notebooks/{notebook_id}/sources/text")
    async def sources_add_text(notebook_id: str, body: TextSourceIn, response: Response) -> dict[str, Any]:
        return _added(await service.add_source(notebook_id, "text", title=body.title, text=body.text, force=body.force),
                      response)

    if not agent:
        @r.post("/notebooks/{notebook_id}/sources/upload")
        async def sources_upload(
            notebook_id: str, response: Response, file: Annotated[UploadFile, File()],
            title: Annotated[str | None, Form()] = None, force: Annotated[bool, Form()] = False,
        ) -> dict[str, Any]:
            """파일 업로드(상류 지원: PDF·문서 등). 크기 상한을 넘으면 413. 임시 파일은 job 이 끝나면 지운다."""
            s = get_settings()
            name = Path(file.filename or "upload").name[:120] or "upload"
            folder = session.uploads_dir() / uuid.uuid4().hex
            folder.mkdir(parents=True, exist_ok=True)
            folder.chmod(0o700)
            path = folder / name
            digest, size = hashlib.sha256(), 0
            try:
                with path.open("wb") as f:
                    while chunk := await file.read(1024 * 1024):
                        size += len(chunk)
                        if size > s.notebooklm_upload_max_bytes:
                            raise HTTPException(413,
                                                {"code": "too_large", "message": "파일이 너무 큽니다."})
                        digest.update(chunk)
                        f.write(chunk)
                if size == 0:
                    raise errors.Invalid("빈 파일은 등록할 수 없습니다.")
            except BaseException:
                import shutil

                shutil.rmtree(folder, ignore_errors=True)
                raise
            return _added(await service.add_source(
                notebook_id, "file", file_path=path, file_sha256=digest.hexdigest(), title=title or name, force=force),
                response)

    @r.delete("/notebooks/{notebook_id}/sources/{source_id}", status_code=status.HTTP_204_NO_CONTENT)
    async def sources_delete(notebook_id: str, source_id: str) -> Response:
        await service.delete_source(notebook_id, source_id)
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    @r.post("/notebooks/{notebook_id}/query")
    async def notebooks_query(notebook_id: str, body: QueryIn) -> dict[str, Any]:
        """질의. answer(NotebookLM 분석)·citations(인용)·sources(출처)를 분리해 돌려준다.
        export_for_ledger=true 면 지식 원장용 구조(원문 발췌/분석/인용 구분, 자동 승격 없음)를 함께 준다."""
        result = await service.query(notebook_id, body.question, conversation_id=body.conversation_id,
                                     source_ids=body.source_ids)
        out = result.model_dump()
        if body.export_for_ledger:
            out["ledger_export"] = build_ledger_export(result)
        return out

    @r.get("/jobs")
    async def jobs_list(limit: int = 20) -> list[dict[str, Any]]:
        return await service.list_jobs(DEFAULT_ACCOUNT, limit)

    @r.get("/jobs/{job_id}")
    async def jobs_get(job_id: str) -> dict[str, Any]:
        return await service.get_job(job_id, DEFAULT_ACCOUNT)

    @r.post("/jobs/{job_id}/cancel")
    async def jobs_cancel(job_id: str) -> dict[str, Any]:
        return await service.cancel_job(job_id, DEFAULT_ACCOUNT)


router = APIRouter(prefix="/api/notebooklm", tags=["notebooklm"], dependencies=[Depends(user_access)])
agent_router = APIRouter(prefix="/api/agent/notebooklm", tags=["notebooklm"], dependencies=[Depends(agent_access)])
_routes(router, agent=False)
_routes(agent_router, agent=True)
