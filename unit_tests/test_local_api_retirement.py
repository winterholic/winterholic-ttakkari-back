"""Local-mode API retirement and rollback lifecycle. No DB, model servers, host signals or launchd.

The local API (the rollback path of platform v2) owns the background consumers (NotebookLM, Reverse,
manual index) and the Creative/game consumers. A generation may only report ``drain_ready`` once the
work it already claimed has finished, it must stop claiming when it starts draining, and its shutdown
must wait for claimed work (bounded) instead of cutting it with ``stop(0)``.
"""
import asyncio
import inspect
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from ttakkari import background_consumers, generation, generation_service, queued_work


@pytest.fixture(autouse=True)
def _clean_globals():
    queued_work.reopen()
    previous = generation.current
    yield
    queued_work.reopen()
    generation.current = previous


class _RetirableRuntime:
    """Everything the generation itself tracks says it may retire; only the consumers can object."""

    _listener = SimpleNamespace(is_closed=lambda: False)

    async def snapshot(self) -> dict:
        return {"state": "draining"}

    async def can_retire(self) -> bool:
        return True


async def _drain_ready() -> bool:
    return (await generation_service.ready(_RetirableRuntime()))["drain_ready"]


# ------------------------------------------------------------------ (a) ready 가 맡은 일을 포함하는가

async def test_ready_is_blocked_by_inflight_background_consumer_work(monkeypatch):
    active = [1]
    monkeypatch.setattr(background_consumers, "inflight", lambda: active[0])
    assert await _drain_ready() is False
    active[0] = 0
    assert await _drain_ready() is True


async def test_ready_is_blocked_by_inflight_creative_or_game_work():
    release = asyncio.Event()
    job = asyncio.create_task(queued_work.run_tracked(release.wait()))
    await asyncio.sleep(0)
    try:
        assert await _drain_ready() is False
    finally:
        release.set()
        await job
    assert await _drain_ready() is True


async def test_ready_is_blocked_by_a_pump_that_is_still_claiming(monkeypatch):
    entered, release = asyncio.Event(), asyncio.Event()

    async def pump() -> int:
        entered.set()
        await release.wait()  # 행은 claim 했지만 아직 task 로 등록되기 전
        return 0

    consumers = background_consumers.BackgroundConsumers()
    monkeypatch.setattr(background_consumers, "consumers", consumers)
    monkeypatch.setattr(background_consumers, "PUMPS", (("held", pump),))
    monkeypatch.setattr(background_consumers, "inflight", lambda: 0)
    monkeypatch.setattr("ttakkari.reverse.resume_waiting_reverse_jobs", AsyncMock(return_value=0))
    await consumers.start()
    try:
        await asyncio.wait_for(entered.wait(), 5)
        assert await _drain_ready() is False
        release.set()
        consumers.stop_claiming()
        await asyncio.wait_for(consumers.stop(drain_seconds=1), 5)
        assert await _drain_ready() is True
    finally:
        release.set()
        consumers.stop_claiming()
        await consumers.stop(drain_seconds=1)


# ------------------------------------------------------------------ 상태 전환: 새 claim 중단

class _CoordinatorRuntime:
    def __init__(self) -> None:
        self.signal = asyncio.Event()
        self.state = "draining"
        self.id = "api-test"

    async def listen(self) -> None:
        pass

    async def refresh(self) -> str:
        return self.state


async def test_draining_transition_stops_new_claims_and_returning_to_active_resumes_them(monkeypatch):
    from ttakkari.runs import desk_pool, profiles, usage_store
    consumers = background_consumers.BackgroundConsumers()
    monkeypatch.setattr(background_consumers, "consumers", consumers)
    monkeypatch.setattr(generation_service, "coordinate_runs", AsyncMock())
    monkeypatch.setattr(generation_service, "sessionmaker", lambda: lambda: _FakeSession())
    monkeypatch.setattr("ttakkari.schedules.process_due_schedules", AsyncMock())
    for owner, name in ((desk_pool, "shutdown"), (profiles, "load_cooldowns"), (usage_store, "load_latest")):
        monkeypatch.setattr(owner, name, AsyncMock())
    monkeypatch.setattr(generation_service.get_settings(), "runtime_poll_seconds", 0.02)
    runtime = _CoordinatorRuntime()
    task = asyncio.create_task(generation_service.coordinate(runtime, SimpleNamespace(remote=False)))
    try:
        for _ in range(100):
            if queued_work.draining():
                break
            await asyncio.sleep(0.02)
        assert queued_work.draining() is True  # Creative/게임 소비자가 백로그를 더 가져가지 않는다
        assert consumers.claiming_paused is True  # NotebookLM/Reverse/색인 소비자도 마찬가지
        runtime.state = "active"  # 롤백으로 이 세대가 다시 active 가 되면 소비를 재개한다
        runtime.signal.set()
        for _ in range(100):
            if not queued_work.draining():
                break
            await asyncio.sleep(0.02)
        assert queued_work.draining() is False
        assert consumers.claiming_paused is False
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


class _FakeSession:
    async def __aenter__(self):
        return SimpleNamespace()

    async def __aexit__(self, *_exc):
        return None


# ------------------------------------------------------------------ (b) 종료가 맡은 일을 끊지 않는가

@pytest.fixture
def local_lifespan(monkeypatch):
    """generation_service.lifespan 을 DB·모델 없이 local 모드로 돌린다. 종료 단계 호출만 관찰한다."""
    from ttakkari.artifacts import preview
    from ttakkari.meetings.local_stt import jobs as stt_jobs
    from ttakkari.runs import desk_pool, luna_benchmark, profiles, usage_store
    from ttakkari.runs.desk_qwen_service import service as qwen
    from ttakkari.runs.manager import manager
    from ttakkari.tts import jobs as tts_jobs

    class Runtime:
        def __init__(self, *_a, **_k) -> None:
            self.id = "api-local-test"
            self.role, self.execution_mode = "api", "local"
            self.signal = asyncio.Event()
            self.state = "active"
            self.http_inflight = 0

        async def register(self) -> None:
            pass

        async def listen(self) -> None:
            pass

        async def close(self) -> None:
            pass

        @asynccontextmanager
        async def work(self, name, *, allow_draining=False):
            yield True

    async def coordinate_forever(*_a, **_k) -> None:
        await asyncio.Event().wait()

    dispose = AsyncMock()
    monkeypatch.setattr(generation, "GenerationRuntime", Runtime)
    monkeypatch.setattr("ttakkari.deployment.revision", lambda: "rev")
    monkeypatch.setattr(generation_service.get_settings(), "release_revision", "rev")
    monkeypatch.setattr(generation_service, "sessionmaker", lambda: lambda: _FakeSession())
    monkeypatch.setattr(generation_service, "dispose_engine", dispose)
    monkeypatch.setattr(generation_service, "start_api_services", AsyncMock())
    monkeypatch.setattr(generation_service, "coordinate", coordinate_forever)
    monkeypatch.setattr(usage_store, "install", lambda: None)
    for owner, name in ((profiles, "load_cooldowns"), (profiles, "flush_persist"), (usage_store, "load_latest"),
                        (usage_store, "flush"), (stt_jobs, "stop"), (tts_jobs, "stop"), (tts_jobs, "drain"),
                        (luna_benchmark, "shutdown"), (qwen, "stop"), (manager, "shutdown"),
                        (desk_pool, "shutdown"), (preview, "stop_worker")):
        monkeypatch.setattr(owner, name, AsyncMock())
    monkeypatch.setattr("ttakkari.notebooklm.service.service.shutdown", AsyncMock())
    monkeypatch.setattr(tts_jobs, "_draining", False)  # stop_claiming() 이 전역을 바꾸므로 시험 뒤 되돌린다
    monkeypatch.setattr(background_consumers, "consumers", background_consumers.BackgroundConsumers())
    return SimpleNamespace(dispose=dispose, app=SimpleNamespace(state=SimpleNamespace()))


async def test_local_api_shutdown_waits_for_a_claimed_creative_or_game_job(monkeypatch, local_lifespan):
    started, release, finished = asyncio.Event(), asyncio.Event(), []

    async def long_job() -> None:
        await release.wait()
        finished.append(True)

    async def consume() -> None:
        if not started.is_set():
            started.set()
            await queued_work.run_tracked(long_job())

    monkeypatch.setattr(generation_service, "maintenance_jobs", lambda _s: [("creative-assets", consume, 0.01)])
    cm = generation_service.lifespan(local_lifespan.app)
    await cm.__aenter__()
    await asyncio.wait_for(started.wait(), 5)
    leaving = asyncio.create_task(cm.__aexit__(None, None, None))
    try:
        await asyncio.sleep(0.3)
        assert not leaving.done() and finished == []  # 맡은 일이 끝나기 전에는 종료하지 않는다
        local_lifespan.dispose.assert_not_awaited()
        assert queued_work.draining() is True  # 기다리는 동안 새로 맡지 않는다
    finally:
        release.set()
        await asyncio.wait_for(leaving, 10)
    assert finished == [True]


async def test_local_api_shutdown_waits_for_inflight_background_consumer_work(monkeypatch, local_lifespan):
    active = [1]
    cancelled = AsyncMock()
    monkeypatch.setattr(generation_service, "maintenance_jobs", lambda _s: [])
    monkeypatch.setattr(background_consumers, "inflight", lambda: active[0])
    monkeypatch.setattr(background_consumers, "_cancel_inflight", cancelled)
    cm = generation_service.lifespan(local_lifespan.app)
    await cm.__aenter__()
    leaving = asyncio.create_task(cm.__aexit__(None, None, None))
    try:
        await asyncio.sleep(0.4)
        assert not leaving.done()
        cancelled.assert_not_awaited()  # stop(0) 처럼 바로 취소하지 않는다
    finally:
        active[0] = 0
        await asyncio.wait_for(leaving, 10)
    local_lifespan.dispose.assert_awaited_once()


async def test_local_api_shutdown_lets_the_accepted_tts_job_finish_before_the_owner_stop(monkeypatch, local_lifespan):
    from ttakkari.tts import jobs as tts_jobs
    order: list[str] = []
    tts_jobs.drain.side_effect = lambda: order.append("drain")
    tts_jobs.stop.side_effect = lambda: order.append("stop")
    monkeypatch.setattr(generation_service, "maintenance_jobs", lambda _s: [])
    cm = generation_service.lifespan(local_lifespan.app)
    await cm.__aenter__()
    await cm.__aexit__(None, None, None)
    assert order == ["drain", "stop"]


# ------------------------------------------------------------------ 상한 timeout 과 명시적 결과

async def test_background_drain_reports_whether_work_finished_or_was_cancelled(monkeypatch):
    cancel = AsyncMock()
    monkeypatch.setattr(background_consumers, "_cancel_inflight", cancel)
    consumers = background_consumers.BackgroundConsumers()
    monkeypatch.setattr(background_consumers, "inflight", lambda: 0)
    assert await consumers.drain(0.05) is True
    monkeypatch.setattr(background_consumers, "inflight", lambda: 2)
    assert await consumers.drain(0.05) is False  # 상한을 넘기면 취소하고 그 사실을 돌려준다
    cancel.assert_awaited_once()


async def test_queued_work_drain_reports_timeout_after_cancelling_what_is_left():
    release = asyncio.Event()
    quick = asyncio.create_task(queued_work.run_tracked(asyncio.sleep(0.02)))
    await asyncio.sleep(0)
    assert await queued_work.drain(1) is True and quick.done()
    assert queued_work.draining() is True  # drain 은 먼저 새 claim 을 닫는다
    queued_work.reopen()
    stuck = asyncio.create_task(queued_work.run_tracked(release.wait()))
    await asyncio.sleep(0)
    assert queued_work.draining() is False
    assert await queued_work.drain(0.05) is False
    assert queued_work.draining() is True  # 기다리는 동안에도 새 claim 은 닫혀 있다
    await asyncio.gather(stuck, return_exceptions=True)
    assert queued_work.inflight() == 0


# ------------------------------------------------------------------ local rollback 경로: runtime_generation=false

async def test_legacy_runtime_runs_the_creative_and_game_consumers_so_rollback_leaves_no_backlog(monkeypatch):
    from ttakkari import main
    from ttakkari.creative.v2 import asset_worker
    from ttakkari.games import builds
    seen = {"assets": asyncio.Event(), "builds": asyncio.Event()}

    async def assets():
        seen["assets"].set()
        return False

    async def game_builds():
        seen["builds"].set()  # 아무것도 없으면 None 을 돌려준다

    monkeypatch.setattr(asset_worker, "consume_once", assets)
    monkeypatch.setattr(builds, "consume_queued_build", game_builds)
    generation.current = None  # runtime_generation=false 에는 세대 런타임이 없다
    tasks = generation_service.start_legacy_queued_consumers(0.02)
    try:
        await asyncio.wait_for(asyncio.gather(*(e.wait() for e in seen.values())), 5)
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
    source = inspect.getsource(main.lifespan)
    assert "start_legacy_queued_consumers" in source and "finish_claimed_work" in source
    assert "stop(drain_seconds=0)" not in source


# ------------------------------------------------------------------ queued 행 보존 경로: drain 중 미착수 대기열 불변

async def test_drain_preserves_queued_rows_for_notebooklm_reverse_and_context_index(monkeypatch):
    """drain() 은 이미 running/in-flight 인 작업만 자연 완료(또는 상한 후 취소)를 기다리며,
    아직 claim 되지 않은 queued 행(NotebookLM, Reverse, 수동색인)은 건드리지 않고 보존해야 한다."""
    queued_store = {
        "notebooklm": ["job-queued-1", "job-queued-2"],
        "reverse": ["rev-queued-1"],
        "context-index": ["source-queued-1"],
    }
    claimed = []

    async def fake_pump_notebooklm():
        if queued_store["notebooklm"]:
            job = queued_store["notebooklm"].pop(0)
            claimed.append(job)
            return 1
        return 0

    async def fake_pump_reverse():
        if queued_store["reverse"]:
            job = queued_store["reverse"].pop(0)
            claimed.append(job)
            return 1
        return 0

    async def fake_pump_context():
        if queued_store["context-index"]:
            job = queued_store["context-index"].pop(0)
            claimed.append(job)
            return 1
        return 0

    consumers = background_consumers.BackgroundConsumers()
    monkeypatch.setattr(background_consumers, "consumers", consumers)
    monkeypatch.setattr(background_consumers, "PUMPS", (
        ("notebooklm", fake_pump_notebooklm),
        ("reverse", fake_pump_reverse),
        ("context-index", fake_pump_context),
    ))
    monkeypatch.setattr(background_consumers, "inflight", lambda: 0)
    monkeypatch.setattr("ttakkari.reverse.resume_waiting_reverse_jobs", AsyncMock(return_value=0))

    # 1. 종료 신호 도착: stop_claiming() 호출
    consumers.stop_claiming()

    # 2. drain 실행 (소비자 루프는 정지된 상태에서 잔여 in-flight drain 대기)
    drained = await consumers.drain(drain_seconds=0.1)
    assert drained is True

    # 3. stop_claiming 이후이므로 queued 항목들은 소비/삭제/취소되지 않고 그대로 보존됨
    assert queued_store["notebooklm"] == ["job-queued-1", "job-queued-2"]
    assert queued_store["reverse"] == ["rev-queued-1"]
    assert queued_store["context-index"] == ["source-queued-1"]
    assert claimed == []


# ------------------------------------------------------------------ pump 진행 중 stop 상한 (독립 검증 재현)

async def test_stop_respects_drain_seconds_when_pump_is_held(monkeypatch):
    """fake pump를 이벤트로 붙잡고 stop(drain_seconds=0.01) 호출 시 100ms 뒤에도 stop이 끝나지 않는 결함을 재현.
    교정 후에는 drain_seconds 상한 안에서 종료되고 timeout 결과(False)를 반환해야 한다."""
    entered = asyncio.Event()
    release = asyncio.Event()

    async def fake_held_pump() -> int:
        entered.set()
        await release.wait()
        return 0

    consumers = background_consumers.BackgroundConsumers()
    monkeypatch.setattr(background_consumers, "consumers", consumers)
    monkeypatch.setattr(background_consumers, "PUMPS", (("held", fake_held_pump),))
    monkeypatch.setattr(background_consumers, "inflight", lambda: 0)
    monkeypatch.setattr("ttakkari.reverse.resume_waiting_reverse_jobs", AsyncMock(return_value=0))

    await consumers.start()
    await asyncio.wait_for(entered.wait(), 5)

    stop_task = asyncio.create_task(consumers.stop(drain_seconds=0.01))
    await asyncio.sleep(0.1)
    try:
        assert stop_task.done(), "stop() must finish within drain_seconds even if pump is held"
        assert stop_task.result() is False
    finally:
        release.set()
        await asyncio.gather(stop_task, return_exceptions=True)

async def test_finish_claimed_work_respects_drain_seconds_when_pump_is_held(monkeypatch):
    """pump가 붙잡혀 있어도 finish_claimed_work()는 상한 내에 반환하고 background에 timeout(False)이 기록된다."""
    entered = asyncio.Event()
    release = asyncio.Event()

    async def fake_held_pump() -> int:
        entered.set()
        await release.wait()
        return 0

    consumers = background_consumers.BackgroundConsumers()
    monkeypatch.setattr(background_consumers, "consumers", consumers)
    monkeypatch.setattr(background_consumers, "PUMPS", (("held", fake_held_pump),))
    monkeypatch.setattr(background_consumers, "inflight", lambda: 0)
    monkeypatch.setattr("ttakkari.reverse.resume_waiting_reverse_jobs", AsyncMock(return_value=0))
    from ttakkari.tts import jobs as tts_jobs
    monkeypatch.setattr(tts_jobs, "drain", AsyncMock())
    monkeypatch.setattr(queued_work, "drain", AsyncMock(return_value=True))

    await consumers.start()
    await asyncio.wait_for(entered.wait(), 5)

    finish_task = asyncio.create_task(generation_service.finish_claimed_work(seconds=0.01))
    await asyncio.sleep(0.1)
    try:
        assert finish_task.done(), "finish_claimed_work() must finish within seconds"
        results = finish_task.result()
        assert results.get("background") is False
    finally:
        release.set()
        await asyncio.gather(finish_task, return_exceptions=True)


async def test_stop_cancels_inflight_when_pump_times_out(monkeypatch):
    """pump가 상한 내에 종료되지 않아 timeout될 때 남아있는 inflight 작업도 _cancel_inflight로 정리된다."""
    entered = asyncio.Event()
    release = asyncio.Event()
    cancelled = AsyncMock()

    async def fake_held_pump() -> int:
        entered.set()
        await release.wait()
        return 0

    consumers = background_consumers.BackgroundConsumers()
    monkeypatch.setattr(background_consumers, "consumers", consumers)
    monkeypatch.setattr(background_consumers, "PUMPS", (("held", fake_held_pump),))
    monkeypatch.setattr(background_consumers, "inflight", lambda: 1)
    monkeypatch.setattr(background_consumers, "_cancel_inflight", cancelled)
    monkeypatch.setattr("ttakkari.reverse.resume_waiting_reverse_jobs", AsyncMock(return_value=0))

    await consumers.start()
    await asyncio.wait_for(entered.wait(), 5)

    stop_task = asyncio.create_task(consumers.stop(drain_seconds=0.01))
    await asyncio.sleep(0.1)
    try:
        assert stop_task.done()
        assert stop_task.result() is False
        cancelled.assert_awaited_once()
    finally:
        release.set()
        await asyncio.gather(stop_task, return_exceptions=True)
