from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from ttakkari.api import (
    accounts,
    agent,
    artifacts,
    assistant,
    auth,
    orchestrator,
    planner,
    push,
    sessions,
    system,
    transfers,
    workspaces,
)
from ttakkari.artifacts import preview
from ttakkari.config import get_settings
from ttakkari.context.embedder import warmup as embedder_warmup
from ttakkari.context.indexer import index_all
from ttakkari.db import dispose_engine, init_engine, sessionmaker
from ttakkari.planner import kr_holidays
from ttakkari.runs.manager import (
    expire_artifacts,
    manager,
    recover_interrupted,
    resume_queued,
)
from ttakkari.transfer import service as transfer_service

log = logging.getLogger("ttakkari")
RETENTION_INTERVAL = 3600


async def _retention_loop() -> None:
    while True:
        with contextlib.suppress(Exception):
            transfer_service.sweep_stale()
        with contextlib.suppress(Exception):
            async with sessionmaker()() as db:
                # 바뀐 파일만 다시 임베딩하므로 매시간 돌려도 싸다(ADR 0007).
                await index_all(db)
        with contextlib.suppress(Exception):
            n = await expire_artifacts()
            if n:
                log.info("expired %d artifacts", n)
        await asyncio.sleep(RETENTION_INTERVAL)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    s = get_settings()
    s.ensure_dirs()
    init_engine()
    n = await recover_interrupted()
    if n:
        log.warning("marked %d runs as interrupted after restart", n)
    await resume_queued()
    try:
        async with sessionmaker()() as db:
            await workspaces.ensure_default_workspace(db)
    except Exception as e:  # noqa: BLE001
        # 기본 경로가 없어도 서버는 뜬다. 프론트가 /api/workspaces/default 로 다시 시도하며 오류를 보여 준다.
        log.warning("default workspace not created: %s", e)
    await preview.start_worker()
    embedder_warmup()
    kr_holidays.warm(datetime.now(UTC).year)
    retention = asyncio.create_task(_retention_loop())
    try:
        yield
    finally:
        retention.cancel()
        await manager.shutdown()
        await preview.stop_worker()
        await dispose_engine()


def create_app() -> FastAPI:
    s = get_settings()
    app = FastAPI(title="Winterholic Ttakkari", version="0.1.0", lifespan=lifespan,
                  docs_url=None if s.is_prod else "/docs", redoc_url=None, openapi_url=None if s.is_prod else "/openapi.json")
    app.add_middleware(
        CORSMiddleware, allow_origins=s.cors_origins, allow_credentials=True,
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE"], allow_headers=["Authorization", "Content-Type", "Last-Event-ID", "Range"],
        expose_headers=["Content-Disposition", "Content-Length", "Content-Range", "Accept-Ranges"],
    )

    @app.exception_handler(HTTPException)
    async def _http_exc(_: Request, exc: HTTPException) -> JSONResponse:
        # 오류 응답 형식을 {code, message} 하나로 고정한다(프론트 계약).
        detail = exc.detail
        if isinstance(detail, dict):
            body = {"code": detail.get("code", "error"), "message": detail.get("message", "")}
        else:
            body = {"code": {401: "unauthorized", 403: "forbidden", 404: "not_found", 409: "conflict",
                             429: "rate_limited"}.get(exc.status_code, "error"), "message": str(detail)}
        return JSONResponse(body, status_code=exc.status_code, headers=exc.headers)

    @app.exception_handler(RequestValidationError)
    async def _validation_exc(_: Request, exc: RequestValidationError) -> JSONResponse:
        errors = [{"loc": list(e.get("loc", [])), "msg": e.get("msg", "")} for e in exc.errors()]
        return JSONResponse({"code": "validation", "message": "요청 형식이 올바르지 않습니다.", "errors": errors},
                            status_code=422)

    for r in (auth.router, workspaces.router, sessions.router, artifacts.router, system.router, push.router,
              orchestrator.router, accounts.router, transfers.router, transfers.tokens, agent.router, assistant.router, planner.router):
        app.include_router(r)
    return app


app = create_app()
