"""아이디어 한 줄 → 게임 생성 API: scaffold·등록·잠금 승인·첫 개발 요청, 거부와 정리 (TASK-G15, ADR 0053)."""

import json
import shutil
import subprocess
import uuid
from pathlib import Path

import pytest
from helpers import wait_run
from sqlalchemy import func, select
from test_game_build_snapshots import _cleanup_snapshots  # noqa: F401

from ttakkari.db import sessionmaker
from ttakkari.games import create as game_create
from ttakkari.games.contract import MANIFEST_FILE, game_dir
from ttakkari.models import AuditLog, GameProject, PmsProject, Run, Workspace

IDEA = "2D 로그라이크 한 판짜리: 방 이동, 적, 공격, 체력, 게임오버"


@pytest.fixture(autouse=True)
def _clean_created_dirs():
    yield
    for p in game_dir("x-probe").parent.iterdir():
        if p.name.startswith(("cr-", "game-")) or p.name in {"dot-dungeon", "flappy-dot"}:
            shutil.rmtree(p, ignore_errors=True)


async def create(client, auth, **body):
    return await client.post("/api/games/create", json={"idea": IDEA, **body}, headers=auth)


def git_log(root: Path) -> list[str]:
    return subprocess.run(["git", "log", "--format=%s"], cwd=root, capture_output=True, text=True, check=True).stdout.split("\n")[:-1]


def test_derive_name_and_slug():
    assert game_create.derive_name("  점프하는   고양이 게임\n둘째 줄") == "점프하는 고양이 게임"
    assert game_create.derive_name("x" * 200) == "x" * game_create.MAX_NAME_CHARS
    assert game_create.derive_name("   ") == "새 게임"
    assert game_create.derive_slug("Flappy Dot!!") == "flappy-dot"
    assert game_create.derive_slug("점프하는 고양이").startswith("game-")  # 한글만 있으면 무작위 slug
    assert game_create.derive_slug("점프하는 고양이") != game_create.derive_slug("점프하는 고양이")
    assert game_create.derive_slug("a" * 80) == "a" * game_create.SLUG_BASE_MAX


async def test_create_requires_authentication(client):
    for headers in ({}, {"Authorization": "Bearer nope"}):
        assert (await client.post("/api/games/create", json={"idea": IDEA}, headers=headers)).status_code == 401


async def test_create_scaffolds_registers_approves_and_queues_first_develop_request(client, auth):
    r = await create(client, auth, name="Flappy Dot", slug="cr-flappy", genre="아케이드",
                     locked_decisions=["2D 도트 픽셀 그래픽", "  세로 모바일 터치  "])
    assert r.status_code == 201, r.text
    out = r.json()
    game = out["game"]
    assert game["slug"] == "cr-flappy" and game["name"] == "Flappy Dot" and game["engine_id"] == "web-phaser" and game["genre"] == "아케이드"
    assert game["locked_decisions"] == ["2D 도트 픽셀 그래픽", "세로 모바일 터치"]  # 공백은 정리된다
    assert game["lock_approved"] is True and game["lock_matches_repo"] is True and game["locked_revision"] == 1
    assert game["pms_project_id"] and game["last_build"] is None
    # 게임 저장소: scaffold 커밋 하나, 매니페스트가 요청과 일치
    root = game_dir("cr-flappy")
    assert git_log(root) == ["scaffold web-phaser project"]
    mf = json.loads((root / MANIFEST_FILE).read_text())
    assert mf["id"] == "cr-flappy" and mf["engine"] == "web-phaser" and mf["entrypoint"] == "index.html"
    assert mf["locked_decisions"] == ["2D 도트 픽셀 그래픽", "세로 모바일 터치"]
    assert (root / "src" / "bridge.ts").is_file()
    # 첫 개발 요청: 기존 worker 세션·Run, develop 으로 접수되고 prompt 에 아이디어와 잠금 결정이 들어간다
    req = out["request"]
    assert out["request_error"] is None and req["kind"] == "develop" and req["lock"] == "verified" and req["build_id"] is None
    run = (await client.get(f"/api/runs/{req['run_id']}", headers=auth)).json()
    sess = (await client.get(f"/api/sessions/{req['session_id']}", headers=auth)).json()
    assert sess["kind"] == "worker" and sess["workspace_id"] == game["workspace_id"] and sess["worker_task_type"] == "general"
    for needle in (IDEA, "Flappy Dot", "2D 도트 픽셀 그래픽", "세로 모바일 터치", "installBridge", "publishState", "게임을 개발한다"):
        assert needle in run["prompt"], needle
    assert run["usage"]["game"]["kind"] == "develop"
    await wait_run(client, auth, req["run_id"])
    listed = (await client.get(f"/api/games/{game['id']}/requests", headers=auth)).json()
    assert [x["run_id"] for x in listed] == [req["run_id"]]
    # 감사 기록: 생성과 잠금 승인이 각각 남는다
    async with sessionmaker()() as db:
        actions = set(await db.scalars(select(AuditLog.action).where(AuditLog.target_id == game["id"])))
        assert {"game.create", "game.lock.approve"} <= actions


async def test_create_without_decisions_leaves_lock_unapproved_and_can_skip_request(client, auth):
    r = await create(client, auth, slug="cr-plain", start_development=False, create_pms=False)
    assert r.status_code == 201, r.text
    out = r.json()
    assert out["request"] is None and out["request_error"] is None
    assert out["game"]["lock_approved"] is False and out["game"]["pms_project_id"] is None and out["game"]["name"] == IDEA[:game_create.MAX_NAME_CHARS].rstrip()
    async with sessionmaker()() as db:
        assert await db.scalar(select(func.count()).select_from(Run).where(Run.workspace_id == uuid.UUID(out["game"]["workspace_id"]))) == 0


async def test_create_derives_ascii_slug_and_avoids_collisions(client, auth):
    a = (await create(client, auth, name="CR Dot Run", start_development=False)).json()["game"]
    assert a["slug"] == "cr-dot-run"
    b = (await create(client, auth, name="CR Dot Run", start_development=False)).json()["game"]
    assert b["slug"].startswith("cr-dot-run-") and b["slug"] != a["slug"]  # 같은 이름이어도 덮어쓰지 않는다
    k = (await create(client, auth, name="점프 고양이", start_development=False)).json()["game"]
    assert k["slug"].startswith("game-") and k["name"] == "점프 고양이"


async def test_create_rejects_taken_explicit_slug_without_touching_existing_game(client, auth):
    first = (await create(client, auth, slug="cr-taken", start_development=False)).json()["game"]
    root = game_dir("cr-taken")
    (root / "marker.txt").write_text("keep")
    r = await create(client, auth, slug="cr-taken", start_development=False)
    assert r.status_code == 409 and r.json()["code"] == "exists"
    assert (root / "marker.txt").read_text() == "keep"  # 실패한 호출이 기존 게임을 지우지 않는다
    assert (await client.get(f"/api/games/{first['id']}", headers=auth)).status_code == 200
    # 디스크에만 있는 디렉터리(등록되지 않은 저장소)도 덮어쓰지 않는다
    stray = game_dir("cr-stray")
    stray.mkdir()
    (stray / "mine.txt").write_text("mine")
    assert (await create(client, auth, slug="cr-stray", start_development=False)).status_code == 409
    assert (stray / "mine.txt").read_text() == "mine"


async def test_create_validates_input(client, auth):
    cases = [
        {"idea": ""}, {"idea": "   "}, {"idea": "x" * 2001}, {"slug": "../escape"}, {"slug": "UPPER"}, {"slug": "a"},
        {"slug": "-bad-"}, {"slug": "x" * 41}, {"engine": "godot"}, {"locked_decisions": ["a"] * 21}, {"locked_decisions": [""]},
        {"locked_decisions": ["x" * 501]}, {"name": "n" * 61}, {"genre": "g" * 61}, {"root_path": "/etc"},
    ]
    for extra in cases:
        body = {"idea": IDEA, **extra}
        r = await client.post("/api/games/create", json=body, headers=auth)
        assert r.status_code in (409, 422), (extra, r.status_code, r.text)
        assert r.status_code == 422 or r.json()["code"] in {"bad_request", "bad_slug"}
    async with sessionmaker()() as db:
        assert await db.scalar(select(func.count()).select_from(GameProject)) == 0
    names = {p.name for p in game_dir("x-probe").parent.iterdir()}
    assert not any(n.startswith(("game-", "cr-")) or n in {"escape", "etc"} for n in names)  # 거부된 입력이 디렉터리를 남기지 않는다


async def test_create_failure_after_scaffold_removes_only_the_new_directory(client_500, auth, monkeypatch):
    async def boom(*a, **kw):
        raise RuntimeError("db down")

    monkeypatch.setattr(game_create, "register_game_project", boom)
    other = game_dir("cr-bystander")
    other.mkdir()
    (other / "x.txt").write_text("x")
    r = await client_500.post("/api/games/create", json={"idea": IDEA, "slug": "cr-fail"}, headers=auth)
    assert r.status_code == 500
    assert not game_dir("cr-fail").exists()  # 등록 실패 시 scaffold 결과를 정리한다
    assert (other / "x.txt").exists()
    async with sessionmaker()() as db:
        assert await db.scalar(select(func.count()).select_from(GameProject).where(GameProject.slug == "cr-fail")) == 0
        assert await db.scalar(select(func.count()).select_from(Workspace).where(Workspace.root_path == str(game_dir("cr-fail")))) == 0


async def test_first_request_failure_keeps_game_and_reports_error(client, auth, monkeypatch):
    from ttakkari.games import worker as game_worker

    async def refuse(*a, **kw):
        raise game_worker.GameContractError("worker 를 시작할 수 없습니다.", "no_workspace")

    monkeypatch.setattr(game_worker, "start_request", refuse)
    r = await create(client, auth, slug="cr-norun")
    assert r.status_code == 201, r.text
    out = r.json()
    assert out["request"] is None and out["request_error"]["code"] == "no_workspace"
    assert (await client.get(f"/api/games/{out['game']['id']}", headers=auth)).status_code == 200  # 게임은 남아 관제탑에서 다시 요청할 수 있다


async def test_create_attaches_pms_project_only_when_requested(client, auth):
    async with sessionmaker()() as db:
        before = await db.scalar(select(func.count()).select_from(PmsProject))
    await create(client, auth, slug="cr-nopms", create_pms=False, start_development=False)
    await create(client, auth, slug="cr-pms", create_pms=True, start_development=False)
    async with sessionmaker()() as db:
        assert await db.scalar(select(func.count()).select_from(PmsProject)) == before + 1
