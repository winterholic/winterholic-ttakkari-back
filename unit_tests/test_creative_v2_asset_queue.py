"""Creative V2 자동 자산 생성: API 는 요청을 production.json 에 queued 로 남기고, 별도 소비자가 순차 실행한다.

행동을 확인한다. 생성기는 Protocol 을 구현한 가짜이고, 큐 상태·파일 lock·자산 등록·checkpoint 는 실제 코드를 쓴다.
"""
from __future__ import annotations

import asyncio
import fcntl
import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest
from test_creative_v2_asset_generation import FakeGenerator, approved_plan, make_intent

from ttakkari import queued_work
from ttakkari.api import creative_v2
from ttakkari.creative.v2 import asset_generation, asset_worker
from ttakkari.creative.v2 import asset_generation_managed as managed
from ttakkari.creative.v2.duration import DurationRequest
from ttakkari.creative.v2.plans import PlanStore, build_plan
from ttakkari.creative.v2.registry import Registry

UNIT_DIR = Path(__file__).parent


@pytest.fixture(autouse=True)
def _fresh_queue_state():
    queued_work.reopen()
    creative_v2._ASSET_GENERATION_TASKS.clear()
    yield
    queued_work.reopen()
    creative_v2._ASSET_GENERATION_TASKS.clear()


def records(plans: PlanStore, plan_id: str) -> list[dict]:
    return asset_generation.ProductionStore(plans).read(plan_id).get("asset_generations", [])


def second_plan(plans: PlanStore) -> dict:
    plan = plans.create(build_plan("두 번째 설명 영상 3막 구조", make_intent(), registry=Registry()))
    return plans.approve(plan["id"], plan["revision"])


def test_enqueue_records_queued_request_without_running_the_generator(tmp_path):
    plans, plan = approved_plan(tmp_path)
    generator = FakeGenerator(tmp_path)

    status = asset_generation.enqueue_assets(plans, plan["id"], plan["revision"], generator)

    assert status == "queued"
    assert generator.calls == 0
    (record,) = records(plans, plan["id"])
    assert record["status"] == "queued" and record["plan_revision"] == plan["revision"]
    assert record["requested_at"] and record["started_at"] is None
    # 사용자가 production 상태를 조회하면 queued 가 그대로 보인다(HTTP 계약 보존).
    assert asset_generation.ProductionStore(plans).read(plan["id"])["asset_generations"][-1]["status"] == "queued"


def test_duplicate_enqueue_keeps_one_record_and_one_execution(tmp_path):
    plans, plan = approved_plan(tmp_path)
    generator = FakeGenerator(tmp_path)

    assert asset_generation.enqueue_assets(plans, plan["id"], plan["revision"], generator) == "queued"
    assert asset_generation.enqueue_assets(plans, plan["id"], plan["revision"], generator) == "queued"
    assert len(records(plans, plan["id"])) == 1

    assert asyncio.run(asset_worker.consume_once(plans=plans, generator=generator)) is True
    assert asyncio.run(asset_worker.consume_once(plans=plans, generator=generator)) is False
    assert generator.calls == 1
    (record,) = records(plans, plan["id"])
    assert record["status"] == "succeeded"
    # 이미 성공한 같은 키의 재요청은 새 작업도 새 기록도 만들지 않는다(기존 계약: queued 로 답하고 아무것도 하지 않음).
    assert asset_generation.enqueue_assets(plans, plan["id"], plan["revision"], generator) == "queued"
    assert asyncio.run(asset_worker.consume_once(plans=plans, generator=generator)) is False
    assert generator.calls == 1
    assert records(plans, plan["id"])[0]["status"] == "succeeded"


def test_consumer_completes_queued_work_and_preserves_asset_bodies(tmp_path):
    plans, plan = approved_plan(tmp_path)
    generator = FakeGenerator(tmp_path)
    asset_generation.enqueue_assets(plans, plan["id"], plan["revision"], generator)

    assert asyncio.run(asset_worker.consume_once(plans=plans, generator=generator)) is True

    (record,) = records(plans, plan["id"])
    assert record["status"] == "succeeded" and record["error"] is None
    assert set(record["assets"]["scenes"]) == {s["scene_id"] for s in plan["scenes"]}
    store = asset_generation.ProductionStore(plans)
    for asset in (*record["assets"]["scenes"].values(), *record["assets"]["voices"].values(), record["assets"]["music"]):
        assert store.asset_path(plan["id"], asset).read_bytes()  # 본문이 실제로 등록 폴더에 있다
    # 요청 시각과 진행 기록은 실행 기록에도 남는다.
    assert record["requested_at"] and record["progress"]


def test_live_execution_is_never_queued_again_and_queued_behind_it_is_skipped(tmp_path):
    plans, plan = approved_plan(tmp_path)
    generator = FakeGenerator(tmp_path)
    asset_generation.enqueue_assets(plans, plan["id"], plan["revision"], generator)
    key = records(plans, plan["id"])[0]["key"]
    lock_path = asset_generation.ProductionStore(plans).folder(plan["id"]) / "generation-work" / key / "generation.lock"
    lock_path.parent.mkdir(parents=True)
    holder = lock_path.open("a")
    fcntl.flock(holder, fcntl.LOCK_EX)  # 살아 있는 다른 프로세스의 실행을 흉내 낸다
    try:
        assert asyncio.run(asset_worker.consume_once(plans=plans, generator=generator)) is False
        assert generator.calls == 0
        assert records(plans, plan["id"])[0]["status"] == "queued"  # 실패 처리하지 않고 보존
    finally:
        fcntl.flock(holder, fcntl.LOCK_UN)
        holder.close()
    assert asyncio.run(asset_worker.consume_once(plans=plans, generator=generator)) is True
    assert generator.calls == 1


def test_running_record_with_live_owner_is_reported_running_not_requeued(tmp_path):
    plans, plan = approved_plan(tmp_path)
    generator = FakeGenerator(tmp_path)
    store = asset_generation.ProductionStore(plans)
    key = asset_generation._key(plan, generator)
    with store.tx(plan["id"]) as data:
        data["asset_generations"] = [{"id": "g1", "key": key, "plan_revision": plan["revision"], "status": "running",
                                      "progress": [], "assets": {}, "receipt": None, "error": None}]
    workdir = store.folder(plan["id"]) / "generation-work" / key
    workdir.mkdir(parents=True)
    holder = (workdir / "generation.lock").open("a")
    fcntl.flock(holder, fcntl.LOCK_EX)
    try:
        assert asset_generation.enqueue_assets(plans, plan["id"], plan["revision"], generator) == "running"
    finally:
        holder.close()
    assert records(plans, plan["id"])[0]["status"] == "running"


def test_orphaned_running_record_is_never_rerun_by_the_consumer_but_explicit_request_resumes_it(tmp_path):
    plans, plan = approved_plan(tmp_path)
    generator = FakeGenerator(tmp_path)
    store = asset_generation.ProductionStore(plans)
    key = asset_generation._key(plan, generator)
    previous = [{"stage": "scene_comfy_submitted", "scene_id": "scene-0", "task_id": "T1", "at": "t"}]
    with store.tx(plan["id"]) as data:
        data["asset_generations"] = [{"id": "orphan", "key": key, "plan_revision": plan["revision"], "status": "running",
                                      "started_at": "t", "progress": previous, "assets": {}, "receipt": None, "error": None}]

    # 배포·재시작 뒤 소비자가 시작돼도 이전 실행이 남긴 running 은 맹목적으로 다시 실행하지 않는다.
    assert asyncio.run(asset_worker.consume_once(plans=plans, generator=generator)) is False
    assert generator.calls == 0 and records(plans, plan["id"])[0]["status"] == "running"

    # 사용자가 명시적으로 다시 요청하면 같은 기록(id·진행 이력)을 이어서 queued 로 올린다.
    assert asset_generation.enqueue_assets(plans, plan["id"], plan["revision"], generator) == "queued"
    (queued,) = records(plans, plan["id"])
    assert queued["id"] == "orphan" and queued["status"] == "queued" and queued["progress"] == previous
    assert asyncio.run(asset_worker.consume_once(plans=plans, generator=generator)) is True
    (done,) = records(plans, plan["id"])
    assert done["id"] == "orphan" and done["status"] == "succeeded"
    assert done["progress"][0] == previous[0]  # 이전 이력은 지워지지 않는다
    assert generator.calls == 1


def test_failed_generation_can_be_retried_through_the_queue(tmp_path):
    plans, plan = approved_plan(tmp_path)

    class FailingOnce(FakeGenerator):
        def generate(self, plan, *, workdir, progress):
            if self.calls == 0:
                self.calls += 1
                raise RuntimeError("Comfy 일시 실패")
            return super().generate(plan, workdir=workdir, progress=progress)

    generator = FailingOnce(tmp_path)
    asset_generation.enqueue_assets(plans, plan["id"], plan["revision"], generator)
    assert asyncio.run(asset_worker.consume_once(plans=plans, generator=generator)) is True
    (failed,) = records(plans, plan["id"])
    assert failed["status"] == "failed" and "Comfy 일시 실패" in failed["error"]
    # 실패한 요청은 소비자가 다시 집지 않는다. 사용자가 다시 요청해야 한다.
    assert asyncio.run(asset_worker.consume_once(plans=plans, generator=generator)) is False

    assert asset_generation.enqueue_assets(plans, plan["id"], plan["revision"], generator) == "queued"
    assert asyncio.run(asset_worker.consume_once(plans=plans, generator=generator)) is True
    (done,) = records(plans, plan["id"])
    assert done["status"] == "succeeded"


def test_invalid_queued_request_fails_without_blocking_the_next_plan(tmp_path):
    plans, first = approved_plan(tmp_path)
    second = second_plan(plans)
    generator = FakeGenerator(tmp_path)
    asset_generation.enqueue_assets(plans, first["id"], first["revision"], generator)
    asset_generation.enqueue_assets(plans, second["id"], second["revision"], generator)
    # 대기 중에 첫 계획의 revision 이 바뀌어 더는 승인 revision 이 아니다.
    plans.edit(first["id"], first["revision"], duration=DurationRequest(mode="preferred", seconds=45), registry=Registry())

    assert asyncio.run(asset_worker.consume_once(plans=plans, generator=generator)) is True  # 첫 요청을 실패로 닫는다
    bad = records(plans, first["id"])[0]
    assert bad["status"] == "failed" and "승인" in bad["error"]
    assert asyncio.run(asset_worker.consume_once(plans=plans, generator=generator)) is True
    assert records(plans, second["id"])[0]["status"] == "succeeded"
    assert generator.calls == 1


def test_consumer_runs_queued_plans_in_request_order_and_never_twice(tmp_path):
    plans, first = approved_plan(tmp_path)
    second = second_plan(plans)
    order: list[str] = []

    class Recording(FakeGenerator):
        def generate(self, plan, *, workdir, progress):
            order.append(plan["id"])
            return super().generate(plan, workdir=workdir, progress=progress)

    generator = Recording(tmp_path)
    asset_generation.enqueue_assets(plans, second["id"], second["revision"], generator)
    asset_generation.enqueue_assets(plans, first["id"], first["revision"], generator)

    while asyncio.run(asset_worker.consume_once(plans=plans, generator=generator)):
        pass

    assert order == [second["id"], first["id"]]
    assert all(records(plans, p["id"])[0]["status"] == "succeeded" for p in (first, second))


async def test_two_consumers_racing_for_one_request_execute_it_once(tmp_path):
    plans, plan = approved_plan(tmp_path)
    generator = FakeGenerator(tmp_path)
    asset_generation.enqueue_assets(plans, plan["id"], plan["revision"], generator)

    await asyncio.gather(
        asset_worker.consume_once(plans=plans, generator=generator),
        asset_worker.consume_once(plans=plans, generator=generator),
    )

    assert generator.calls == 1
    assert records(plans, plan["id"])[0]["status"] == "succeeded"


def test_drain_stops_new_claims_and_keeps_queued_work(tmp_path):
    plans, plan = approved_plan(tmp_path)
    generator = FakeGenerator(tmp_path)
    asset_generation.enqueue_assets(plans, plan["id"], plan["revision"], generator)

    queued_work.begin_drain()
    assert asyncio.run(asset_worker.consume_once(plans=plans, generator=generator)) is False
    assert generator.calls == 0 and records(plans, plan["id"])[0]["status"] == "queued"

    queued_work.reopen()
    assert asyncio.run(asset_worker.consume_once(plans=plans, generator=generator)) is True
    assert generator.calls == 1


def test_drain_waits_for_the_running_generation_to_finish(tmp_path):
    plans, plan = approved_plan(tmp_path)
    gate = {"release": None}

    class Slow(FakeGenerator):
        def generate(self, plan, *, workdir, progress):
            gate["started"].set()
            asyncio.run_coroutine_threadsafe(gate["release"].wait(), gate["loop"]).result(10)
            return super().generate(plan, workdir=workdir, progress=progress)

    generator = Slow(tmp_path)
    asset_generation.enqueue_assets(plans, plan["id"], plan["revision"], generator)

    async def scenario():
        gate["loop"] = asyncio.get_running_loop()
        gate["release"] = asyncio.Event()
        gate["started"] = __import__("threading").Event()
        consumer = asyncio.create_task(asset_worker.consume_once(plans=plans, generator=generator))
        await asyncio.to_thread(gate["started"].wait, 10)
        queued_work.begin_drain()
        consumer.cancel()  # 소비자 task 가 취소돼도(종료 신호) 실행 중인 작업은 끝까지 간다
        idle = asyncio.create_task(queued_work.wait_idle())
        await asyncio.sleep(0.2)
        assert not idle.done()
        assert records(plans, plan["id"])[0]["status"] == "running"
        gate["release"].set()
        await asyncio.wait_for(idle, 20)
        with pytest.raises(asyncio.CancelledError):
            await consumer

    asyncio.run(scenario())
    assert records(plans, plan["id"])[0]["status"] == "succeeded"


def test_queued_record_with_older_engine_version_is_adopted_by_current_engine(tmp_path):
    plans, plan = approved_plan(tmp_path)
    old = FakeGenerator(tmp_path)
    old.version = "test-0"
    asset_generation.enqueue_assets(plans, plan["id"], plan["revision"], old)
    queued_id = records(plans, plan["id"])[0]["id"]

    current = FakeGenerator(tmp_path)  # 실행부가 더 새 버전으로 교체된 뒤
    assert asyncio.run(asset_worker.consume_once(plans=plans, generator=current)) is True

    (record,) = records(plans, plan["id"])
    assert record["id"] == queued_id and record["status"] == "succeeded"
    assert record["engine"]["version"] == "test-1"
    assert asyncio.run(asset_worker.consume_once(plans=plans, generator=current)) is False  # 남은 queued 가 없다


def test_checkpoint_is_flushed_before_atomic_replace_and_leaves_no_temp(tmp_path, monkeypatch):
    target = tmp_path / "scene-0-managed-comfy.json"
    target.write_text('{"identity": "old", "attempts": []}')
    events: list[str] = []
    real_fsync, real_replace = os.fsync, Path.replace
    monkeypatch.setattr(os, "fsync", lambda fd: (events.append("fsync"), real_fsync(fd))[1])

    def spy_replace(self, destination):
        events.append("replace")
        return real_replace(self, destination)

    monkeypatch.setattr(Path, "replace", spy_replace)

    managed.ManagedAssetGenerator._write_checkpoint(target, {"identity": "new", "attempts": [{"task_id": "T"}]})

    assert events.index("fsync") < events.index("replace")
    assert json.loads(target.read_text()) == {"attempts": [{"task_id": "T"}], "identity": "new"}
    assert [p.name for p in tmp_path.iterdir()] == [target.name]


def test_checkpoint_failure_before_replace_keeps_the_previous_checkpoint(tmp_path, monkeypatch):
    target = tmp_path / "scene-0-managed-comfy.json"
    target.write_text('{"identity": "old", "attempts": [{"task_id": "T0"}]}')

    def broken_fsync(fd):
        raise OSError("disk full")

    monkeypatch.setattr(os, "fsync", broken_fsync)
    with pytest.raises(OSError):
        managed.ManagedAssetGenerator._write_checkpoint(target, {"identity": "new", "attempts": []})

    assert json.loads(target.read_text())["attempts"] == [{"task_id": "T0"}]


# ---- API 경계 ------------------------------------------------------------------------------------------


class _RemoteApi:
    role = "api"
    execution_mode = "remote"


class _LocalApi:
    role = "api"
    execution_mode = "local"


async def _post(plans: PlanStore, plan: dict, monkeypatch, runtime):
    from ttakkari import generation

    monkeypatch.setattr(creative_v2, "default_plan_store", lambda: plans)
    monkeypatch.setattr(generation, "current", runtime)
    return await creative_v2.production_generate_assets(
        __import__("uuid").UUID(plan["id"]), creative_v2.PlanApprove(revision=plan["revision"]), "owner"
    )


async def test_remote_api_persists_the_request_and_owns_no_task(tmp_path, monkeypatch):
    plans, plan = approved_plan(tmp_path)
    started: list[tuple] = []

    async def must_not_run(plan_id, revision):
        started.append((plan_id, revision))

    monkeypatch.setattr(creative_v2, "_run_asset_generation", must_not_run)
    tasks_before = set(asyncio.all_tasks())

    first = await _post(plans, plan, monkeypatch, _RemoteApi())
    second = await _post(plans, plan, monkeypatch, _RemoteApi())

    assert first == {"plan_id": plan["id"], "plan_revision": plan["revision"], "status": "queued"}
    assert second["status"] == "queued"
    await asyncio.sleep(0)
    assert started == [] and creative_v2._ASSET_GENERATION_TASKS == {}
    assert set(asyncio.all_tasks()) == tasks_before  # API 가 장시간 작업 task 를 소유하지 않는다
    assert len(records(plans, plan["id"])) == 1


async def test_queued_request_survives_api_restart_and_is_finished_by_a_separate_consumer(tmp_path, monkeypatch):
    plans, plan = approved_plan(tmp_path)
    await _post(plans, plan, monkeypatch, _RemoteApi())

    # API 프로세스 종료·재기동: 메모리 상태는 전부 사라지고 디스크 상태만 남는다.
    creative_v2._ASSET_GENERATION_TASKS.clear()
    reopened = PlanStore(plans.root, registry=Registry())
    assert records(reopened, plan["id"])[0]["status"] == "queued"

    generator = FakeGenerator(tmp_path)
    assert await asset_worker.consume_once(plans=reopened, generator=generator) is True

    (record,) = records(reopened, plan["id"])
    assert record["status"] == "succeeded" and generator.calls == 1


def test_consumer_in_a_fresh_process_finishes_work_queued_by_a_previous_process(tmp_path):
    plans, plan = approved_plan(tmp_path)
    asset_generation.enqueue_assets(plans, plan["id"], plan["revision"], FakeGenerator(tmp_path))
    script = textwrap.dedent(
        f"""
        import asyncio, sys
        sys.path.insert(0, {str(UNIT_DIR)!r})
        from pathlib import Path
        from test_creative_v2_asset_generation import FakeGenerator
        from ttakkari.creative.v2 import asset_worker
        from ttakkari.creative.v2.plans import PlanStore
        from ttakkari.creative.v2.registry import Registry

        plans = PlanStore(Path({str(plans.root)!r}), registry=Registry())
        generator = FakeGenerator(Path({str(tmp_path)!r}))
        print("consumed", asyncio.run(asset_worker.consume_once(plans=plans, generator=generator)), generator.calls)
        """
    )
    result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, timeout=120, check=False)
    assert result.returncode == 0, result.stderr[-2000:]
    assert "consumed True 1" in result.stdout
    (record,) = records(plans, plan["id"])
    assert record["status"] == "succeeded"


async def test_local_mode_keeps_the_in_process_task_path(tmp_path, monkeypatch):
    plans, plan = approved_plan(tmp_path)
    ran = asyncio.Event()

    async def fake_run(plan_id, revision):
        ran.set()

    monkeypatch.setattr(creative_v2, "_run_asset_generation", fake_run)

    response = await _post(plans, plan, monkeypatch, _LocalApi())
    await asyncio.wait_for(ran.wait(), 5)

    assert response["status"] == "queued"
    assert records(plans, plan["id"]) == []  # 기존 경로는 production.json 에 queued 를 쓰지 않는다


async def test_api_rejects_unapproved_revision_before_queueing(tmp_path, monkeypatch):
    plans = PlanStore(tmp_path / "plans", registry=Registry())
    plan = plans.create(build_plan("승인 전 계획 3막 구조", make_intent(), registry=Registry()))

    with pytest.raises(Exception) as caught:
        await _post(plans, plan, monkeypatch, _RemoteApi())

    assert getattr(caught.value, "status_code", None) in (409, 422)
    assert records(plans, plan["id"]) == []
