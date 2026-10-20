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
    agent_pms,
    artcraft,
    artifacts,
    assistant,
    auth,
    checklists,
    deployments,
    design_system,
    desk_feedback,
    games,
    google_accounts,
    host_env,
    library_insight,
    meetings,
    notebooklm,
    notifications,
    oplogs,
    orchestrator,
    planner,
    pms,
    public_shares,
    push,
    remote_desktop,
    research,
    reverse,
    sessions,
    shares,
    stocks,
    system,
    tasks,
    transfers,
    workspaces,
)
from ttakkari.api import (
    github as github_api,
)
from ttakkari.api import (
    weather as weather_api,
)
from ttakkari.artifacts import preview
from ttakkari.config import get_settings
from ttakkari.context.indexer import index_all
from ttakkari.db import dispose_engine, init_engine, sessionmaker
from ttakkari.meetings import vectors as meeting_vectors
from ttakkari.meetings.local_stt import jobs as local_stt_jobs
from ttakkari.planner import checklist_reminders, kr_holidays, reminders
from ttakkari.runs import desk_pool, lead_archive, profiles, usage_store
from ttakkari.runs.manager import (
    expire_artifacts,
    manager,
    recover_interrupted,
    resume_interrupted,
    resume_queued,
)
from ttakkari.runs.usage_collector import Collector
from ttakkari.sharing.middleware import ShareSecurityMiddleware
from ttakkari.sharing.service import cleanup as cleanup_shares
from ttakkari.transfer import diagnostics as transfer_diagnostics
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
        try:
            await cleanup_shares()
        except Exception:  # noqa: BLE001 -- bounded maintenance: log failure without SQL/body/secret
            log.warning("public share cleanup failed")
        await asyncio.sleep(RETENTION_INTERVAL)


async def _reminder_loop(interval: int) -> None:
    # 시작하자마자 한 번 훑어 꺼져 있던 사이에 지난 알림을 따라잡는다(planner/reminders.py).
    while True:
        try:
            await reminders.send_due()
        except Exception as e:  # noqa: BLE001
            log.warning("reminder scan failed: %s", e)
        try:
            await checklist_reminders.send_due()
        except Exception:
            log.exception("checklist reminder scan failed")
        try:
            from ttakkari.weather import service as weather_service
            await weather_service.scan()
        except Exception:
            log.exception("weather briefing scan failed")
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
    from ttakkari.deployment import revision
    app.state.revision = revision()
    init_engine()
    from ttakkari.generation_bootstrap import holding
    app.state.bootstrap_hold = holding()
    if app.state.bootstrap_hold:
        # The old deployer has drained Runs. Keep the interim app passive until
        # its successor installs the immutable generation and proxy.
        try:
            yield
        finally:
            await dispose_engine()
        return
    if s.runtime_generation:
        from ttakkari.generation_service import lifespan as generation_lifespan
        async with generation_lifespan(app):
            yield
        return
    n = await recover_interrupted()
    if n:
        log.warning("marked %d runs as interrupted after restart", n)
    try:
        from ttakkari.reverse import recover_reverse_jobs, resume_waiting_reverse_jobs
        rn = await recover_reverse_jobs()
        if rn:
            log.warning("marked %d reverse jobs as failed after restart", rn)
        wn = await resume_waiting_reverse_jobs()
        if wn:
            log.warning("requeued %d waiting reverse jobs after restart", wn)
    except Exception as e:  # noqa: BLE001 - 리버스 복구 실패가 기동을 막지 않게
        log.warning("reverse recovery failed: %s", e)
    try:
        from ttakkari.notebooklm.service import service as notebooklm_service
        nn = await notebooklm_service.recover_jobs()
        if nn:
            log.warning("marked %d notebooklm jobs as interrupted after restart", nn)
    except Exception as e:  # noqa: BLE001 - NotebookLM 복구 실패가 기동을 막지 않게
        log.warning("notebooklm recovery failed: %s", e)
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
    from ttakkari.runs.desk_qwen_service import service as qwen_service
    await qwen_service.start()
    kr_holidays.warm(datetime.now(UTC).year)
    # 회의록 벡터 색인(끊긴 것·아직 없는 것)과 로컬 전사 큐
    with contextlib.suppress(Exception):
        n = await meeting_vectors.resume_pending()
        if n:
            log.info("meeting vectors: %d meetings queued for indexing", n)
    await local_stt_jobs.start()
    retention = asyncio.create_task(_retention_loop())
    reminder = asyncio.create_task(_reminder_loop(s.reminder_scan_seconds)) if s.reminder_scan_seconds > 0 else None
    collector = asyncio.create_task(Collector().run()) if s.usage_poll_seconds > 0 else None
    from ttakkari import resource_history
    resource_collector = (asyncio.create_task(resource_history.run(s.resource_sample_seconds))
                          if s.resource_sample_seconds > 0 else None)
    lead_sweeper = (asyncio.create_task(_lead_archive_loop(s.lead_archive_scan_seconds))
                    if s.lead_archive_scan_seconds > 0 else None)
    from ttakkari.oplog import ingest as ops_ingest
    ops_sync = asyncio.create_task(ops_ingest.sync_loop(s.ops_log_sync_seconds)) if s.ops_log_sync_seconds > 0 else None
    try:
        yield
    finally:
        if ops_sync:
            ops_sync.cancel()
        retention.cancel()
        if collector:
            collector.cancel()
        if resource_collector:
            resource_collector.cancel()
            await asyncio.gather(resource_collector, return_exceptions=True)
        if reminder:
            reminder.cancel()
        if lead_sweeper:
            lead_sweeper.cancel()
        await local_stt_jobs.stop()
        from ttakkari.runs import luna_benchmark
        await luna_benchmark.shutdown()
        from ttakkari.notebooklm.service import service as notebooklm_service
        await notebooklm_service.shutdown()
        await manager.shutdown()
        await desk_pool.shutdown()
        await qwen_service.stop()
        await preview.stop_worker()
        from ttakkari.security import login_throttle
        await login_throttle.shutdown()
        await dispose_engine()


def create_app() -> FastAPI:
    s = get_settings()
    app = FastAPI(title="Winterholic Ttakkari", version="0.1.0", lifespan=lifespan,
                  docs_url=None if s.is_prod else "/docs", redoc_url=None, openapi_url=None if s.is_prod else "/openapi.json")
    from ttakkari.generation_service import InflightMiddleware
    app.add_middleware(InflightMiddleware)
    app.add_middleware(
        CORSMiddleware, allow_origins=s.cors_origins, allow_credentials=True,
        allow_methods=["HEAD", "GET", "POST", "PUT", "PATCH", "DELETE"], allow_headers=["Authorization", "Content-Type", "Last-Event-ID", "Range", "Idempotency-Key"],
        expose_headers=["Retry-After", "Content-Disposition", "Content-Length", "Content-Range", "Accept-Ranges"],
    )
    app.add_middleware(transfer_diagnostics.TransferDiagnosticsMiddleware)
    app.add_middleware(ShareSecurityMiddleware)
    if s.game_origin_host:
        # 가장 바깥에 둬서 게임 host 요청이 인증·CORS·API 라우트에 닿지 않게 한다(ADR 0049).
        from ttakkari.games.origin import (
            GameOriginMiddleware,
            install_access_log_filter,
        )
        install_access_log_filter()
        app.add_middleware(GameOriginMiddleware, host=s.game_origin_host)

    @app.exception_handler(HTTPException)
    async def _http_exc(request: Request, exc: HTTPException) -> JSONResponse:
        # 오류 응답 형식을 {code, message} 하나로 고정한다(프론트 계약).
        detail = exc.detail
        if isinstance(detail, dict):
            body = {"code": detail.get("code", "error"), "message": detail.get("message", "")}
        else:
            body = {"code": {401: "unauthorized", 403: "forbidden", 404: "not_found", 409: "conflict",
                             429: "rate_limited"}.get(exc.status_code, "error"), "message": str(detail)}
        body.update(transfer_diagnostics.fields(request))
        return JSONResponse(body, status_code=exc.status_code, headers=exc.headers)

    @app.exception_handler(Exception)
    async def _unhandled_exc(request: Request, exc: Exception) -> JSONResponse:
        # 운영 오류 이슈로 남긴다(ADR 0033). 기록 실패는 응답을 막지 않는다.
        from ttakkari.oplog import ingest as ops_ingest
        await ops_ingest.capture_exception(exc, source="api", tags={
            "method": request.method, "path": ops_ingest.safe_path(request.url.path)})
        return JSONResponse({"code": "internal", "message": "서버 오류가 발생했습니다."}, status_code=500)

    @app.exception_handler(RequestValidationError)
    async def _validation_exc(request: Request, exc: RequestValidationError) -> JSONResponse:
        errors = [{"loc": list(e.get("loc", [])), "msg": e.get("msg", "")} for e in exc.errors()]
        return JSONResponse({"code": "validation", "message": "요청 형식이 올바르지 않습니다.", "errors": errors,
                             **transfer_diagnostics.fields(request)},
                            status_code=422)

    for r in (auth.router, workspaces.router, sessions.router, artifacts.router, system.router, push.router,
              orchestrator.router, accounts.router, transfers.router, transfers.tokens, agent.router, assistant.router, library_insight.router, planner.router,
              meetings.router, meetings.agent_router, pms.router, agent_pms.router, tasks.router, desk_feedback.router, deployments.router, host_env.router,
              oplogs.router, oplogs.agent_router, design_system.router, design_system.agent_router, artcraft.router,
              reverse.router, reverse.agent_router):
        app.include_router(r)
    app.include_router(shares.router)
    app.include_router(shares.exports_router)
    app.include_router(public_shares.router)
    app.include_router(checklists.router)
    app.include_router(checklists.agent_router)
    app.include_router(notifications.router)
    app.include_router(weather_api.router)
    app.include_router(stocks.router)
    app.include_router(stocks.agent_router)
    app.include_router(games.router)
    app.include_router(notebooklm.router)
    app.include_router(notebooklm.agent_router)
    notebooklm.install(app)  # code·retry_after·outcome_unknown 을 살리는 NotebookLM 오류 핸들러(ADR 0045)
    app.include_router(google_accounts.router)
    app.include_router(google_accounts.agent_router)
    google_accounts.install(app)  # Google 계정 공통 오류 핸들러(ADR 0051)
    app.include_router(github_api.router)
    app.include_router(remote_desktop.router)
    github_api.install(app)  # reason·reset·retry_after 를 살리는 GithubError 전용 핸들러(ADR 0042)
    from ttakkari.api import knowledge
    app.include_router(knowledge.router)
    app.include_router(knowledge.agent_router)
    app.include_router(research.router)
    app.include_router(research.agent_router)
    return app


app = create_app()
