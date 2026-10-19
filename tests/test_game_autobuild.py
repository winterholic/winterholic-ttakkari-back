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


# ---- 요청 기록: 종료 시점의 잠금 상태·자동 빌드 결과, 타입 검사 실패의 수리 요청 (ADR 0058) -----------------


async def request_rows(client, auth, game_id: str) -> list[dict]:
    return (await client.get(f"/api/games/{game_id}/requests", headers=auth)).json()


async def test_request_record_shows_lock_violation_and_skipped_auto_build(client, auth, fake_adapter):  # noqa: F811
    game = await new_game(client, auth, "ab-record-lock")
    run_id = await make_run(game)  # 요청 시점에는 잠금이 승인본과 같았다
    drift_decisions("ab-record-lock")  # Run 도중 worker 가 승인된 잠금 결정을 바꿨다
    assert await autobuild.after_run(run_id) is None
    [row] = await request_rows(client, auth, game["id"])
    assert row["lock"] == "verified" and row["lock_after"] == "tampered"  # 요청 시점 값은 그대로 두고 종료 시점 값을 따로 둔다
    assert row["auto_build_result"]["status"] == "skipped" and row["auto_build_result"]["code"] == "lock_violation"
    assert (await client.post(f"/api/games/{game['id']}/builds", json={}, headers=auth)).status_code == 409  # 수동 빌드도 같은 사유로 거부
    assert await all_builds(game["id"]) == []
    # 사용자가 바뀐 결정을 다시 승인하는 정식 경로: 그 뒤에는 빌드가 되고 활성 빌드가 새 승인본으로 이어진다
    assert (await client.post(f"/api/games/{game['id']}/lock/approve", headers=auth)).json()["lock_matches_repo"] is True
    b = (await client.post(f"/api/games/{game['id']}/builds", json={}, headers=auth)).json()
    assert b["status"] in ("building", "succeeded")


async def test_request_record_for_successful_and_unchanged_auto_builds(client, auth, fake_adapter):  # noqa: F811
    game = await new_game(client, auth, "ab-record-ok")
    first = await make_run(game)
    build = await autobuild.after_run(first)
    again = await make_run(game, kind="modify")
    assert await autobuild.after_run(again) is None
    failed_run = await make_run(game, status=RunStatus.failed, kind="modify")
    assert await autobuild.after_run(failed_run) is None
    rows = {r["run_id"]: r for r in await request_rows(client, auth, game["id"])}
    assert rows[str(first)]["lock_after"] == "verified"
    assert rows[str(first)]["auto_build_result"] == {"status": "succeeded", "build_id": str(build.id), "seq": 1}
    assert rows[str(again)]["auto_build_result"] == {"status": "skipped", "code": "no_change"}
    assert rows[str(failed_run)]["auto_build_result"] == {"status": "skipped", "code": "run_not_succeeded"}


TSC_FAIL = "타입 검사 실패(tsc exit 2): 오류 1건\nsrc/game.ts(12,7): error TS2322: Type 'string' is not assignable to type 'number'."


async def worker_runs(game_id: str) -> list[Run]:
    async with sessionmaker()() as db:
        gid = uuid.UUID(game_id)
        ws = (await db.execute(select(Run).join(ChatSession, ChatSession.id == Run.session_id))).scalars().all()
        return [r for r in ws if ((r.usage or {}).get("game") or {}).get("game_id") == str(gid)]


async def test_typecheck_failure_is_returned_to_worker_once_with_the_errors(client, auth, monkeypatch, tmp_path, auto_on):
    from test_game_build_snapshots import FakeAdapter

    monkeypatch.setattr(builds, "get_adapter", lambda engine_id, **kw: FakeAdapter(tmp_path / "w", fail=TSC_FAIL))
    game = await new_game(client, auth, "ab-repair")
    run_id = await make_run(game)
    build = await autobuild.after_run(run_id)
    assert build is not None and build.status == "failed"
    [original, *_] = [r for r in await request_rows(client, auth, game["id"]) if r["run_id"] == str(run_id)]
    assert original["auto_build_result"]["code"] == "typecheck_failed" and "TS2322" in original["auto_build_result"]["message"]
    # 수리 요청이 같은 게임에 자동으로 한 번 접수됐고, 프롬프트에 오류가 실려 있다
    repairs = [r for r in await worker_runs(game["id"]) if r.id != run_id and (r.usage["game"].get("repair_attempt") == 1)]
    assert len(repairs) == 1
    r = repairs[0]
    assert str(r.origin) == "system" and r.usage["game"]["kind"] == "modify" and r.usage["game"]["auto_build"] is True
    assert "[직전 빌드 실패]" in r.prompt and "src/game.ts(12,7): error TS2322" in r.prompt and "타입 검사를 통과" in r.prompt
    await wait_run(client, auth, str(r.id))
    # 수리 Run 이 끝나 같은 이유로 다시 실패해도 상한(game_auto_repair_attempts=1)을 넘어 접수하지 않는다
    deadline = asyncio.get_running_loop().time() + 20
    while len([b for b in await all_builds(game["id"]) if b.status == "failed"]) < 2 and asyncio.get_running_loop().time() < deadline:
        await asyncio.sleep(0.2)
    await asyncio.sleep(1)
    assert len([r for r in await worker_runs(game["id"]) if r.usage["game"].get("repair_attempt")]) == 1


async def test_typecheck_repair_can_be_disabled_and_other_failures_do_not_repair(client, auth, monkeypatch, tmp_path, auto_on):
    from test_game_build_snapshots import FakeAdapter

    monkeypatch.setattr(get_settings(), "game_auto_repair_attempts", 0)
    monkeypatch.setattr(builds, "get_adapter", lambda engine_id, **kw: FakeAdapter(tmp_path / "w", fail=TSC_FAIL))
    off = await new_game(client, auth, "ab-repair-off")
    assert (await autobuild.after_run(await make_run(off))).status == "failed"
    assert [r for r in await worker_runs(off["id"]) if r.usage["game"].get("repair_attempt")] == []
    monkeypatch.setattr(get_settings(), "game_auto_repair_attempts", 1)
    monkeypatch.setattr(builds, "get_adapter", lambda engine_id, **kw: FakeAdapter(tmp_path / "w2", fail="vite 번들 오류"))
    other = await new_game(client, auth, "ab-repair-other")
    assert (await autobuild.after_run(await make_run(other))).status == "failed"
    assert [r for r in await worker_runs(other["id"]) if r.usage["game"].get("repair_attempt")] == []  # 타입 검사 실패만 되돌린다
    row = (await request_rows(client, auth, other["id"]))[0]
    assert row["auto_build_result"]["code"] == "build_failed"
