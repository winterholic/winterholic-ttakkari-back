"""ASGI lifecycle for opt-in generation mode. Standby has no recovery sweep."""
from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager

from sqlalchemy import select

from ttakkari import generation
from ttakkari.config import get_settings
from ttakkari.db import dispose_engine, sessionmaker
from ttakkari.models import (
    TERMINAL_RUN_STATUSES,
    ChatSession,
    Meeting,
    Run,
    RuntimeCommand,
    utcnow,
)

log = logging.getLogger(__name__)


async def apply_reset(db, sess) -> None:
    from ttakkari.runs import context_usage
    commands = list(await db.scalars(select(RuntimeCommand).where(
        RuntimeCommand.kind == "reset", RuntimeCommand.session_id == sess.id,
        RuntimeCommand.acknowledged_at.is_(None))))
    if commands:
        context_usage.reset_engine_sessions(sess)
        for command in commands:
            command.acknowledged_at = utcnow()


async def _commands(runtime, manager) -> None:
    for command in await runtime.commands():
        if command.kind == "cancel":
            if command.run_id in manager._handles:
                await manager.cancel(command.run_id)
                async with sessionmaker()() as db:
                    run = await db.get(Run, command.run_id)
                    if run is None or run.status in TERMINAL_RUN_STATUSES:
                        await runtime.ack(command.id)
            else:
                async with sessionmaker()() as db:
                    run = await db.get(Run, command.run_id)
                    if run is None or run.status not in {"queued", "running"}:
                        await runtime.ack(command.id)
                    elif run.status == "queued" and not await manager.is_owned(run.id):
                        await manager.cancel_queued(db, run)
                        await db.commit()
                        await runtime.ack(command.id)
        elif command.kind == "reset":
            if command.session_id:
                manager._reset_requests.add(command.session_id)
                async with sessionmaker()() as db:
                    sess = await db.get(ChatSession, command.session_id)
                    if sess and not await db.scalar(select(Run.id).where(
                            Run.session_id == sess.id, Run.status == "running").limit(1)):
                        await apply_reset(db, sess)
                        await db.commit()
        elif command.kind == "release_waiting":
            await manager.release_waiting()
            await runtime.ack(command.id)


async def coordinate_runs(runtime, manager, state) -> None:
    if manager.remote:
        return
    await _commands(runtime, manager)
    if state == "active" and not getattr(runtime, "draining", False):
        from ttakkari.runs.manager import resume_queued
        await resume_queued()


async def coordinate(runtime, manager) -> None:
    from ttakkari import background_consumers, queued_work
    from ttakkari.runs import desk_pool, profiles, usage_store
    previous = "standby"
    while True:
        try:
            runtime.signal.clear()
            await runtime.listen()
            state = await runtime.refresh()
            if state == "draining" and previous != state:
                if not manager.remote:
                    # 이 세대가 소비자를 돌리는 local 모드다. 이미 맡은 일은 두고 새로 가져가는 것만 멈춘다.
                    queued_work.begin_drain()
                    background_consumers.consumers.pause_claiming()
                # In-use exchanges were popped out of _warm; this only stops idle/refill.
                await desk_pool.shutdown()
            elif state == "active" and previous == "draining" and not manager.remote:
                # 롤백으로 퇴역 중이던 세대가 다시 active 가 됐다.
                queued_work.reopen()
                background_consumers.consumers.resume_claiming()
            previous = state
            if state == "active":
                async with sessionmaker()() as db:
                    await profiles.load_cooldowns(db)
                    await usage_store.load_latest(db, refresh=True)
            await coordinate_runs(runtime, manager, state)
            if state == "active" and not manager.remote and not getattr(runtime, "draining", False):
                from ttakkari.schedules import process_due_schedules
                # work 잠금으로 감싸 주기 작업(schedules)과 겹치지 않게 하고, 실행 중에는 세대가 퇴역하지 않게 한다.
                async with generation.work("schedules") as acquired:
                    if acquired:
                        await process_due_schedules(owner_id=f"gen-{runtime.id}")
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("generation coordinator failed; retrying without stealing work")
        try:
            await asyncio.wait_for(runtime.signal.wait(), get_settings().runtime_poll_seconds)
        except TimeoutError:
            pass


async def periodic(name, operation, interval) -> None:
    while True:
        try:
            async with generation.work(name) as acquired:
                if acquired:
                    await operation()
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("generation maintenance failed: %s", name)
        await asyncio.sleep(interval)


async def consume(name, next_job, execute) -> None:
    """A durable singleton queue; a transient DB error must not kill its consumer."""
    while True:
        worked = False
        try:
            async with generation.work(name) as acquired:
                if acquired and (job_id := await next_job()):
                    await execute(job_id)
                    worked = True
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("generation queue failed: %s", name)
        if not worked:
            await asyncio.sleep(get_settings().runtime_poll_seconds)


async def _retention() -> None:
    from ttakkari.context.indexer import index_all
    from ttakkari.runs.manager import expire_artifacts
    from ttakkari.sharing.service import cleanup
    from ttakkari.transfer.service import sweep_stale
    sweep_stale()
    async with sessionmaker()() as db:
        await index_all(db)
    await expire_artifacts()
    await cleanup()


async def _reminders() -> None:
    from ttakkari.planner import checklist_reminders, reminders
    await reminders.send_due()
    await checklist_reminders.send_due()


async def _notebooklm_jobs() -> None:
    from ttakkari.config import get_settings
    from ttakkari.notebooklm.service import service
    # 이전 세대가 아직 돌리는 job 을 건드리지 않도록 하트비트가 오래된 것만 정리한다.
    await service.recover_jobs(stale_seconds=get_settings().notebooklm_job_stale_seconds)


async def _weather() -> None:
    from ttakkari.weather import service
    await service.scan()


async def _vectors() -> None:
    from ttakkari.meetings.vectors import reindex
    async with sessionmaker()() as db:
        mid = await db.scalar(select(Meeting.id).where(
            Meeting.vector_status.in_(("pending", "none")),
            (Meeting.transcript.is_not(None)) | (Meeting.summary.is_not(None)))
            .order_by(Meeting.created_at).limit(1))
    if mid:
        await reindex(mid)


async def _schedules() -> None:
    from ttakkari.schedules import process_due_schedules
    owner = f"gen-{generation.current.id if generation.current else 'single'}"
    await process_due_schedules(owner_id=owner)


async def _collector_consume() -> None:
    from ttakkari.collector import pipeline
    from ttakkari.collector.runner import consume_once
    pipeline.install()
    await consume_once()


async def _collector_control() -> None:
    from ttakkari import kb_delivery as delivery
    from ttakkari.collector import budget, control, pilot, pipeline, runtime
    from ttakkari.collector.runner import consume_once
    async with sessionmaker()() as db:
        paused = await runtime.is_paused(db)
        if not paused and get_settings().collector_pilot_enabled:
            await pilot.seed(db)
        await db.commit()
    if not paused:
        await control.schedule_once()
        pipeline.install()
        await consume_once(limit=1, managed_only=True)
    # A pause stops new fetching only. Already collected items still reach their permitted sink, which re-checks rights.
    await delivery.deliver_once()
    await budget.sweep_orphans()


async def _creative_assets() -> None:
    from ttakkari.creative.v2 import asset_worker
    while await asset_worker.consume_once():
        pass


async def _game_builds() -> None:
    from ttakkari.games import builds
    while await builds.consume_queued_build():
        pass


async def _kb_vectors() -> None:
    from ttakkari.kb_retrieval import index_pending
    await index_pending(limit=8)


async def start_api_services(execution_mode: str) -> None:
    """Start request-local API services and local-mode durable workers."""
    from ttakkari import background_consumers
    from ttakkari.artifacts import preview
    from ttakkari.meetings.local_stt import jobs as stt_jobs
    from ttakkari.runs.desk_qwen_service import service as qwen_service
    from ttakkari.tts import jobs as tts_jobs

    if execution_mode == "local":
        await preview.start_worker()
        await stt_jobs.start()
        await tts_jobs.start()
        await background_consumers.start()
    # Remote API generations do not spawn or stop the llama-server process;
    # only local (single-process legacy/rollback) runs own the managed process.
    await qwen_service.start(owner=(execution_mode == "local"))


async def ready(runtime) -> dict:
    from ttakkari import background_consumers, queued_work
    from ttakkari.api.assistant import _tasks as indexing
    from ttakkari.meetings.vectors import _pending
    from ttakkari.runs import usage_store
    from ttakkari.runs.manager import _background, manager
    value = await runtime.snapshot()
    pending = sum(not t.done() for t in (*_background, *indexing, *usage_store._tasks, *_pending.values()))
    # 로컬 API 가 소비자로 이미 맡은 일(NotebookLM/Reverse/색인 task, claim 중인 pump, Creative/게임 job).
    consumer_work = background_consumers.busy() + queued_work.inflight()
    value["finalizers"] = pending
    value["consumer_work"] = consumer_work
    value["drain_ready"] = await runtime.can_retire() and not (manager._handles or pending or consumer_work)
    value["ready"] = runtime._listener is not None and not runtime._listener.is_closed()
    return value


def maintenance_jobs(s) -> list[tuple[str, object, float]]:
    """주기 작업 목록. 운영은 이 경로로만 도니 새 주기 작업(날씨 scan 등)은 여기에 등록한다."""
    from ttakkari.creative.render import scan as creative_scan
    from ttakkari.runs import lead_archive
    from ttakkari.runs.usage_collector import TICK_SECONDS, Collector

    jobs: list[tuple[str, object, float]] = [
        ("retention", _retention, 3600), ("vectors", _vectors, s.runtime_poll_seconds),
        ("notebooklm-jobs", _notebooklm_jobs, 60), ("creative-render", creative_scan, 2),
        ("schedules", _schedules, s.runtime_poll_seconds),
        # 원격 API 가 접수한 일을 순차로 끝낸다. 로컬 모드 API 도 같은 주기 작업을 돌려 모드 전환 때 대기 요청이 남지 않는다.
        ("creative-assets", _creative_assets, s.runtime_poll_seconds), ("game-builds", _game_builds, s.runtime_poll_seconds)]
    if s.collector_consume_enabled:
        jobs.append(("collector-consume", _collector_consume, s.runtime_poll_seconds))
    if s.collector_control_enabled:
        jobs.append(("collector-control", _collector_control, max(10, s.runtime_poll_seconds)))
    if s.kb_semantic_enabled:
        jobs.append(("kb-vectors", _kb_vectors, 60))
    if s.reminder_scan_seconds > 0:
        jobs += [("reminders", _reminders, s.reminder_scan_seconds), ("weather", _weather, s.reminder_scan_seconds)]
    if s.usage_poll_seconds > 0:
        jobs.append(("usage", Collector().tick, TICK_SECONDS))
    if s.qwen_flash_poll_seconds > 0:
        from ttakkari.runs.qwen_flash_monitor import collect_once
        jobs.append(("qwen-flash-usage", collect_once, float(s.qwen_flash_poll_seconds)))
    if s.resource_sample_seconds > 0:
        from ttakkari.resource_history import collect_once
        jobs.append(("resource-history", collect_once, s.resource_sample_seconds))
    if s.lead_archive_scan_seconds > 0:
        jobs.append(("archive", lead_archive.archive_idle_leads, s.lead_archive_scan_seconds))
    if s.ops_log_sync_seconds > 0:
        from ttakkari.oplog.ingest import sync_all
        jobs.append(("ops-logs", sync_all, s.ops_log_sync_seconds))
    return jobs


def start_legacy_queued_consumers(interval: float) -> list[asyncio.Task]:
    """runtime_generation=false 로 내려도 API 가 접수해 둔 Creative 자산·게임 빌드 대기분이 남지 않게 한다.

    세대 런타임이 없는 프로세스는 maintenance_jobs 를 돌리지 않는다. 두 소비자는 조건부 claim 이라
    같은 대기분을 실행부와 함께 가져가도 한쪽만 실행한다.
    """
    return [asyncio.create_task(periodic(name, operation, interval), name=f"periodic:{name}")
            for name, operation in (("creative-assets", _creative_assets), ("game-builds", _game_builds))]


SHUTDOWN_DRAIN_SECONDS = 1800.0


async def finish_claimed_work(seconds: float = SHUTDOWN_DRAIN_SECONDS) -> dict[str, bool]:
    """local 모드 종료: 이미 맡은 일을 상한 안에서 자연 완료까지 기다리고, 넘긴 일은 취소했다고 남긴다.

    새 claim 은 호출 전에 이미 닫혀 있어야 한다. 값이 False 인 항목이 상한을 넘겨 취소된 일이다.
    """
    from ttakkari import background_consumers, queued_work
    from ttakkari.tts import jobs as tts_jobs
    loop = asyncio.get_running_loop()
    deadline = loop.time() + seconds

    def left() -> float:
        return max(0.0, deadline - loop.time())

    results: dict[str, bool] = {}
    tts_drain = asyncio.create_task(tts_jobs.drain())
    done, _ = await asyncio.wait({tts_drain}, timeout=left())
    results["tts"] = bool(done)
    if not done:
        # drain() 은 취소를 삼키므로 wait_for 로는 상한 초과를 구분할 수 없다. 진행 중 합성은 이어지는 stop() 이 끊는다.
        tts_drain.cancel()
        await asyncio.gather(tts_drain, return_exceptions=True)
    results["creative_game"] = await queued_work.drain(left())
    results["background"] = await background_consumers.stop(drain_seconds=left())
    if not all(results.values()):
        log.warning("local shutdown cut claimed work after %.0fs: %s", seconds,
                    sorted(name for name, finished in results.items() if not finished))
    return results


@asynccontextmanager
async def lifespan(app):
    from ttakkari import background_consumers, queued_work
    from ttakkari.artifacts import preview
    from ttakkari.deployment import revision
    from ttakkari.meetings.local_stt import jobs
    from ttakkari.runs import desk_pool, profiles, usage_store
    from ttakkari.runs.desk_qwen_service import service as qwen_service
    from ttakkari.runs.manager import manager
    from ttakkari.tts import jobs as tts_jobs
    s = get_settings()
    if s.is_prod and not s.release_revision:
        raise RuntimeError("generation production requires a pinned release revision")
    runtime = generation.GenerationRuntime(s.home, revision=revision(), api_url=s.runtime_api_url)
    tasks = []
    generation.current = runtime
    queued_work.reopen()  # 같은 프로세스에서 lifespan 이 다시 열리면 이전 종료의 claim 차단을 풀어 둔다
    try:
        await runtime.register()
        await runtime.listen()
        app.state.runtime = runtime
        usage_store.install()
        # Reading previous samples is harmless in standby. No recovery/start sweeps.
        async with sessionmaker()() as db:
            await profiles.load_cooldowns(db)
            await usage_store.load_latest(db)
        await start_api_services(runtime.execution_mode)
        tasks.append(asyncio.create_task(coordinate(runtime, manager)))
        if runtime.execution_mode == "local":
            for name, operation, interval in maintenance_jobs(s):
                tasks.append(asyncio.create_task(periodic(name, operation, interval), name=f"periodic:{name}"))
        yield
    finally:
        local = runtime.execution_mode == "local"
        if local:
            # 새 claim 을 먼저 닫는다. 이미 맡은 일은 아래에서 상한 안에 자연 완료를 기다린다.
            queued_work.begin_drain()
            background_consumers.consumers.stop_claiming()
            tts_jobs.stop_claiming()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        if local:
            await jobs.stop()
            await finish_claimed_work()
            await tts_jobs.stop()
        from ttakkari.runs import luna_benchmark
        await luna_benchmark.shutdown()
        from ttakkari.notebooklm.service import service as notebooklm_service
        await notebooklm_service.shutdown()
        await manager.shutdown()
        await desk_pool.shutdown()
        await qwen_service.stop()
        if runtime.execution_mode == "local":
            await preview.stop_worker()
        await profiles.flush_persist()
        await usage_store.flush()
        await runtime.close()
        generation.current = None
        await dispose_engine()


class InflightMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        runtime = generation.current
        path = scope.get("path", "")
        if (scope["type"] == "http" and getattr(scope.get("app").state, "bootstrap_hold", False)
                and path not in {"/api/health", "/api/version", "/api/bootstrap-ready"}):
            from starlette.responses import JSONResponse
            response = JSONResponse({"code": "deploying", "message": "최초 배포 전환 중입니다. 잠시 후 다시 시도해 주세요."},
                                    status_code=503, headers={"Retry-After": "5"})
            await response(scope, receive, send)
            return
        counted = runtime and scope["type"] == "http" and not (
            path in {"/api/health", "/api/version", "/api/ready"} or path.endswith("/stream"))
        if counted:
            runtime.http_inflight += 1
        try:
            await self.app(scope, receive, send)
        finally:
            if counted:
                runtime.http_inflight -= 1
