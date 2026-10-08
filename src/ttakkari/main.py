from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from ttakkari.api import artifacts, auth, sessions, system, workspaces
from ttakkari.artifacts import preview
from ttakkari.config import get_settings
from ttakkari.db import dispose_engine, init_engine
from ttakkari.runs.manager import expire_artifacts, manager, recover_interrupted

log = logging.getLogger("ttakkari")
RETENTION_INTERVAL = 3600


async def _retention_loop() -> None:
    while True:
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
    await preview.start_worker()
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
        allow_methods=["GET", "POST", "PATCH", "DELETE"], allow_headers=["Authorization", "Content-Type", "Last-Event-ID"],
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

    for r in (auth.router, workspaces.router, sessions.router, artifacts.router, system.router):
        app.include_router(r)
    return app


app = create_app()
