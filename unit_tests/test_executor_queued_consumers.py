"""실행부가 Creative 자산 생성·게임 빌드 소비자를 주기 작업으로 돌리고, 종료 때 맡은 일을 끝까지 기다리는지 확인한다."""
import asyncio
import os
import signal
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

from ttakkari import executor, generation, generation_service, queued_work


def test_consumers_are_registered_as_maintenance_jobs_with_the_poll_interval():
    settings = SimpleNamespace(
        runtime_poll_seconds=7, collector_consume_enabled=False, collector_control_enabled=False,
        kb_semantic_enabled=False, reminder_scan_seconds=0, usage_poll_seconds=0, qwen_flash_poll_seconds=0,
        resource_sample_seconds=0, lead_archive_scan_seconds=0, ops_log_sync_seconds=0)

    jobs = {name: (operation, interval) for name, operation, interval in generation_service.maintenance_jobs(settings)}

    assert jobs["creative-assets"] == (generation_service._creative_assets, 7)
    assert jobs["game-builds"] == (generation_service._game_builds, 7)


async def test_each_tick_drains_the_backlog_one_job_at_a_time(monkeypatch):
    from ttakkari.creative.v2 import asset_worker
    from ttakkari.games import builds

    results = {"assets": [True, True, False], "builds": [object(), object(), None]}
    calls = {"assets": 0, "builds": 0}

    async def fake_assets():
        calls["assets"] += 1
        return results["assets"].pop(0)

    async def fake_builds():
        calls["builds"] += 1
        return results["builds"].pop(0)

    monkeypatch.setattr(asset_worker, "consume_once", fake_assets)
    monkeypatch.setattr(builds, "consume_queued_build", fake_builds)

    await generation_service._creative_assets()
    await generation_service._game_builds()

    assert calls == {"assets": 3, "builds": 3}


async def test_periodic_scheduler_runs_the_consumer_under_the_executor_work_lock(monkeypatch):
    from ttakkari.games import builds

    seen = asyncio.Event()
    entered = []

    @asynccontextmanager
    async def work(name, *, allow_draining=False):
        entered.append(name)
        yield True

    async def fake_builds():
        seen.set()

    monkeypatch.setattr(generation, "work", work)
    monkeypatch.setattr(builds, "consume_queued_build", fake_builds)
    task = asyncio.create_task(generation_service.periodic("game-builds", generation_service._game_builds, 0.05))
    try:
        await asyncio.wait_for(seen.wait(), 5)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    assert entered[0] == "game-builds"


class _Runtime:
    def __init__(self, *_a, **_k):
        self.signal = asyncio.Event()
        self.id = "executor-test"

    async def register(self, *, activate=False):
        pass

    async def listen(self):
        pass

    async def refresh(self):
        return "active"

    async def close(self):
        pass


async def test_executor_shutdown_waits_for_a_claimed_job_and_stops_new_claims(monkeypatch):
    from ttakkari import background_consumers
    from ttakkari.artifacts import preview
    from ttakkari.meetings.local_stt import jobs as stt_jobs
    from ttakkari.runs import profiles, usage_store
    from ttakkari.runs.desk_qwen_service import service as qwen
    from ttakkari.runs.manager import manager
    from ttakkari.tts import jobs as tts_jobs

    monkeypatch.setattr(tts_jobs, "_draining", False)
    @asynccontextmanager
    async def fake_session():
        yield SimpleNamespace()

    monkeypatch.setattr(executor, "get_settings", lambda: SimpleNamespace(release_revision="rev", runtime_poll_seconds=0.05, home="/tmp"))
    monkeypatch.setattr(executor, "ExecutorRuntime", _Runtime)
    monkeypatch.setattr(executor, "sessionmaker", lambda: fake_session)
    monkeypatch.setattr(executor, "dispose_engine", AsyncMock())
    monkeypatch.setattr(executor, "start_background_services", AsyncMock(return_value=[]))
    monkeypatch.setattr(executor, "drain", AsyncMock(return_value=True))
    monkeypatch.setattr(qwen, "start", AsyncMock())
    monkeypatch.setattr(qwen, "stop", AsyncMock())
    monkeypatch.setattr(background_consumers, "stop", AsyncMock())
    monkeypatch.setattr(generation_service, "coordinate_runs", AsyncMock())
    monkeypatch.setattr("ttakkari.deployment.revision", lambda: "rev")
    for owner, name in ((profiles, "load_cooldowns"), (profiles, "flush_persist"), (usage_store, "load_latest"),
                        (usage_store, "flush"), (stt_jobs, "stop"), (preview, "stop_worker"), (manager, "shutdown")):
        monkeypatch.setattr(owner, name, AsyncMock())
    monkeypatch.setattr(usage_store, "install", lambda: None)
    queued_work.reopen()
    release = asyncio.Event()
    finished = []

    async def long_job():
        await release.wait()
        finished.append(True)

    serving = asyncio.create_task(executor.serve(activate=True))
    await asyncio.sleep(0.2)
    job = asyncio.create_task(queued_work.run_tracked(long_job()))  # 소비자가 맡은 빌드·자산 생성
    await asyncio.sleep(0.05)

    try:
        os.kill(os.getpid(), signal.SIGTERM)  # 배포가 실행부에 보내는 종료 신호
        await asyncio.sleep(0.5)
        assert queued_work.draining() is True  # 새로 맡지 않는다
        assert not serving.done() and finished == []  # 맡은 일이 끝날 때까지 프로세스는 내려가지 않는다
        release.set()
        await asyncio.wait_for(serving, 10)
        await job
        assert finished == [True]
    finally:
        release.set()
        await asyncio.gather(serving, job, return_exceptions=True)
        queued_work.reopen()
        tts_jobs._draining = False
