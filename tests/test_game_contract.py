"""게임 프로젝트 데이터 계약 검사(TASK-G02): 매니페스트, 디렉터리 위치, 기존 모델 연결, 잠금 결정 승인 해시."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from helpers import fresh_dir
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from ttakkari.config import get_settings
from ttakkari.db import sessionmaker
from ttakkari.games import contract
from ttakkari.games.contract import (
    GameContractError,
    approve_lock,
    decisions_sha256,
    game_dir,
    parse_manifest,
    read_manifest,
    register_game_project,
    verify_lock,
)
from ttakkari.models import (
    Artifact,
    GameBuild,
    GameFeedback,
    GameProject,
    PmsProject,
    Run,
    Workspace,
)


def manifest_dict(**over) -> dict:
    base = {"schema": 1, "id": "dot-runner", "name": "Dot Runner", "genre": "arcade", "engine": "web-phaser", "version": "0.1.0",
            "entrypoint": "dist/index.html", "locked_decisions": ["2D 도트 픽셀 그래픽", "탑다운 뷰"]}
    base.update(over)
    return base


def raw(**over) -> bytes:
    return json.dumps(manifest_dict(**over), ensure_ascii=False).encode()


def make_game_repo(slug: str = "dot-runner", **over) -> Path:
    path = game_dir(slug)
    path.mkdir(parents=True, exist_ok=True)
    (path / contract.MANIFEST_FILE).write_bytes(raw(id=slug, **over))
    (path / ".git").mkdir(exist_ok=True)
    return path


def test_manifest_round_trips_the_design_fields():
    m = parse_manifest(raw())
    assert (m.id, m.engine, m.entrypoint) == ("dot-runner", "web-phaser", "dist/index.html")
    assert m.locked_decisions == ("2D 도트 픽셀 그래픽", "탑다운 뷰")
    assert parse_manifest(json.dumps(m.to_json()).encode()) == m


@pytest.mark.parametrize("over", [
    {"schema": 2}, {"id": "Bad_Id"}, {"id": "ab"}, {"engine": "../x"}, {"name": ""}, {"version": ""},
    {"entrypoint": "/etc/passwd"}, {"entrypoint": "../x/index.html"}, {"entrypoint": "a\\b"},
    {"locked_decisions": "2D"}, {"locked_decisions": [""]}, {"locked_decisions": [1]}, {"locked_decisions": ["x" * 501]},
    {"locked_decisions": ["a"] * 51},
])
def test_manifest_rejects_invalid_values(over):
    with pytest.raises(GameContractError) as e:
        parse_manifest(raw(**over))
    assert e.value.code == "bad_manifest"


def test_manifest_rejects_non_json_and_oversize():
    for blob in (b"not json", b"[]", b"\xff\xfe", b"x" * (contract.MANIFEST_MAX_BYTES + 1)):
        with pytest.raises(GameContractError):
            parse_manifest(blob)


def test_read_manifest_requires_regular_file_in_root(tmp_path: Path):
    with pytest.raises(GameContractError) as e:
        read_manifest(tmp_path)
    assert e.value.code == "no_manifest"
    outside = tmp_path / "outside.json"
    outside.write_bytes(raw())
    root = tmp_path / "proj"
    root.mkdir()
    (root / contract.MANIFEST_FILE).symlink_to(outside)
    with pytest.raises(GameContractError) as e:
        read_manifest(root)
    assert e.value.code == "bad_manifest"


def test_game_dir_stays_inside_games_root_and_allowed_roots(monkeypatch, tmp_path: Path):
    assert game_dir("dot-runner").parent == get_settings().games_root.resolve()
    for bad in ("../escape", "a/b", "UPPER", "x", "-bad", ""):
        with pytest.raises(GameContractError) as e:
            game_dir(bad)
        assert e.value.code == "bad_slug"
    monkeypatch.setattr(get_settings(), "games_root", tmp_path / "elsewhere")  # allowed_roots 밖
    with pytest.raises(GameContractError) as e:
        game_dir("dot-runner")
    assert e.value.code == "outside_root"


def test_lock_hash_ignores_other_fields_but_not_decisions():
    base = parse_manifest(raw())
    bumped = parse_manifest(raw(version="0.2.0", name="Renamed"))
    changed = parse_manifest(raw(locked_decisions=["2D 도트 픽셀 그래픽"]))
    game = GameProject(slug="x", name="x", engine_id="web-phaser", locked_decisions_sha256=decisions_sha256(base.locked_decisions))
    assert verify_lock(game, base) and verify_lock(game, bumped) and not verify_lock(game, changed)
    assert not verify_lock(GameProject(slug="y", name="y", engine_id="e"), base)  # 승인 전


async def test_register_links_workspace_pms_and_lock_approval():
    make_game_repo()
    manifest, _ = read_manifest(game_dir("dot-runner"))
    async with sessionmaker()() as db:
        game = await register_game_project(db, manifest)
        await approve_lock(db, game, manifest)
        await db.commit()
        ws = await db.get(Workspace, game.workspace_id)
        pms = await db.get(PmsProject, game.pms_project_id)
        assert ws.root_path == str(game_dir("dot-runner")) and ws.is_git
        assert pms.name == "Dot Runner" and pms.status == "active"
        assert (game.locked_revision, verify_lock(game, manifest)) == (1, True)
        await approve_lock(db, game, manifest)
        assert game.locked_revision == 1  # 같은 결정은 revision 을 올리지 않는다
        with pytest.raises(GameContractError) as e:
            await register_game_project(db, manifest)
        assert e.value.code == "exists"


async def test_register_rejects_missing_dir_and_symlinked_dir(tmp_path: Path):
    manifest = parse_manifest(raw(id="ghost-game"))
    async with sessionmaker()() as db:
        with pytest.raises(GameContractError) as e:
            await register_game_project(db, manifest)
        assert e.value.code == "not_found"
    target = fresh_dir(get_settings().allowed_roots[0] / "real-elsewhere")
    link = game_dir("linked-game")
    if link.is_symlink() or link.is_file():
        link.unlink()  # 이전 실행이 남긴 심링크를 치운다
    link.symlink_to(target)
    async with sessionmaker()() as db:
        with pytest.raises(GameContractError) as e:
            await register_game_project(db, parse_manifest(raw(id="linked-game")))
        assert e.value.code == "bad_path"


async def test_builds_and_feedback_reference_existing_models_and_cascade():
    make_game_repo("cascade-game")
    manifest, _ = read_manifest(game_dir("cascade-game"))
    async with sessionmaker()() as db:
        game = await register_game_project(db, manifest, create_pms=False)
        game_id, workspace_id = game.id, game.workspace_id
        build = GameBuild(game_id=game.id, seq=1, status="succeeded")
        db.add(build)
        await db.flush()
        db.add(GameFeedback(game_id=game.id, build_id=build.id, kind="annotation", payload={"x": 0.42, "y": 0.65}))
        await db.commit()
        db.add(GameBuild(game_id=game_id, seq=1))  # 게임별 seq 는 유일
        with pytest.raises(IntegrityError):
            await db.flush()
        await db.rollback()
        # 워크스페이스는 게임이 있으면 지울 수 없다(RESTRICT).
        ws = await db.get(Workspace, workspace_id)
        await db.delete(ws)
        with pytest.raises(IntegrityError):
            await db.flush()
        await db.rollback()
        # 게임을 지우면 빌드·피드백 기록이 함께 사라진다.
        await db.delete(await db.get(GameProject, game_id))
        await db.commit()
        assert (await db.scalars(select(GameBuild))).all() == []
        assert (await db.scalars(select(GameFeedback))).all() == []


async def test_build_run_and_artifact_links_are_nullable_references():
    # Run/Artifact 가 지워져도 빌드 기록은 남는다(SET NULL). 외래키 방향 확인용.
    for model, col in ((GameBuild, "run_id"), (GameBuild, "artifact_id"), (GameFeedback, "run_id"), (GameFeedback, "screenshot_artifact_id")):
        fk = next(iter(model.__table__.c[col].foreign_keys))
        assert fk.ondelete == "SET NULL" and fk.column.table.name in {Run.__tablename__, Artifact.__tablename__}
