"""게임 빌드 실행부 인계: 원격 API 는 building 행(queue_state=queued)만 남기고, 별도 소비자가 순차로 빌드한다.

빌드·스냅샷·잠금은 실제 코드이고 격리 러너(docker)만 FakeAdapter 로 대신한다. 소비자는 API 와 다른 DB 세션·상태로 돈다.
"""

import asyncio
import uuid
from datetime import timedelta
from pathlib import Path

import pytest
from helpers import fresh_dir
from sqlalchemy import func, select
from test_api_games import make_repo, register, wait_build
from test_game_build_snapshots import FakeAdapter, git, manifest  # noqa: F401

from ttakkari import generation, queued_work
from ttakkari.config import get_settings
from ttakkari.db import sessionmaker
from ttakkari.games import builds
from ttakkari.games.builds import delete_snapshot_files, verify_snapshot
from ttakkari.games.contract import game_dir
from ttakkari.models import GameBuild, GameProject, utcnow


class RemoteApi:
    """원격 모드 API 세대. InflightMiddleware 가 http_inflight 를 읽는다."""

    role, execution_mode, http_inflight = "api", "remote", 0


class ExecutorLike:
    role, execution_mode, http_inflight = "executor", "local", 0


@pytest.fixture(autouse=True)
def _cleanup():
    queued_work.reopen()
    yield
    queued_work.reopen()
    root = get_settings().game_builds_dir
    if root.exists():
        for child in root.iterdir():
            delete_snapshot_files(child)


@pytest.fixture
def remote_api(monkeypatch):
    monkeypatch.setattr(generation, "current", RemoteApi())


class Probe:
    """빌드가 실제로 몇 번, 동시에 몇 개 도는지 센다."""

    def __init__(self, tmp_path: Path):
        self.started: list[str] = []
        self.now = self.peak = 0
        self.delay = 0.0
        self.gate: asyncio.Event | None = None
        self.adapter = FakeAdapter(tmp_path / "w", hook=self.hook)

    async def hook(self, project_path: Path):
        self.started.append(project_path.name)
        self.now += 1
        self.peak = max(self.peak, self.now)
        try:
            if self.gate is not None:
                await self.gate.wait()
            await asyncio.sleep(self.delay)
        finally:
            self.now -= 1


@pytest.fixture
def probe(monkeypatch, tmp_path: Path) -> Probe:
    p = Probe(tmp_path)
    monkeypatch.setattr(builds, "get_adapter", lambda engine_id, **kw: p.adapter)
    return p


async def new_game(client, auth, slug: str) -> dict:
    make_repo(slug)
    game = (await register(client, auth, slug)).json()
    assert (await client.post(f"/api/games/{game['id']}/lock/approve", headers=auth)).status_code == 200
    return game


async def start(client, auth, game_id: str):
    return await client.post(f"/api/games/{game_id}/builds", json={}, headers=auth)


async def row(build_id: str) -> GameBuild:
    async with sessionmaker()() as db:
        return await db.get(GameBuild, uuid.UUID(build_id))


# ---- 접수: API 는 기록만 하고 조회만 돌려준다 ------------------------------------------------------


async def test_remote_api_accepts_build_without_running_it_and_keeps_the_response_contract(client, auth, probe, monkeypatch):
    game = await new_game(client, auth, "q-contract")
    local = (await start(client, auth, game["id"])).json()  # 로컬 모드(기존 경로): 같은 응답 모양의 기준
    await wait_build(client, auth, game["id"], local["id"])
    monkeypatch.setattr(generation, "current", RemoteApi())
    probe.started.clear()
    before = set(asyncio.all_tasks())

    r = await start(client, auth, game["id"])

    assert r.status_code == 202
    queued = r.json()
    assert set(queued) == set(local) and queued["status"] == "building" and queued["seq"] == 2
    await asyncio.sleep(0.3)
    assert probe.started == [] and set(asyncio.all_tasks()) - before == set()  # API 는 빌드 task 를 소유하지 않는다
    stored = await row(queued["id"])
    assert (stored.status, stored.queue_state, stored.claimed_at, stored.finished_at) == ("building", "queued", None, None)
    polled = (await client.get(f"/api/games/{game['id']}/builds/{queued['id']}", headers=auth)).json()
    assert polled["status"] == "building"  # 조회는 접수 상태를 그대로 돌려준다


async def test_queued_build_survives_api_restart_and_a_separate_consumer_completes_it(client, auth, probe, remote_api):
    game = await new_game(client, auth, "q-restart")
    accepted = (await start(client, auth, game["id"])).json()

    # API 프로세스 종료·재기동: 메모리의 task/상태는 사라지고 DB 행만 남는다. 소비자는 새 세션으로 시작한다.
    from ttakkari.runs.manager import _background
    _background.clear()
    consumed = await builds.consume_queued_build()

    assert consumed is not None and consumed.id == uuid.UUID(accepted["id"]) and consumed.status == "succeeded"
    done = await wait_build(client, auth, game["id"], accepted["id"])
    assert done["status"] == "succeeded" and done["artifact_id"] and len(done["bundle_sha256"]) == 64
    stored = await row(accepted["id"])
    assert stored.queue_state == "claimed" and stored.claimed_at is not None and verify_snapshot(stored)
    assert probe.started.count(game["slug"]) == 1
    assert (await client.get(f"/api/games/{game['id']}/builds/{accepted['id']}?verify=true", headers=auth)).json()["snapshot_verified"] is True


async def test_duplicate_accept_is_rejected_and_queued_row_is_never_aged_out(client, auth, probe, remote_api):
    game = await new_game(client, auth, "q-dup")
    first = (await start(client, auth, game["id"])).json()
    async with sessionmaker()() as db:  # 실행부가 오래 멈춰 있었던 것처럼 접수 시각을 한참 과거로 돌린다
        (await db.get(GameBuild, uuid.UUID(first["id"]))).created_at = utcnow() - timedelta(days=2)
        await db.commit()

    again = await start(client, auth, game["id"])

    assert again.status_code == 409 and again.json()["code"] == "build_in_progress"
    stored = await row(first["id"])
    assert stored.status == "building" and stored.queue_state == "queued" and stored.error is None
    async with sessionmaker()() as db:
        assert await db.scalar(select(func.count()).select_from(GameBuild)) == 1
    assert (await builds.consume_queued_build()).status == "succeeded"
    assert probe.started.count(game["slug"]) == 1


async def test_two_consumers_never_build_the_same_row_twice(client, auth, probe, remote_api):
    game = await new_game(client, auth, "q-race")
    await start(client, auth, game["id"])
    probe.delay = 0.2

    results = await asyncio.gather(builds.consume_queued_build(), builds.consume_queued_build())

    assert sorted(r is None for r in results) == [False, True]
    assert probe.started.count(game["slug"]) == 1 and probe.peak == 1
    async with sessionmaker()() as db:
        assert (await db.scalars(select(GameBuild.status))).all() == ["succeeded"]


# ---- 기존 행·내구성 ----------------------------------------------------------------------------------


async def test_consumer_never_touches_building_rows_it_did_not_receive(client, auth, probe, remote_api):
    game = await new_game(client, auth, "q-legacy")
    async with sessionmaker()() as db:
        db.add(GameBuild(game_id=uuid.UUID(game["id"]), seq=1, status="building", created_at=utcnow() - timedelta(hours=3)))
        db.add(GameBuild(game_id=uuid.UUID(game["id"]), seq=2, status="building", created_at=utcnow()))
        await db.commit()

    assert await builds.consume_queued_build() is None

    assert probe.started == []
    async with sessionmaker()() as db:
        rows = (await db.scalars(select(GameBuild).order_by(GameBuild.seq))).all()
        assert [(b.status, b.queue_state, b.error, b.finished_at) for b in rows] == [("building", None, None, None)] * 2


async def test_claimed_build_is_judged_stale_from_its_claim_time_not_its_queue_time(client, auth, probe, remote_api):
    game = await new_game(client, auth, "q-stale")
    first = (await start(client, auth, game["id"])).json()
    async with sessionmaker()() as db:  # 오래 대기하다 방금 실행부가 맡았다. 아직 살아 있는 빌드다.
        b = await db.get(GameBuild, uuid.UUID(first["id"]))
        b.created_at, b.queue_state, b.claimed_at = utcnow() - timedelta(days=1), "claimed", utcnow()
        await db.commit()

    blocked = await start(client, auth, game["id"])
    assert blocked.status_code == 409 and blocked.json()["code"] == "build_in_progress"
    assert (await row(first["id"])).status == "building"

    async with sessionmaker()() as db:  # 맡은 뒤 실행부가 죽어 한참 지났다. 기존 stale 규칙대로 실패로 닫는다.
        (await db.get(GameBuild, uuid.UUID(first["id"]))).claimed_at = utcnow() - timedelta(days=1)
        await db.commit()
    accepted = await start(client, auth, game["id"])
    assert accepted.status_code == 202
    assert (await row(first["id"])).status == "failed"
    assert (await row(accepted.json()["id"])).queue_state == "queued"


async def test_drain_stops_new_claims_and_preserves_queued_builds(client, auth, probe, remote_api):
    game = await new_game(client, auth, "q-drain")
    accepted = (await start(client, auth, game["id"])).json()

    queued_work.begin_drain()
    assert await builds.consume_queued_build() is None
    stored = await row(accepted["id"])
    assert (stored.status, stored.queue_state) == ("building", "queued") and probe.started == []

    queued_work.reopen()
    assert (await builds.consume_queued_build()).status == "succeeded"


async def test_drain_lets_the_running_build_finish_even_if_its_consumer_is_cancelled(client, auth, probe, remote_api):
    game = await new_game(client, auth, "q-finish")
    accepted = (await start(client, auth, game["id"])).json()
    probe.gate = asyncio.Event()

    consumer = asyncio.create_task(builds.consume_queued_build())
    for _ in range(100):
        if probe.started:
            break
        await asyncio.sleep(0.05)
    assert probe.started
    queued_work.begin_drain()
    consumer.cancel()  # 종료 신호로 소비 루프가 취소된다
    idle = asyncio.create_task(queued_work.wait_idle())
    await asyncio.sleep(0.2)
    assert not idle.done() and (await row(accepted["id"])).status == "building"

    probe.gate.set()
    await asyncio.wait_for(idle, 60)
    with pytest.raises(asyncio.CancelledError):
        await consumer
    done = await row(accepted["id"])
    assert done.status == "succeeded" and verify_snapshot(done)


async def test_queued_builds_of_different_games_run_one_at_a_time_in_acceptance_order(client, auth, probe, remote_api):
    games = [await new_game(client, auth, f"q-seq-{i}") for i in range(3)]
    for game in games:
        assert (await start(client, auth, game["id"])).status_code == 202
        await asyncio.sleep(0.01)
    probe.delay = 0.05

    while await builds.consume_queued_build():
        pass

    assert probe.started == [g["slug"] for g in games] and probe.peak == 1
    async with sessionmaker()() as db:
        assert set((await db.scalars(select(GameBuild.status))).all()) == {"succeeded"}


async def test_failed_build_is_recorded_and_does_not_block_the_next_queued_build(client, auth, monkeypatch, tmp_path, remote_api):
    bad, good = await new_game(client, auth, "q-bad"), await new_game(client, auth, "q-good")
    first = (await start(client, auth, bad["id"])).json()
    second = (await start(client, auth, good["id"])).json()
    monkeypatch.setattr(builds, "get_adapter", lambda engine_id, **kw: FakeAdapter(tmp_path / "w", fail="빌드 실패(exit 2)"))

    failed = await builds.consume_queued_build()
    monkeypatch.setattr(builds, "get_adapter", lambda engine_id, **kw: FakeAdapter(tmp_path / "w"))
    ok = await builds.consume_queued_build()

    assert failed.status == "failed" and failed.error == "빌드 실패(exit 2)" and failed.id == uuid.UUID(first["id"])
    assert ok.status == "succeeded" and ok.id == uuid.UUID(second["id"])
    assert (await builds.consume_queued_build()) is None


async def test_unrecoverable_queued_row_is_closed_as_failed_instead_of_blocking_the_game(client, auth, probe, remote_api):
    game = await new_game(client, auth, "q-gone")
    accepted = (await start(client, auth, game["id"])).json()
    fresh_dir(game_dir("q-gone"))  # 대기 중 저장소가 비워졌다(매니페스트 없음)

    closed = await builds.consume_queued_build()
    stored = await row(accepted["id"])

    assert closed is not None and closed.id == stored.id  # 맡은 행의 결과를 돌려준다(실패로 닫힌 상태)
    assert stored.status == "failed" and stored.error and stored.finished_at
    assert probe.started == []
    # 닫혔으므로 같은 게임의 다음 접수를 막지 않는다(저장소가 복구되면 새 빌드를 받는다).
    make_repo("q-gone")
    assert (await start(client, auth, game["id"])).status_code == 202


# ---- 롤백, 로컬 모드 ---------------------------------------------------------------------------------


async def test_rollback_with_source_restore_hands_its_build_to_the_executor(client, auth, probe, remote_api):
    game = await new_game(client, auth, "q-roll")
    root = game_dir("q-roll")
    v1 = (await start(client, auth, game["id"])).json()
    assert (await builds.consume_queued_build()).status == "succeeded"
    (root / "a.txt").write_text("v2")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "v2")
    v2 = (await start(client, auth, game["id"])).json()
    assert (await builds.consume_queued_build()).status == "succeeded"
    probe.started.clear()

    r = await client.post(f"/api/games/{game['id']}/rollback", json={"build_id": v1["id"], "restore_source": True, "confirm": True}, headers=auth)

    assert r.status_code == 200, r.text
    restore = r.json()["restore_build"]
    assert restore["status"] == "building" and restore["restored_from_id"] == v1["id"]
    await asyncio.sleep(0.3)
    assert probe.started == []  # API 는 복구 빌드를 직접 돌리지 않는다
    assert (await row(restore["id"])).queue_state == "queued"

    assert (await builds.consume_queued_build()).status == "succeeded"
    async with sessionmaker()() as db:
        g = await db.get(GameProject, uuid.UUID(game["id"]))
        assert g.active_build_id == uuid.UUID(restore["id"])
        for build_id in (v1["id"], v2["id"]):
            assert (await db.get(GameBuild, uuid.UUID(build_id))).status == "succeeded"  # 이전 산출물은 그대로


async def test_local_mode_keeps_running_the_build_inside_the_api_process(client, auth, probe):
    assert generation.current is None  # 기본(로컬) 모드
    game = await new_game(client, auth, "q-local")

    r = await start(client, auth, game["id"])
    done = await wait_build(client, auth, game["id"], r.json()["id"])

    assert done["status"] == "succeeded"
    assert (await row(r.json()["id"])).queue_state is None
    assert await builds.consume_queued_build() is None  # 소비할 인계 행이 없다


async def test_only_remote_api_delegates_not_the_executor_that_runs_run_builds(monkeypatch):
    assert queued_work.delegated() is False
    monkeypatch.setattr(generation, "current", ExecutorLike())
    assert queued_work.delegated() is False
    monkeypatch.setattr(generation, "current", RemoteApi())
    assert queued_work.delegated() is True


async def test_consumer_in_a_fresh_process_completes_a_build_accepted_by_the_api(client, auth, remote_api, tmp_path):
    import subprocess
    import sys
    import textwrap

    game = await new_game(client, auth, "q-process")
    accepted = (await start(client, auth, game["id"])).json()  # API 는 여기까지만 한다
    script = textwrap.dedent(
        f"""
        import asyncio, sys
        sys.path.insert(0, {str(Path(__file__).parent)!r})
        from pathlib import Path
        from test_game_build_snapshots import FakeAdapter
        from ttakkari.db import dispose_engine
        from ttakkari.games import builds

        async def main():
            build = await builds.consume_queued_build(adapter=FakeAdapter(Path({str(tmp_path / "proc-w")!r})))
            print("built", build.id, build.status)
            await dispose_engine()

        asyncio.run(main())
        """
    )
    result = await asyncio.to_thread(
        subprocess.run, [sys.executable, "-c", script], capture_output=True, text=True, timeout=180, check=False)

    assert result.returncode == 0, result.stderr[-2000:]
    assert f"built {accepted['id']} succeeded" in result.stdout
    done = await row(accepted["id"])
    assert done.status == "succeeded" and done.queue_state == "claimed" and verify_snapshot(done)
    assert (await client.get(f"/api/games/{game['id']}/builds/{accepted['id']}", headers=auth)).json()["status"] == "succeeded"
