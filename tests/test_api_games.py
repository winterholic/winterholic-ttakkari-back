"""게임 프로젝트·빌드 API: 인증, 등록 거부(잘못된 경로·slug·ID), 잠금 승인, 비동기 빌드 상태·목록 (TASK-G06)."""

import asyncio
import json
import uuid
from pathlib import Path

import pytest
from helpers import fresh_dir
from sqlalchemy import select
from test_game_build_snapshots import (  # noqa: F401
    RUNTIME,
    FakeAdapter,
    git,
    manifest,
    requires_runtime,
)

from ttakkari.config import get_settings
from ttakkari.db import sessionmaker
from ttakkari.games import builds
from ttakkari.games.adapter import PhaserAdapter
from ttakkari.games.builds import delete_snapshot_files
from ttakkari.games.contract import game_dir
from ttakkari.models import GameBuild, PmsProject, utcnow


@pytest.fixture(autouse=True)
def _cleanup_snapshots():
    yield
    root = get_settings().game_builds_dir
    if root.exists():
        for child in root.iterdir():
            delete_snapshot_files(child)


@pytest.fixture
def fake_adapter(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(builds, "get_adapter", lambda engine_id, **kw: FakeAdapter(tmp_path / "w"))


def make_repo(slug: str) -> Path:
    root = fresh_dir(game_dir(slug))
    (root / "ttakkari-game.json").write_text(json.dumps(manifest(slug).to_json(), ensure_ascii=False))
    (root / "a.txt").write_text("v1")
    git(root, "init", "-q", "-b", "main")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "init")
    return root


async def register(client, auth, slug: str, **extra):
    return await client.post("/api/games", json={"slug": slug, **extra}, headers=auth)


async def wait_build(client, auth, game_id: str, build_id: str, timeout: float = 300) -> dict:
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        r = await client.get(f"/api/games/{game_id}/builds/{build_id}", headers=auth)
        assert r.status_code == 200
        if r.json()["status"] != "building":
            return r.json()
        assert asyncio.get_running_loop().time() < deadline, "빌드가 끝나지 않았다"
        await asyncio.sleep(0.1)


async def test_all_endpoints_require_authentication(client):
    gid, bid = uuid.uuid4(), uuid.uuid4()
    calls = [
        ("GET", "/api/games"), ("POST", "/api/games"), ("GET", f"/api/games/{gid}"), ("POST", f"/api/games/{gid}/lock/approve"),
        ("GET", f"/api/games/{gid}/builds"), ("POST", f"/api/games/{gid}/builds"), ("GET", f"/api/games/{gid}/builds/{bid}"),
    ]
    for method, url in calls:
        r = await client.request(method, url, json={"slug": "abc-game"} if method == "POST" else None)
        assert r.status_code == 401, (method, url, r.status_code)
        r = await client.request(method, url, headers={"Authorization": "Bearer nope"}, json={"slug": "abc-game"} if method == "POST" else None)
        assert r.status_code == 401, (method, url, r.status_code)


async def test_register_links_workspace_and_pms_and_rejects_duplicates(client, auth):
    make_repo("api-one")
    r = await register(client, auth, "api-one")
    assert r.status_code == 201, r.text
    game = r.json()
    assert game["slug"] == "api-one" and game["engine_id"] == "web-phaser" and game["pms_project_id"]
    assert game["lock_approved"] is False and game["lock_matches_repo"] is False and game["last_build"] is None
    assert game["locked_decisions"] == ["2D", "터치 우선"]
    ws = (await client.get(f"/api/workspaces/{game['workspace_id']}", headers=auth)).json()
    assert ws["root_path"] == str(game_dir("api-one")) and ws["is_git"] is True
    assert [g["id"] for g in (await client.get("/api/games", headers=auth)).json()] == [game["id"]]
    assert (await client.get(f"/api/games/{game['id']}", headers=auth)).json()["id"] == game["id"]
    assert (await register(client, auth, "api-one")).status_code == 409


async def test_register_attaches_to_existing_pms_project_and_rejects_unknown_one(client, auth):
    make_repo("api-pms")
    async with sessionmaker()() as db:
        pms = PmsProject(name="기존 프로젝트", status="active")
        db.add(pms)
        await db.commit()
        pms_id = pms.id
    assert (await register(client, auth, "api-pms", pms_project_id=str(uuid.uuid4()))).status_code == 404
    r = await register(client, auth, "api-pms", pms_project_id=str(pms_id))
    assert r.status_code == 201 and r.json()["pms_project_id"] == str(pms_id)
    async with sessionmaker()() as db:
        assert len((await db.scalars(select(PmsProject))).all()) == 1  # 새 프로젝트를 만들지 않았다


async def test_register_rejects_bad_paths_slugs_and_unknown_fields(client, auth, tmp_path: Path):
    assert (await register(client, auth, "no-such-game")).status_code == 404  # 저장소 없음
    fresh_dir(game_dir("no-manifest"))
    assert (await register(client, auth, "no-manifest")).json()["code"] == "no_manifest"
    for bad in ("../escape", "a/b", "UPPER-CASE", "-bad-slug", "x" * 41, "..", "/etc"):
        r = await register(client, auth, bad)
        assert r.status_code in (404, 422), (bad, r.status_code)
        assert r.status_code == 422 or r.json()["code"] != "ok"
    r = await client.post("/api/games", json={"slug": "api-extra", "root_path": str(tmp_path)}, headers=auth)
    assert r.status_code == 422  # 임의 경로를 받지 않는다
    root = make_repo("api-mismatch")  # 매니페스트 id 가 디렉터리와 다르면 거부
    (root / "ttakkari-game.json").write_text(json.dumps(manifest("other-name").to_json()))
    r = await register(client, auth, "api-mismatch")
    assert r.status_code == 422 and r.json()["code"] == "bad_manifest"
    link = game_dir("api-linked")
    link.symlink_to(tmp_path)
    try:
        assert (await register(client, auth, "api-linked")).status_code in (400, 404, 422)
    finally:
        link.unlink()
    assert (await client.get("/api/games", headers=auth)).json() == []


async def test_unknown_ids_are_404_and_builds_do_not_cross_games(client, auth, fake_adapter):
    for url in ("", "/lock/approve", "/builds", f"/builds/{uuid.uuid4()}"):
        method = client.post if url == "/lock/approve" else client.get
        r = await method(f"/api/games/{uuid.uuid4()}{url}", headers=auth)
        assert r.status_code == 404, url
    assert (await client.post(f"/api/games/{uuid.uuid4()}/builds", json={}, headers=auth)).status_code == 404
    assert (await client.get("/api/games/not-a-uuid", headers=auth)).status_code == 422
    make_repo("api-a")
    make_repo("api-b")
    a = (await register(client, auth, "api-a")).json()
    b = (await register(client, auth, "api-b")).json()
    started = (await client.post(f"/api/games/{a['id']}/builds", json={}, headers=auth)).json()
    await wait_build(client, auth, a["id"], started["id"])
    assert (await client.get(f"/api/games/{b['id']}/builds/{started['id']}", headers=auth)).status_code == 404
    assert (await client.get(f"/api/games/{b['id']}/builds", headers=auth)).json() == []


async def test_lock_approval_flow_and_repo_drift(client, auth):
    root = make_repo("api-lock")
    game = (await register(client, auth, "api-lock")).json()
    r = await client.post(f"/api/games/{game['id']}/lock/approve", headers=auth)
    assert r.status_code == 200
    assert (r.json()["lock_approved"], r.json()["lock_matches_repo"], r.json()["locked_revision"]) == (True, True, 1)
    assert (await client.post(f"/api/games/{game['id']}/lock/approve", headers=auth)).json()["locked_revision"] == 1
    (root / "ttakkari-game.json").write_text(json.dumps(manifest("api-lock", locked_decisions=("전부 3D",)).to_json()))  # 에이전트가 파일을 고침
    drift = (await client.get(f"/api/games/{game['id']}", headers=auth)).json()
    assert drift["lock_matches_repo"] is False and drift["locked_decisions"] == ["전부 3D"] and drift["locked_revision"] == 1
    (root / "ttakkari-game.json").unlink()
    gone = (await client.get(f"/api/games/{game['id']}", headers=auth)).json()
    assert gone["lock_matches_repo"] is None and gone["locked_decisions"] == []
    assert (await client.post(f"/api/games/{game['id']}/lock/approve", headers=auth)).status_code == 404


async def test_build_runs_in_background_and_is_listed_with_status(client, auth, fake_adapter):
    root = make_repo("api-build")
    game = (await register(client, auth, "api-build")).json()
    await client.post(f"/api/games/{game['id']}/lock/approve", headers=auth)
    r = await client.post(f"/api/games/{game['id']}/builds", json={}, headers=auth)
    assert r.status_code == 202
    first = r.json()
    assert first["seq"] == 1 and first["status"] in ("building", "succeeded") and "bundle_rel_path" not in first
    done = await wait_build(client, auth, game["id"], first["id"])
    assert done["status"] == "succeeded" and done["lock_verified"] is True and done["source_dirty"] is False
    assert done["source_commit"] == git(root, "rev-parse", "HEAD") and len(done["bundle_sha256"]) == 64 and done["artifact_id"]
    assert "log_tail" in done and done["snapshot_verified"] is None
    verified = (await client.get(f"/api/games/{game['id']}/builds/{first['id']}?verify=true", headers=auth)).json()
    assert verified["snapshot_verified"] is True
    second = (await client.post(f"/api/games/{game['id']}/builds", json={}, headers=auth)).json()
    assert second["seq"] == 2
    await wait_build(client, auth, game["id"], second["id"])
    listing = (await client.get(f"/api/games/{game['id']}/builds", headers=auth)).json()
    assert [b["seq"] for b in listing] == [2, 1] and all("log_tail" not in b for b in listing)
    assert (await client.get(f"/api/games/{game['id']}", headers=auth)).json()["last_build"]["id"] == second["id"]
    art = (await client.get(f"/api/artifacts/{done['artifact_id']}", headers=auth)).json()
    assert art["filename"] == "api-build-v1.zip"


async def test_build_rejections_and_failure_status(client, auth, monkeypatch, tmp_path: Path):
    make_repo("api-fail")
    game = (await register(client, auth, "api-fail")).json()
    monkeypatch.setattr(builds, "get_adapter", lambda engine_id, **kw: FakeAdapter(tmp_path / "w", fail="빌드 실패(exit 2)"))
    assert (await client.post(f"/api/games/{game['id']}/builds", json={"run_id": str(uuid.uuid4())}, headers=auth)).status_code == 404
    assert (await client.post(f"/api/games/{game['id']}/builds", json={"root_path": "/etc"}, headers=auth)).status_code == 422
    async with sessionmaker()() as db:
        assert (await db.scalars(select(GameBuild))).all() == []  # 거부된 요청은 행을 남기지 않는다
    r = await client.post(f"/api/games/{game['id']}/builds", json={}, headers=auth)
    failed = await wait_build(client, auth, game["id"], r.json()["id"])
    assert failed["status"] == "failed" and failed["error"] == "빌드 실패(exit 2)" and failed["artifact_id"] is None
    assert "TS2304" in failed["log_tail"] and failed["finished_at"]


async def test_second_build_while_one_is_running_is_conflict(client, auth, fake_adapter):
    make_repo("api-busy")
    game = (await register(client, auth, "api-busy")).json()
    async with sessionmaker()() as db:
        db.add(GameBuild(game_id=uuid.UUID(game["id"]), seq=1, status="building", created_at=utcnow()))
        await db.commit()
    r = await client.post(f"/api/games/{game['id']}/builds", json={}, headers=auth)
    assert r.status_code == 409 and r.json()["code"] == "build_in_progress"


@requires_runtime
async def test_real_phaser_game_registered_and_built_through_api(client, auth):
    root = game_dir("api-real")
    await PhaserAdapter().scaffold_project(root, manifest("api-real"))
    game = (await register(client, auth, "api-real")).json()
    await client.post(f"/api/games/{game['id']}/lock/approve", headers=auth)
    started = await client.post(f"/api/games/{game['id']}/builds", json={}, headers=auth)
    assert started.status_code == 202
    done = await wait_build(client, auth, game["id"], started.json()["id"])
    assert done["status"] == "succeeded", done["error"]
    assert done["lock_verified"] and done["bundle_files"] >= 2 and done["bundle_bytes"] > 500_000
    assert git(root, "rev-parse", "HEAD") == done["source_commit"]
    assert (await client.get(f"/api/games/{game['id']}/builds/{done['id']}?verify=true", headers=auth)).json()["snapshot_verified"] is True
