"""불변 빌드 스냅샷 (TASK-G04, docs/design/game-studio.md §5.1, ADR 0046·0047).

성공한 빌드의 번들은 에이전트가 쓸 수 없는 data_dir/game-builds/<game_id>/<build_id>/bundle 에 읽기 전용으로 복사하고,
소스 commit·매니페스트 해시·번들 해시를 GameBuild 행에 묶는다. 재빌드는 항상 새 빌드 ID와 새 디렉터리를 만들어
이전 스냅샷을 건드리지 않는다. 롤백(G12)이 이전 빌드를 그대로 가리킬 수 있게 하는 것이 목적이다.

이 모듈의 함수는 장시간 빌드 동안 DB 트랜잭션을 열어 두지 않으려고 직접 commit 한다(building 행 생성, 완료 기록).
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import re
import shutil
import uuid
import zipfile
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from ttakkari import queued_work
from ttakkari.artifacts.service import register_file
from ttakkari.broker.policy import ResolvedPath
from ttakkari.config import get_settings
from ttakkari.db import sessionmaker
from ttakkari.games.adapter import (
    GameEngineAdapter,
    get_adapter,
    resolve_project_path,
    run_git,
)
from ttakkari.games.contract import (
    MANIFEST_FILE,
    GameContractError,
    ProjectManifest,
    parse_manifest,
    read_manifest,
    verify_lock,
)
from ttakkari.games.isolation import IsolatedBuildRunner
from ttakkari.models import Artifact, GameBuild, GameProject, Run, Workspace, utcnow

log = logging.getLogger(__name__)

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


def require_lock_intact(game: GameProject, manifest: ProjectManifest) -> None:
    """사용자가 승인한 잠금 결정과 저장소 매니페스트가 다르면 빌드·적용을 거부한다. 아직 승인한 적이 없으면 막을 대상이 없다."""
    if game.locked_decisions_sha256 is not None and not verify_lock(game, manifest):
        raise GameContractError(
            "저장소의 잠금 결정이 사용자가 승인한 내용과 다릅니다. ttakkari-game.json 의 locked_decisions 를 승인본으로 되돌리거나 사용자가 새로 승인해야 합니다.",
            "lock_violation")


def _is_stale(build: GameBuild, stale_before) -> bool:
    """맡은 시각(없으면 접수 시각)이 기준보다 오래된 building 행. 실행부 대기(queued)는 아직 시작 전이라 오래돼도 stale 이 아니다."""
    if build.queue_state == "queued":
        return False
    started = build.claimed_at or build.created_at
    return started.replace(tzinfo=None) < stale_before.replace(tzinfo=None)


async def _next_build(
    db: AsyncSession, game: GameProject, run: Run | None, restored_from: uuid.UUID | None = None, *, queued: bool = False,
) -> GameBuild:
    """게임 행을 잠가 순번을 매기고 building 행을 만든다. 진행 중인 빌드가 있으면 거부하고, 중단된 오래된 빌드는 실패로 닫는다.

    queued=True 이면 원격 API 접수다. 행은 그대로 building 이고 queue_state=queued 로 실행부에 넘긴다.
    """
    s = get_settings()
    await db.execute(select(GameProject.id).where(GameProject.id == game.id).with_for_update())
    stale_before = utcnow() - timedelta(seconds=s.game_build_install_timeout_seconds + s.game_build_timeout_seconds + STALE_SLACK_SECONDS)
    for b in (await db.scalars(select(GameBuild).where(GameBuild.game_id == game.id, GameBuild.status == "building"))).all():
        if _is_stale(b, stale_before):
            b.status, b.error, b.finished_at = "failed", "빌드가 중단되었습니다(서버 재시작 또는 시간 초과).", utcnow()
        else:
            await db.rollback()
            raise GameContractError("이 게임의 다른 빌드가 진행 중입니다.", "build_in_progress")
    seq = (await db.scalar(select(func.coalesce(func.max(GameBuild.seq), 0)).where(GameBuild.game_id == game.id))) + 1
    build = GameBuild(game_id=game.id, seq=seq, status="building", run_id=run.id if run else None, restored_from_id=restored_from,
                      queue_state="queued" if queued else None)
    db.add(build)
    await db.commit()
    return build


@dataclass
class BuildPlan:
    """검증을 마치고 building 행까지 만든 빌드. execute_build 가 실제 빌드·스냅샷을 수행한다."""

    game_id: uuid.UUID
    build_id: uuid.UUID
    root: Path
    workspace_id: uuid.UUID
    engine_id: str
    manifest_sha: str


async def prepare_build(
    db: AsyncSession, game: GameProject, *, run_id: uuid.UUID | None = None, restored_from: uuid.UUID | None = None, queued: bool = False,
) -> tuple[GameBuild, BuildPlan]:
    """경로·매니페스트·Run 을 검증하고 순번을 매긴 building 행을 만든다. 검증에 실패하면 빌드 행을 만들지 않는다.

    queued=True(원격 API)이면 행을 실행부 대기로 표시하고, 빌드는 consume_queued_build 가 이어받는다."""
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
    require_lock_intact(game, manifest)
    build = await _next_build(db, game, run, restored_from, queued=queued)
    return build, BuildPlan(game.id, build.id, root, ws.id, game.engine_id, manifest_sha)


async def execute_build(
    db: AsyncSession, plan: BuildPlan, *, adapter: GameEngineAdapter | None = None, runner: IsolatedBuildRunner | None = None,
) -> GameBuild:
    """prepare_build 가 만든 building 행을 격리 빌드하고 불변 스냅샷으로 완료한다. 실패해도 failed 행으로 남는다.

    db 세션은 prepare_build 의 것과 달라도 된다(백그라운드 실행).
    """
    build = await db.get(GameBuild, plan.build_id)
    game = await db.get(GameProject, plan.game_id)
    ws = await db.get(Workspace, plan.workspace_id)
    if build is None or game is None or ws is None or build.status != "building":
        raise GameContractError("실행할 빌드를 찾을 수 없습니다.", "no_build")
    run = await db.get(Run, build.run_id) if build.run_id else None
    adapter = adapter or get_adapter(plan.engine_id, **({"runner": runner} if runner else {}))
    work = getattr(adapter, "work_dir", get_settings().game_builds_dir / "_adapter")
    root = plan.root
    try:
        manifest, manifest_sha = read_manifest(root)
        require_lock_intact(game, manifest)  # prepare_build 이후 실행 전에 바뀐 경우까지 막는다
        head, dirty, fp_before = await git_state(root)
        build.source_commit, build.source_dirty = head, dirty
        build.manifest_sha256, build.lock_verified = manifest_sha, verify_lock(game, manifest)
        res = await adapter.build(root, str(plan.build_id))
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
        await asyncio.to_thread(shutil.rmtree, work / str(plan.build_id), True)
        await db.commit()
    return build


async def _plan_for(db: AsyncSession, build: GameBuild) -> BuildPlan:
    """DB 행만으로 BuildPlan 을 다시 만든다. 경로·엔진은 prepare_build 가 검증한 게임·workspace 에서 그대로 유도된다."""
    game = await db.get(GameProject, build.game_id)
    ws = await db.get(Workspace, game.workspace_id) if game else None
    if game is None or ws is None:
        raise GameContractError("게임 또는 workspace 를 찾을 수 없습니다.", "no_workspace")
    root = resolve_project_path(Path(ws.root_path))
    if root.name != game.slug:
        raise GameContractError("workspace 경로가 게임 slug 와 다릅니다.", "bad_path")
    return BuildPlan(game.id, build.id, root, ws.id, game.engine_id, build.manifest_sha256 or "")


async def claim_queued_build(db: AsyncSession) -> BuildPlan | None:
    """원격 API 가 넘긴(queue_state=queued) 가장 오래된 빌드 하나를 맡는다. 조건부 UPDATE 라 둘이 동시에 맡을 수 없다.

    queue_state 가 NULL 인 building 행(API/Run 이 직접 돌리는 빌드, 이 표식 이전 행)은 후보가 아니다."""
    candidates = list(await db.scalars(
        select(GameBuild.id).where(GameBuild.status == "building", GameBuild.queue_state == "queued")
        .order_by(GameBuild.created_at, GameBuild.id)))
    for build_id in candidates:
        claimed = await db.execute(
            update(GameBuild).where(GameBuild.id == build_id, GameBuild.status == "building", GameBuild.queue_state == "queued")
            .values(queue_state="claimed", claimed_at=utcnow()))
        await db.commit()
        if claimed.rowcount != 1:
            continue
        build = await db.get(GameBuild, build_id, populate_existing=True)
        try:
            return await _plan_for(db, build)
        except GameContractError as e:
            # 맡았지만 실행할 수 없는 행은 실패로 닫는다. 닫지 않으면 이 게임의 다음 접수를 영원히 막는다.
            build.status, build.error, build.finished_at = "failed", str(e)[:500], utcnow()
            await db.commit()
    return None


async def consume_queued_build(*, adapter: GameEngineAdapter | None = None, runner: IsolatedBuildRunner | None = None) -> GameBuild | None:
    """대기 중인 빌드 하나를 맡아 끝까지 실행한다. 맡은 행이 없으면 None. 드레인 중에는 새로 맡지 않는다.

    실행 중인 빌드는 소비 루프가 취소돼도 끝까지 간다(queued_work.run_tracked)."""
    if queued_work.draining():
        return None
    async with sessionmaker()() as db:
        plan = await claim_queued_build(db)
    if plan is None:
        return None

    async def run() -> GameBuild | None:
        async with sessionmaker()() as db:
            try:
                return await execute_build(db, plan, adapter=adapter, runner=runner)
            except Exception:
                log.exception("game build %s failed unexpectedly", plan.build_id)  # 행은 execute_build 가 failed 로 닫았다
                return await db.get(GameBuild, plan.build_id, populate_existing=True)

    return await queued_work.run_tracked(run())


async def build_game(
    db: AsyncSession, game: GameProject, *, adapter: GameEngineAdapter | None = None, runner: IsolatedBuildRunner | None = None,
    run_id: uuid.UUID | None = None,
) -> GameBuild:
    """게임 저장소를 격리 빌드하고 불변 스냅샷으로 기록한다(prepare_build + execute_build)."""
    _, plan = await prepare_build(db, game, run_id=run_id)
    return await execute_build(db, plan, adapter=adapter, runner=runner)


async def _snapshot(db: AsyncSession, game: GameProject, build: GameBuild, out: Path, run: Run | None, ws: Workspace) -> None:
    dest = _snapshot_dir(game.id, build.id)
    bundle = dest / BUNDLE_DIRNAME
    try:
        await asyncio.to_thread(_copy_bundle, out, dest)
        sha, total, files = await asyncio.to_thread(bundle_digest, bundle)
        zip_path = dest / f"{game.slug}-v{build.seq}.zip"
        await asyncio.to_thread(_pack_zip, bundle, zip_path)
        art = await register_file(
            db, ResolvedPath(root=dest, path=zip_path), source=ARTIFACT_SOURCE, origin="agent", workspace_id=ws.id,
            session_id=run.session_id if run else None, run_id=run.id if run else None, rel_path=zip_path.name, force_copy=True)
        os.chmod(zip_path, 0o444)
    except BaseException:
        await asyncio.to_thread(delete_snapshot_files, dest)
        raise
    build.bundle_rel_path = bundle.relative_to(get_settings().game_builds_dir).as_posix()
    build.bundle_sha256, build.bundle_bytes, build.bundle_files = sha, total, files
    build.artifact_id = art.id
    build.status = "succeeded"
    await _follow_or_pin(db, game, build)


async def _follow_or_pin(db: AsyncSession, game: GameProject, build: GameBuild) -> None:
    """새 성공 빌드를 활성 빌드로 만들지 정한다. 활성이 없거나 직전 최신 빌드를 따라가던 중이면 새 빌드로 옮기고,
    사용자가 롤백으로 오래된 빌드에 고정해 두었으면 그 고정을 유지한다. 롤백으로 복구한 빌드는 항상 활성이 된다."""
    previous = await db.scalar(select(GameBuild.id).where(GameBuild.game_id == game.id, GameBuild.status == "succeeded", GameBuild.seq < build.seq)
                               .order_by(GameBuild.seq.desc()).limit(1))
    if build.restored_from_id is not None or game.active_build_id is None or game.active_build_id == previous:
        game.active_build_id = build.id


async def effective_active_build(db: AsyncSession, game: GameProject) -> GameBuild | None:
    """관제탑이 여는 빌드: 지정된 활성 빌드, 없으면 가장 최근 성공 빌드."""
    if game.active_build_id is not None:
        active = await db.get(GameBuild, game.active_build_id)
        if active is not None and active.status == "succeeded":
            return active
    return await db.scalar(select(GameBuild).where(GameBuild.game_id == game.id, GameBuild.status == "succeeded")
                           .order_by(GameBuild.seq.desc()).limit(1))


COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")


async def manifest_at_commit(root: Path, commit: str) -> ProjectManifest:
    """해당 commit 시점의 ttakkari-game.json. 작업 트리가 아니라 git 객체를 읽으므로 에이전트가 파일을 고쳐도 영향이 없다."""
    if not COMMIT_RE.fullmatch(commit):
        raise GameContractError("source commit 형식이 올바르지 않습니다.", "bad_commit")
    return parse_manifest((await run_git(root, "show", f"{commit}:{MANIFEST_FILE}")).encode())


async def check_activatable(db: AsyncSession, game: GameProject, build: GameBuild) -> None:
    """빌드를 활성으로 삼아도 되는지(성공·번들 해시 일치·승인된 잠금 결정과 일치). 아니면 GameContractError.

    잠금 결정은 빌드의 source commit 시점 매니페스트로 다시 판정한다. 사용자가 그 뒤 결정을 새로 승인했다면 옛 빌드는 거부된다.
    commit 이 없거나 작업 트리가 더러운 빌드는 매니페스트를 재현할 수 없어 빌드 당시의 lock_verified 만 믿는다."""
    if build.game_id != game.id or build.status != "succeeded":
        raise GameContractError("성공한 이 게임의 빌드만 활성으로 삼을 수 있습니다.", "not_playable")
    if not await asyncio.to_thread(verify_snapshot, build):
        raise GameContractError("빌드 스냅샷이 기록된 해시와 달라 활성으로 삼을 수 없습니다.", "snapshot_tampered")
    if game.locked_decisions_sha256 is None:
        return
    if build.source_commit and not build.source_dirty:
        ws = await db.get(Workspace, game.workspace_id)
        manifest = await manifest_at_commit(resolve_project_path(Path(ws.root_path)), build.source_commit)
        require_lock_intact(game, manifest)
    elif not build.lock_verified:
        raise GameContractError("이 빌드는 승인된 잠금 결정과 일치하는지 확인되지 않아 활성으로 삼을 수 없습니다.", "lock_violation")


async def assert_no_build_in_progress(db: AsyncSession, game: GameProject) -> None:
    s = get_settings()
    stale_before = utcnow() - timedelta(seconds=s.game_build_install_timeout_seconds + s.game_build_timeout_seconds + STALE_SLACK_SECONDS)
    for b in (await db.scalars(select(GameBuild).where(GameBuild.game_id == game.id, GameBuild.status == "building"))).all():
        if not _is_stale(b, stale_before):
            raise GameContractError("이 게임의 다른 빌드가 진행 중입니다.", "build_in_progress")


async def restore_source(root: Path, build: GameBuild) -> str:
    """작업 트리를 건드리지 않는 정책의 소스 복구: 대상 commit 의 트리를 부모 HEAD 로 하는 새 복구 커밋을 만들고 fast-forward 로만 반영한다.

    작업 트리가 깨끗해야 하며(동시 작업물을 덮지 않는다) 기존 커밋·브랜치 이력은 지우지 않는다. 새 커밋 SHA 를 돌려준다."""
    if not build.source_commit or build.source_dirty:
        raise GameContractError("커밋되지 않은 변경으로 만든 빌드는 소스를 복구할 수 없습니다.", "not_restorable")
    if not COMMIT_RE.fullmatch(build.source_commit):
        raise GameContractError("source commit 형식이 올바르지 않습니다.", "bad_commit")
    head, dirty, _ = await git_state(root)
    if head is None:
        raise GameContractError("저장소에 커밋이 없어 복구할 수 없습니다.", "not_restorable")
    if dirty:
        raise GameContractError("저장소에 커밋되지 않은 변경이 있습니다. 변경을 커밋하거나 정리한 뒤 다시 시도하세요.", "dirty_worktree")
    await run_git(root, "cat-file", "-e", f"{build.source_commit}^{{commit}}")
    tree = await run_git(root, "rev-parse", f"{build.source_commit}^{{tree}}")
    if tree == await run_git(root, "rev-parse", "HEAD^{tree}"):
        return head  # 소스가 이미 같다. 새 커밋 없이 이 commit 을 다시 빌드한다.
    new = await run_git(root, "commit-tree", tree, "-p", head, "-m", f"Restore source of build v{build.seq} ({build.source_commit[:10]}) via Ttakkari rollback")
    await run_git(root, "merge", "--ff-only", "--quiet", new)
    return new


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

