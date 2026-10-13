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
    clovanote,
    desk_feedback,
    library_insight,
    meetings,
    orchestrator,
    planner,
    pms,
    push,
    sessions,
    system,
    tasks,
    transfers,
    workspaces,
)
from ttakkari.artifacts import preview
from ttakkari.config import get_settings
from ttakkari.context.indexer import index_all
from ttakkari.db import dispose_engine, init_engine, sessionmaker
from ttakkari.meetings import clova_jobs
from ttakkari.meetings import vectors as meeting_vectors
from ttakkari.planner import kr_holidays, reminders
from ttakkari.runs import desk_pool, lead_archive, profiles, usage_store
from ttakkari.runs.manager import (
    expire_artifacts,
    manager,
    recover_interrupted,
    resume_interrupted,
    resume_queued,
)
from ttakkari.runs.usage_collector import Collector
from ttakkari.transfer import service as transfer_service

log = logging.getLogger("ttakkari")
RETENTION_INTERVAL = 3600


async def _retention_loop() -> None:
    while True:
        with contextlib.suppress(Exception):
            transfer_service.sweep_stale()
        with contextlib.suppress(Exception):
            async with sessionmaker()() as db:
                await index_all(db)
        with contextlib.suppress(Exception):
            n = await expire_artifacts()
            if n:
                log.info("expired %d artifacts", n)
        await asyncio.sleep(RETENTION_INTERVAL)


async def _reminder_loop(interval: int) -> None:
    # 시작하자마자 한 번 훑어 꺼져 있던 사이에 지난 알림을 따라잡는다(planner/reminders.py).
    while True:
        try:
            await reminders.send_due()
        except Exception as e:  # noqa: BLE001
            log.warning("reminder scan failed: %s", e)
        await asyncio.sleep(interval)


async def _lead_archive_loop(interval: int) -> None:
    while True:
        try:
            await lead_archive.archive_idle_leads()
        except Exception as e:  # noqa: BLE001
            log.warning("lead archive scan failed: %s", e)
        await asyncio.sleep(interval)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    s = get_settings()
    s.ensure_dirs()
    init_engine()
    n = await recover_interrupted()
    if n:
        log.warning("marked %d runs as interrupted after restart", n)
    # 한도 상태를 되살린 뒤에 대기열을 돌려야 초기화 전 계정을 다시 두드리지 않는다.
    with contextlib.suppress(Exception):
        async with sessionmaker()() as db:
            await profiles.load_cooldowns(db)
    # 계정 사용량: 재시작 직후에도 마지막 측정값을 보이고, 새 값은 DB 에 쌓고, 주기 수집을 돌린다.
    usage_store.install()
    with contextlib.suppress(Exception):
        async with sessionmaker()() as db:
            await usage_store.load_latest(db)
    n = await resume_interrupted()
    if n:
        log.warning("queued %d interrupted runs for bounded restart recovery", n)
    await resume_queued()
    try:
        async with sessionmaker()() as db:
            await workspaces.ensure_default_workspace(db)
    except Exception as e:  # noqa: BLE001
        # 기본 경로가 없어도 서버는 뜬다. 프론트가 /api/workspaces/default 로 다시 시도하며 오류를 보여 준다.
        log.warning("default workspace not created: %s", e)
    await preview.start_worker()
    kr_holidays.warm(datetime.now(UTC).year)
    # 회의록 벡터 색인(끊긴 것·아직 없는 것)과 클로바노트 작업 큐(ADR 0021)
    with contextlib.suppress(Exception):
        n = await meeting_vectors.resume_pending()
        if n:
            log.info("meeting vectors: %d meetings queued for indexing", n)
    await clova_jobs.start()
    retention = asyncio.create_task(_retention_loop())
    reminder = asyncio.create_task(_reminder_loop(s.reminder_scan_seconds)) if s.reminder_scan_seconds > 0 else None
    collector = asyncio.create_task(Collector().run()) if s.usage_poll_seconds > 0 else None
    lead_sweeper = (asyncio.create_task(_lead_archive_loop(s.lead_archive_scan_seconds))
                    if s.lead_archive_scan_seconds > 0 else None)
    try:
        yield
    finally:
        retention.cancel()
        if collector:
            collector.cancel()
        if reminder:
            reminder.cancel()
        if lead_sweeper:
            lead_sweeper.cancel()
        await clova_jobs.stop()
        await manager.shutdown()
        await desk_pool.shutdown()
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
              orchestrator.router, accounts.router, transfers.router, transfers.tokens, agent.router, assistant.router, library_insight.router, planner.router,
              meetings.router, meetings.agent_router, clovanote.router, clovanote.agent_router, pms.router, tasks.router, desk_feedback.router):
        app.include_router(r)
    return app


app = create_app()
