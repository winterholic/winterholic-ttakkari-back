"""롤백 API: 활성 빌드 전환, 이전 산출물 불변, 소스 복구 커밋·복구 빌드, 잠금 결정 거부 (TASK-G12 백엔드)."""

import hashlib
import json
import uuid
from pathlib import Path

import pytest
from sqlalchemy import func, select
from test_api_games import make_repo, register, wait_build
from test_game_build_snapshots import (  # noqa: F401
    FakeAdapter,
    _cleanup_snapshots,
    git,
    manifest,
)
from test_game_serving_security import game_origin, play  # noqa: F401

from ttakkari.db import sessionmaker
from ttakkari.games import builds
from ttakkari.games.builds import bundle_path, verify_snapshot
from ttakkari.games.contract import game_dir
from ttakkari.models import Artifact, GameBuild

FILES_V1 = {"index.html": "<!doctype html><title>v1</title>", "assets/app.js": "console.log('v1')"}
FILES_V2 = {"index.html": "<!doctype html><title>v2</title>", "assets/app.js": "console.log('v2')"}


@pytest.fixture
def adapter(monkeypatch, tmp_path: Path) -> FakeAdapter:
    a = FakeAdapter(tmp_path / "w", files=dict(FILES_V1))
    monkeypatch.setattr(builds, "get_adapter", lambda engine_id, **kw: a)
    return a


async def new_game(client, auth, slug: str) -> tuple[dict, Path]:
    root = make_repo(slug)
    game = (await register(client, auth, slug)).json()
    assert (await client.post(f"/api/games/{game['id']}/lock/approve", headers=auth)).status_code == 200
    return game, root


async def build(client, auth, game_id: str) -> dict:
    r = await client.post(f"/api/games/{game_id}/builds", json={}, headers=auth)
    assert r.status_code == 202, r.text
    done = await wait_build(client, auth, game_id, r.json()["id"])
    assert done["status"] == "succeeded", done
    return done


async def rollback(client, auth, game_id: str, build_id: str, **extra):
    return await client.post(f"/api/games/{game_id}/rollback", json={"build_id": build_id, "confirm": True, **extra}, headers=auth)


async def game_of(client, auth, game_id: str) -> dict:
    return (await client.get(f"/api/games/{game_id}", headers=auth)).json()


def tree_digest(bundle: Path) -> dict[str, str]:
    return {p.relative_to(bundle).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(bundle.rglob("*")) if p.is_file()}


async def snapshot_state(build_ids: list[str]) -> dict:
    """빌드별 번들 해시·파일 해시·권한·zip Artifact 해시를 한꺼번에 기록한다(롤백 전후 비교용)."""
    out = {}
    async with sessionmaker()() as db:
        for bid in build_ids:
            b = await db.get(GameBuild, uuid.UUID(bid))
            bundle = bundle_path(b)
            art = await db.get(Artifact, b.artifact_id)
            out[bid] = {"sha": b.bundle_sha256, "files": tree_digest(bundle), "mode": oct(bundle.stat().st_mode), "zip": hashlib.sha256(Path(art.stored_path).read_bytes()).hexdigest(),
                        "commit": b.source_commit, "ok": verify_snapshot(b), "status": b.status}
    return out


# ---- 요청 검증 ---------------------------------------------------------------------------------------


async def test_rollback_requires_auth_explicit_confirmation_and_a_valid_build(client, auth, adapter):
    gid = uuid.uuid4()
    assert (await client.post(f"/api/games/{gid}/rollback", json={"build_id": str(uuid.uuid4()), "confirm": True})).status_code == 401
    assert (await rollback(client, auth, str(gid), str(uuid.uuid4()))).status_code == 404
    game, _ = await new_game(client, auth, "rb-valid")
    other, _ = await new_game(client, auth, "rb-valid2")
    b1 = await build(client, auth, game["id"])
    other_b = await build(client, auth, other["id"])
    for body in ({"build_id": b1["id"]}, {"build_id": b1["id"], "confirm": False}, {"build_id": b1["id"], "confirm": True, "x": 1}, {"confirm": True}):
        assert (await client.post(f"/api/games/{game['id']}/rollback", json=body, headers=auth)).status_code == 422, body
    r = await rollback(client, auth, game["id"], str(uuid.uuid4()))
    assert r.status_code == 404 and r.json()["code"] == "no_build"
    r = await rollback(client, auth, game["id"], other_b["id"])  # 다른 게임의 빌드
    assert r.status_code == 404
    adapter.fail = "빌드 실패(exit 2)"
    failed = (await client.post(f"/api/games/{game['id']}/builds", json={}, headers=auth)).json()
    assert (await wait_build(client, auth, game["id"], failed["id"]))["status"] == "failed"
    r = await rollback(client, auth, game["id"], failed["id"])  # 실패 빌드는 활성이 될 수 없다
    assert r.status_code == 409 and r.json()["code"] == "not_playable"
    assert (await game_of(client, auth, game["id"]))["active_build_id"] == b1["id"]


# ---- 포인터 롤백: 이전 번들 해시가 그대로 ---------------------------------------------------------------


async def test_rollback_switches_active_build_and_never_touches_previous_artifacts(client, auth, adapter, game_origin):  # noqa: F811
    game, root = await new_game(client, auth, "rb-pointer")
    v1 = await build(client, auth, game["id"])
    (root / "a.txt").write_text("v2")
    git(root, "commit", "-qam", "v2")
    adapter.files = dict(FILES_V2)
    v2 = await build(client, auth, game["id"])
    assert v1["bundle_sha256"] != v2["bundle_sha256"] and (v1["seq"], v2["seq"]) == (1, 2)
    assert (await game_of(client, auth, game["id"]))["active_build_id"] == v2["id"]  # 새 성공 빌드가 활성을 따라간다
    before = await snapshot_state([v1["id"], v2["id"]])
    head = git(root, "rev-parse", "HEAD")

    r = await rollback(client, auth, game["id"], v1["id"])
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["active_build_id"] == v1["id"] and out["previous_active_build_id"] == v2["id"] and out["restore_commit"] is None and out["restore_build"] is None
    shown = await game_of(client, auth, game["id"])
    assert shown["active_build_id"] == v1["id"] and shown["last_build"]["id"] == v2["id"]
    # 이전 산출물은 바이트 단위로 같고, 읽기 전용 권한과 zip Artifact 도 그대로이며 소스도 건드리지 않았다
    after = await snapshot_state([v1["id"], v2["id"]])
    assert after == before and all(s["ok"] for s in after.values())
    assert before[v1["id"]]["sha"] == v1["bundle_sha256"]
    assert git(root, "rev-parse", "HEAD") == head and git(root, "status", "--porcelain") == ""
    assert len((await client.get(f"/api/games/{game['id']}/builds", headers=auth)).json()) == 2  # 새 빌드 행 없음
    # 활성 빌드가 곧 플레이 대상이다
    url = (await play(client, auth, game["id"])).json()
    assert url["build_id"] == v1["id"] and url["bundle_sha256"] == v1["bundle_sha256"]
    # 같은 빌드로 다시 롤백은 거부, 롤백 뒤 만든 새 빌드는 고정을 덮어쓰지 않는다
    assert (await rollback(client, auth, game["id"], v1["id"])).json()["code"] == "already_active"
    adapter.files = dict(FILES_V2) | {"extra.txt": "v3"}
    v3 = await build(client, auth, game["id"])
    assert (await game_of(client, auth, game["id"]))["active_build_id"] == v1["id"]
    assert (await rollback(client, auth, game["id"], v3["id"])).json()["active_build_id"] == v3["id"]  # 앞으로 이동도 같은 API
    assert (await snapshot_state([v1["id"], v2["id"]])) == before


async def test_rollback_refuses_tampered_snapshot(client, auth, adapter):
    game, _ = await new_game(client, auth, "rb-tamper")
    v1 = await build(client, auth, game["id"])
    adapter.files = dict(FILES_V2)
    v2 = await build(client, auth, game["id"])
    async with sessionmaker()() as db:
        b = await db.get(GameBuild, uuid.UUID(v1["id"]))
        builds._thaw(bundle_path(b))
        (bundle_path(b) / "index.html").write_text("hacked")
    r = await rollback(client, auth, game["id"], v1["id"])
    assert r.status_code == 409 and r.json()["code"] == "snapshot_tampered"
    assert (await game_of(client, auth, game["id"]))["active_build_id"] == v2["id"]


# ---- 소스 복구 -----------------------------------------------------------------------------------------


async def test_restore_source_creates_recovery_commit_and_new_build_keeping_originals(client, auth, adapter):
    game, root = await new_game(client, auth, "rb-restore")
    v1 = await build(client, auth, game["id"])
    c1 = v1["source_commit"]
    (root / "a.txt").write_text("v2")
    (root / "b.txt").write_text("only in v2")
    git(root, "add", "-A")
    git(root, "commit", "-qm", "v2")
    c2 = git(root, "rev-parse", "HEAD")
    adapter.files = dict(FILES_V2)
    v2 = await build(client, auth, game["id"])
    before = await snapshot_state([v1["id"], v2["id"]])
    adapter.files = dict(FILES_V1)  # 되돌린 소스를 빌드하면 v1 과 같은 결과가 나오는 상황

    r = await rollback(client, auth, game["id"], v1["id"], restore_source=True)
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["active_build_id"] == v1["id"] and out["restore_build"]["status"] == "building" and out["restore_build"]["restored_from_id"] == v1["id"]
    new_head = git(root, "rev-parse", "HEAD")
    assert out["restore_commit"] == new_head and new_head not in (c1, c2)
    assert git(root, "rev-parse", "HEAD^") == c2  # 이력을 지우지 않고 그 위에 복구 커밋을 쌓았다
    assert git(root, "rev-parse", "HEAD^{tree}") == git(root, "rev-parse", f"{c1}^{{tree}}")
    assert (root / "a.txt").read_text() == "v1" and not (root / "b.txt").exists() and git(root, "status", "--porcelain") == ""
    assert {c1, c2} <= set(git(root, "rev-list", "HEAD").split())  # 원래·수정 커밋이 모두 남았다
    v3 = await wait_build(client, auth, game["id"], out["restore_build"]["id"])
    assert v3["status"] == "succeeded" and v3["seq"] == 3 and v3["source_commit"] == new_head and v3["restored_from_id"] == v1["id"]
    assert v3["bundle_sha256"] == v1["bundle_sha256"] and v3["id"] not in (v1["id"], v2["id"])
    shown = await game_of(client, auth, game["id"])
    assert shown["active_build_id"] == v3["id"]  # 복구 빌드가 활성이 된다(원래 v1 도 그대로 남아 있다)
    after = await snapshot_state([v1["id"], v2["id"]])
    assert after == before  # 원래·수정 빌드는 모두 변하지 않았다
    assert len((await client.get(f"/api/games/{game['id']}/builds", headers=auth)).json()) == 3


async def test_restore_source_never_overwrites_uncommitted_work_or_a_running_build(client, auth, adapter):
    game, root = await new_game(client, auth, "rb-guard")
    v1 = await build(client, auth, game["id"])
    (root / "a.txt").write_text("v2")
    git(root, "commit", "-qam", "v2")
    v2 = await build(client, auth, game["id"])
    head = git(root, "rev-parse", "HEAD")
    (root / "a.txt").write_text("worker is editing")  # 동시 작업물
    r = await rollback(client, auth, game["id"], v1["id"], restore_source=True)
    assert r.status_code == 409 and r.json()["code"] == "dirty_worktree"
    assert (root / "a.txt").read_text() == "worker is editing" and git(root, "rev-parse", "HEAD") == head
    assert (await game_of(client, auth, game["id"]))["active_build_id"] == v2["id"]
    assert len((await client.get(f"/api/games/{game['id']}/builds", headers=auth)).json()) == 2
    # 더러운 트리여도 포인터 롤백은 소스를 건드리지 않으므로 가능하다
    assert (await rollback(client, auth, game["id"], v1["id"])).status_code == 200
    git(root, "checkout", "-q", "--", "a.txt")
    async with sessionmaker()() as db:  # 진행 중인 빌드가 있으면 복구하지 않는다
        db.add(GameBuild(game_id=uuid.UUID(game["id"]), seq=9, status="building"))
        await db.commit()
    r = await rollback(client, auth, game["id"], v2["id"], restore_source=True)
    assert r.status_code == 409 and r.json()["code"] == "build_in_progress" and git(root, "rev-parse", "HEAD") == head


async def test_restore_source_rejects_builds_without_a_reproducible_commit_and_skips_noop_commit(client, auth, adapter):
    game, root = await new_game(client, auth, "rb-dirty")
    (root / "a.txt").write_text("uncommitted")
    dirty_build = await build(client, auth, game["id"])  # 커밋되지 않은 변경으로 만든 빌드
    assert dirty_build["source_dirty"] is True
    git(root, "checkout", "-q", "--", "a.txt")
    clean = await build(client, auth, game["id"])
    r = await rollback(client, auth, game["id"], dirty_build["id"], restore_source=True)
    assert r.status_code == 409 and r.json()["code"] == "not_restorable"
    # 소스가 이미 같으면 새 커밋 없이 현재 HEAD 로 복구 빌드만 만든다
    head = git(root, "rev-parse", "HEAD")
    r = await rollback(client, auth, game["id"], clean["id"], restore_source=True)
    assert r.status_code == 200 and r.json()["restore_commit"] == head and git(root, "rev-parse", "HEAD") == head
    await wait_build(client, auth, game["id"], r.json()["restore_build"]["id"])


# ---- 잠금 결정 -------------------------------------------------------------------------------------------


def write_decisions(slug: str, decisions) -> None:
    (game_dir(slug) / "ttakkari-game.json").write_text(json.dumps(manifest(slug, locked_decisions=tuple(decisions)).to_json(), ensure_ascii=False))


async def test_rollback_to_build_violating_current_lock_is_rejected_before_touching_anything(client, auth, adapter):
    game, root = await new_game(client, auth, "rb-lock")
    v1 = await build(client, auth, game["id"])  # 결정: 2D, 터치 우선
    write_decisions("rb-lock", ["전부 3D"])
    git(root, "commit", "-qam", "new decisions")
    assert (await client.post(f"/api/games/{game['id']}/lock/approve", headers=auth)).json()["locked_revision"] == 2  # 사용자가 새로 승인
    v2 = await build(client, auth, game["id"])
    head = git(root, "rev-parse", "HEAD")
    for extra in ({}, {"restore_source": True}):
        r = await rollback(client, auth, game["id"], v1["id"], **extra)
        assert r.status_code == 409 and r.json()["code"] == "lock_violation", (extra, r.text)
    assert git(root, "rev-parse", "HEAD") == head and (await game_of(client, auth, game["id"]))["active_build_id"] == v2["id"]
    assert len((await client.get(f"/api/games/{game['id']}/builds", headers=auth)).json()) == 2
    # 승인본과 같은 빌드는 문제없이 롤백된다
    assert (await rollback(client, auth, game["id"], v2["id"], restore_source=True)).status_code == 200


async def test_committed_tampering_is_repaired_by_restoring_an_approved_build(client, auth, adapter):
    game, root = await new_game(client, auth, "rb-repair")
    v1 = await build(client, auth, game["id"])
    write_decisions("rb-repair", ["전부 3D"])  # 에이전트가 잠금 결정을 바꾸고 커밋했다
    git(root, "commit", "-qam", "agent changes decisions")
    r = await client.post(f"/api/games/{game['id']}/builds", json={}, headers=auth)
    assert r.status_code == 409 and r.json()["code"] == "lock_violation"
    r = await rollback(client, auth, game["id"], v1["id"], restore_source=True)  # 승인된 빌드의 소스로 복구
    assert r.status_code == 200, r.text
    done = await wait_build(client, auth, game["id"], r.json()["restore_build"]["id"])
    assert done["status"] == "succeeded" and done["lock_verified"] is True
    shown = await game_of(client, auth, game["id"])
    assert shown["lock_matches_repo"] is True and shown["locked_decisions"] == ["2D", "터치 우선"]
    assert (await client.post(f"/api/games/{game['id']}/builds", json={}, headers=auth)).status_code == 202


async def test_active_build_follows_latest_only_until_pinned(client, auth, adapter):
    game, _ = await new_game(client, auth, "rb-follow")
    assert (await game_of(client, auth, game["id"]))["active_build_id"] is None
    v1 = await build(client, auth, game["id"])
    assert (await game_of(client, auth, game["id"]))["active_build_id"] == v1["id"]
    adapter.files = dict(FILES_V2)
    v2 = await build(client, auth, game["id"])
    assert (await game_of(client, auth, game["id"]))["active_build_id"] == v2["id"]
    async with sessionmaker()() as db:
        assert await db.scalar(select(func.count()).select_from(GameBuild).where(GameBuild.restored_from_id.is_not(None))) == 0
