"""불변 빌드 스냅샷 (TASK-G04, docs/design/game-studio.md §5.1, ADR 0046·0047).

성공한 빌드의 번들은 에이전트가 쓸 수 없는 data_dir/game-builds/<game_id>/<build_id>/bundle 에 읽기 전용으로 복사하고,
소스 commit·매니페스트 해시·번들 해시를 GameBuild 행에 묶는다. 재빌드는 항상 새 빌드 ID와 새 디렉터리를 만들어
이전 스냅샷을 건드리지 않는다. 롤백(G12)이 이전 빌드를 그대로 가리킬 수 있게 하는 것이 목적이다.

이 모듈의 함수는 장시간 빌드 동안 DB 트랜잭션을 열어 두지 않으려고 직접 commit 한다(building 행 생성, 완료 기록).
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import shutil
import uuid
import zipfile
from datetime import timedelta
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ttakkari.artifacts.service import register_file
from ttakkari.broker.policy import ResolvedPath
from ttakkari.config import get_settings
from ttakkari.games.adapter import (
    GameEngineAdapter,
    get_adapter,
    resolve_project_path,
    run_git,
)
from ttakkari.games.contract import GameContractError, read_manifest, verify_lock
from ttakkari.games.isolation import IsolatedBuildRunner
from ttakkari.models import Artifact, GameBuild, GameProject, Run, Workspace, utcnow

BUNDLE_DIRNAME = "bundle"
LOG_TAIL_CHARS = 20_000
ARTIFACT_SOURCE = "game_build"
STALE_SLACK_SECONDS = 120


def _snapshot_dir(game_id: uuid.UUID, build_id: uuid.UUID) -> Path:
    return get_settings().game_builds_dir / str(game_id) / str(build_id)


def bundle_path(build: GameBuild) -> Path | None:
    """빌드의 번들 디렉터리. game_builds_dir 밖을 가리키는 값(DB 변조)은 돌려주지 않는다."""
    if not build.bundle_rel_path:
        return None
    root = get_settings().game_builds_dir.resolve()
    p = (root / build.bundle_rel_path).resolve()
    return p if root in p.parents and p.is_dir() else None


def bundle_digest(bundle: Path) -> tuple[str, int, int]:
    """(번들 sha256, 총 바이트, 파일 수). 상대 경로와 파일 해시를 정렬해 합치므로 복사·압축 방식과 무관하다."""
    h = hashlib.sha256()
    total = 0
    files = 0
    for p in sorted(bundle.rglob("*")):
        if p.is_symlink() or not p.is_file():
            continue
        fh = hashlib.sha256()
        with p.open("rb") as f:
            for chunk in iter(lambda: f.read(1024 * 1024), b""):
                fh.update(chunk)
        h.update(f"{p.relative_to(bundle).as_posix()}\0{fh.hexdigest()}\n".encode())
        total += p.stat().st_size
        files += 1
    return h.hexdigest(), total, files


def _freeze(root: Path) -> None:
    for p in sorted(root.rglob("*"), reverse=True):
        os.chmod(p, 0o555 if p.is_dir() else 0o444)
    os.chmod(root, 0o555)


def _thaw(root: Path) -> None:
    for p in [root, *root.rglob("*")]:
        if not p.is_symlink():
            os.chmod(p, 0o755 if p.is_dir() else 0o644)


def _pack_zip(bundle: Path, dest: Path) -> None:
    """번들을 결정적 zip 으로 묶는다(고정 시각, 정렬). Artifact 다운로드용이며 정본은 번들 디렉터리다."""
    with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as z:
        for p in sorted(bundle.rglob("*")):
            if p.is_file() and not p.is_symlink():
                info = zipfile.ZipInfo(p.relative_to(bundle).as_posix(), date_time=(1980, 1, 1, 0, 0, 0))
                info.external_attr = 0o644 << 16
                z.writestr(info, p.read_bytes(), zipfile.ZIP_DEFLATED)


def _copy_bundle(src: Path, dest: Path) -> None:
    """src 를 dest/bundle 로 복사해 읽기 전용으로 잠근다(dest 는 zip 을 더 쓸 수 있게 잠그지 않는다). 이미 있는 dest 는 덮어쓰지 않는다."""
    if dest.exists():
        raise GameContractError("이미 같은 빌드의 스냅샷이 있습니다.", "snapshot_exists")
    get_settings().game_builds_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    dest.parent.mkdir(exist_ok=True, mode=0o700)
    dest.mkdir(mode=0o700)
    shutil.copytree(src, bundle_dir := dest / BUNDLE_DIRNAME, symlinks=False, copy_function=shutil.copyfile)
    _freeze(bundle_dir)


async def git_state(root: Path) -> tuple[str | None, bool, str]:
    """(HEAD commit 또는 None, 작업 트리 변경 여부, 상태 지문). 지문은 빌드 중 소스가 바뀌었는지 비교하는 데 쓴다."""
    try:
        head = await run_git(root, "rev-parse", "--verify", "HEAD")
    except GameContractError:
        head = None
    status = await run_git(root, "status", "--porcelain=v1")
    diff = await run_git(root, "diff", "HEAD") if head else ""
    fp = hashlib.sha256(f"{head}\0{status}\0{diff}".encode()).hexdigest()
    return head, bool(status), fp


async def _next_build(db: AsyncSession, game: GameProject, run: Run | None) -> GameBuild:
    """게임 행을 잠가 순번을 매기고 building 행을 만든다. 진행 중인 빌드가 있으면 거부하고, 중단된 오래된 빌드는 실패로 닫는다."""
    s = get_settings()
    await db.execute(select(GameProject.id).where(GameProject.id == game.id).with_for_update())
    stale_before = utcnow() - timedelta(seconds=s.game_build_install_timeout_seconds + s.game_build_timeout_seconds + STALE_SLACK_SECONDS)
    for b in (await db.scalars(select(GameBuild).where(GameBuild.game_id == game.id, GameBuild.status == "building"))).all():
        if b.created_at.replace(tzinfo=None) < stale_before.replace(tzinfo=None):
            b.status, b.error, b.finished_at = "failed", "빌드가 중단되었습니다(서버 재시작 또는 시간 초과).", utcnow()
        else:
            await db.rollback()
            raise GameContractError("이 게임의 다른 빌드가 진행 중입니다.", "build_in_progress")
    seq = (await db.scalar(select(func.coalesce(func.max(GameBuild.seq), 0)).where(GameBuild.game_id == game.id))) + 1
    build = GameBuild(game_id=game.id, seq=seq, status="building", run_id=run.id if run else None)
    db.add(build)
    await db.commit()
    return build


async def build_game(
    db: AsyncSession, game: GameProject, *, adapter: GameEngineAdapter | None = None, runner: IsolatedBuildRunner | None = None,
    run_id: uuid.UUID | None = None,
) -> GameBuild:
    """게임 저장소를 격리 빌드하고 불변 스냅샷으로 기록한다. 실패해도 failed 빌드 행이 남는다."""
    ws = await db.get(Workspace, game.workspace_id)
    if ws is None:
        raise GameContractError("게임의 workspace 를 찾을 수 없습니다.", "no_workspace")
    root = resolve_project_path(Path(ws.root_path))
    if root.name != game.slug:
        raise GameContractError("workspace 경로가 게임 slug 와 다릅니다.", "bad_path")
    run = None
    if run_id is not None:
        run = await db.get(Run, run_id)
        if run is None:
            raise GameContractError("Run 을 찾을 수 없습니다.", "no_run")
    manifest, manifest_sha = read_manifest(root)
    if manifest.id != game.slug or manifest.engine != game.engine_id:
        raise GameContractError("매니페스트가 등록된 게임(slug/engine)과 다릅니다.", "bad_manifest")
    adapter = adapter or get_adapter(game.engine_id, **({"runner": runner} if runner else {}))

    build = await _next_build(db, game, run)
    build_id = build.id
    work = adapter.work_dir if hasattr(adapter, "work_dir") else get_settings().game_builds_dir / "_adapter"
    try:
        head, dirty, fp_before = await git_state(root)
        build.source_commit, build.source_dirty = head, dirty
        build.manifest_sha256, build.lock_verified = manifest_sha, verify_lock(game, manifest)
        res = await adapter.build(root, str(build_id))
        build.log_tail = res.log[-LOG_TAIL_CHARS:]
        _, _, fp_after = await git_state(root)
        if fp_after != fp_before:
            build.status, build.error = "failed", "빌드 중 소스가 변경되어 source commit 을 신뢰할 수 없습니다."
        elif not res.success or res.output_dir is None:
            build.status, build.error = "failed", res.error or "빌드 실패"
        else:
            await _snapshot(db, game, build, res.output_dir, run, ws)
    except BaseException as e:
        build.status, build.error = "failed", build.error or f"{type(e).__name__}: {e}"[:500]
        raise
    finally:
        build.finished_at = utcnow()
        await asyncio.to_thread(shutil.rmtree, work / str(build_id), True)
        await db.commit()
    return build


async def _snapshot(db: AsyncSession, game: GameProject, build: GameBuild, out: Path, run: Run | None, ws: Workspace) -> None:
    dest = _snapshot_dir(game.id, build.id)
    bundle = dest / BUNDLE_DIRNAME
    try:
        await asyncio.to_thread(_copy_bundle, out, dest)
        sha, total, files = await asyncio.to_thread(bundle_digest, bundle)
        zip_path = dest / f"{game.slug}-v{build.seq}.zip"
        await asyncio.to_thread(_pack_zip, bundle, zip_path)
        art = await register_file(
            db, ResolvedPath(root=dest, path=zip_path), source=ARTIFACT_SOURCE, workspace_id=ws.id,
            session_id=run.session_id if run else None, run_id=run.id if run else None, rel_path=zip_path.name, force_copy=True)
        os.chmod(zip_path, 0o444)
    except BaseException:
        await asyncio.to_thread(delete_snapshot_files, dest)
        raise
    build.bundle_rel_path = bundle.relative_to(get_settings().game_builds_dir).as_posix()
    build.bundle_sha256, build.bundle_bytes, build.bundle_files = sha, total, files
    build.artifact_id = art.id
    build.status = "succeeded"


def delete_snapshot_files(dest: Path) -> None:
    """스냅샷 디렉터리를 지운다. 읽기 전용 잠금을 풀어야 하므로 보존 정책(삭제)이 정한 경로에서만 호출한다."""
    if dest.exists():
        _thaw(dest)
        shutil.rmtree(dest, ignore_errors=True)


def verify_snapshot(build: GameBuild) -> bool:
    """저장된 번들이 기록된 해시와 아직 같은지 다시 계산한다."""
    p = bundle_path(build)
    if p is None or build.status != "succeeded" or not build.bundle_sha256:
        return False
    return bundle_digest(p)[0] == build.bundle_sha256


async def snapshot_artifact(db: AsyncSession, build: GameBuild) -> Artifact | None:
    return await db.get(Artifact, build.artifact_id) if build.artifact_id else None

