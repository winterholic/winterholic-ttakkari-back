"""개발·수정 worker Run 이 성공하면 같은 게임을 자동 빌드한다: 성공한 요청·소스 변경·잠금·진행 중 빌드 조건과 Run 종료 훅 (TASK-G17, ADR 0054)."""

import asyncio
import uuid

import pytest
from helpers import wait_run
from sqlalchemy import select
from test_api_games import fake_adapter, make_repo, register  # noqa: F401
from test_game_build_snapshots import _cleanup_snapshots, git  # noqa: F401
from test_game_worker_flow import drift_decisions, new_game

from ttakkari.config import get_settings
from ttakkari.db import sessionmaker
from ttakkari.games import autobuild, builds
from ttakkari.games.contract import game_dir
from ttakkari.models import (
    ChatSession,
    GameBuild,
    Run,
    RunStatus,
)


@pytest.fixture
def auto_on(monkeypatch):
    monkeypatch.setattr(get_settings(), "game_auto_build", True)


async def make_run(game: dict, *, status=RunStatus.succeeded, kind="develop", auto=True, tag_extra=None) -> uuid.UUID:
    """요청 Run 을 직접 만든다(worker 실행 없이 종료 훅 이후의 판단만 본다)."""
    async with sessionmaker()() as db:
        sess = ChatSession(workspace_id=uuid.UUID(game["workspace_id"]), title="t", engine="auto")
        db.add(sess)
        await db.flush()
        tag = {"game_id": game["id"], "kind": kind, "build_id": None, "lock": "verified", **({"auto_build": True} if auto else {}), **(tag_extra or {})}
        run = Run(session_id=sess.id, workspace_id=sess.workspace_id, prompt="p", engine="auto", status=status, usage={"game": tag})
        db.add(run)
        await db.commit()
        return run.id


async def all_builds(game_id: str) -> list[GameBuild]:
    async with sessionmaker()() as db:
        return list(await db.scalars(select(GameBuild).where(GameBuild.game_id == uuid.UUID(game_id)).order_by(GameBuild.seq)))


async def test_succeeded_develop_run_builds_once_and_links_run(client, auth, fake_adapter):  # noqa: F811
    game = await new_game(client, auth, "ab-first")
    run_id = await make_run(game)
    build = await autobuild.after_run(run_id)
    assert build is not None and build.status == "succeeded" and build.run_id == run_id and build.seq == 1
    assert (await client.get(f"/api/games/{game['id']}", headers=auth)).json()["active_build_id"] == str(build.id)  # 관제탑이 바로 연다
    # 소스가 그대로면 같은 소스를 다시 빌드하지 않는다
    assert await autobuild.after_run(await make_run(game, kind="modify")) is None
    assert len(await all_builds(game["id"])) == 1


async def test_new_commit_or_dirty_tree_builds_again(client, auth, fake_adapter):  # noqa: F811
    game = await new_game(client, auth, "ab-change")
    assert await autobuild.after_run(await make_run(game)) is not None
    root = game_dir("ab-change")
    (root / "a.txt").write_text("v2")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "worker change")
    b2 = await autobuild.after_run(await make_run(game, kind="modify"))
    assert b2 is not None and b2.seq == 2 and b2.source_dirty is False
    (root / "a.txt").write_text("v3 uncommitted")  # 커밋하지 않고 끝낸 worker
    b3 = await autobuild.after_run(await make_run(game, kind="modify"))
    assert b3 is not None and b3.seq == 3 and b3.source_dirty is True


async def test_no_build_for_failed_verify_untagged_or_missing_runs(client, auth, fake_adapter):  # noqa: F811
    game = await new_game(client, auth, "ab-skip")
    for kwargs in ({"status": RunStatus.failed}, {"status": RunStatus.cancelled}, {"kind": "verify"}, {"auto": False}):
        assert await autobuild.after_run(await make_run(game, **kwargs)) is None, kwargs
    assert await autobuild.after_run(uuid.uuid4()) is None
    assert await all_builds(game["id"]) == []
    async with sessionmaker()() as db:  # 게임이 없는 Run 도 조용히 끝난다
        sess = ChatSession(workspace_id=uuid.UUID(game["workspace_id"]), title="t", engine="auto")
        db.add(sess)
        await db.flush()
        run = Run(session_id=sess.id, workspace_id=sess.workspace_id, prompt="p", engine="auto", status=RunStatus.succeeded,
                  usage={"game": {"game_id": str(uuid.uuid4()), "kind": "develop", "auto_build": True}})
        db.add(run)
        await db.commit()
    assert await autobuild.after_run(run.id) is None


async def test_tampered_lock_and_running_build_are_skipped_without_error(client, auth, fake_adapter):  # noqa: F811
    game = await new_game(client, auth, "ab-lock")
    drift_decisions("ab-lock")  # 승인 후 에이전트가 잠금 결정을 바꿈
    assert await autobuild.after_run(await make_run(game)) is None  # lock_violation 은 빌드 행도 만들지 않는다
    assert await all_builds(game["id"]) == []
    busy = await new_game(client, auth, "ab-busy")
    async with sessionmaker()() as db:
        db.add(GameBuild(game_id=uuid.UUID(busy["id"]), seq=1, status="building"))
        await db.commit()
    assert await autobuild.after_run(await make_run(busy)) is None  # 진행 중인 빌드가 있으면 겹치지 않는다
    assert [b.status for b in await all_builds(busy["id"])] == ["building"]


async def test_failed_build_stays_failed_and_does_not_raise(client, auth, monkeypatch, tmp_path):
    from test_game_build_snapshots import FakeAdapter

    monkeypatch.setattr(builds, "get_adapter", lambda engine_id, **kw: FakeAdapter(tmp_path / "w", fail="tsc 오류"))
    game = await new_game(client, auth, "ab-fail")
    build = await autobuild.after_run(await make_run(game))
    assert build is not None and build.status == "failed" and "tsc" in (build.error or "")


async def test_run_tag_requires_setting_request_flag_and_non_verify(client, auth, monkeypatch, auto_on):
    game = await new_game(client, auth, "ab-tag")
    body = {"instruction": "점프를 가볍게"}

    async def tag_of(**extra) -> dict:
        r = (await client.post(f"/api/games/{game['id']}/requests", json={**body, **extra}, headers=auth)).json()
        run = (await client.get(f"/api/runs/{r['run_id']}", headers=auth)).json()
        await wait_run(client, auth, r["run_id"])
        return run["usage"]["game"]

    assert (await tag_of(kind="modify"))["auto_build"] is True  # 기본값
    assert "auto_build" not in await tag_of(kind="modify", auto_build=False)
    assert "auto_build" not in await tag_of(kind="verify")  # 검증은 소스를 고치지 않으니 빌드하지 않는다
    monkeypatch.setattr(get_settings(), "game_auto_build", False)
    assert "auto_build" not in await tag_of(kind="modify")  # 운영 설정으로 끌 수 있다


async def test_run_finish_hook_builds_game_created_from_idea_end_to_end(client, auth, fake_adapter, auto_on):  # noqa: F811
    r = await client.post("/api/games/create", json={"idea": "점프하는 도트 고양이", "slug": "ab-e2e"}, headers=auth)
    assert r.status_code == 201, r.text
    out = r.json()
    run_id, gid = out["request"]["run_id"], out["game"]["id"]
    assert (await client.get(f"/api/runs/{run_id}", headers=auth)).json()["usage"]["game"]["auto_build"] is True
    assert (await wait_run(client, auth, run_id))["status"] == "succeeded"
    deadline = asyncio.get_running_loop().time() + 20
    while True:  # Run 종료 처리가 빌드를 따로 띄운다
        rows = (await client.get(f"/api/games/{gid}/builds", headers=auth)).json()
        if rows and rows[0]["status"] != "building":
            break
        assert asyncio.get_running_loop().time() < deadline, rows
        await asyncio.sleep(0.1)
    assert len(rows) == 1 and rows[0]["status"] == "succeeded" and rows[0]["run_id"] == run_id
    game = (await client.get(f"/api/games/{gid}", headers=auth)).json()
    assert game["active_build_id"] == rows[0]["id"] and game["last_build"]["id"] == rows[0]["id"]
