"""불변 빌드 스냅샷: source commit, 번들 해시·위치, Run/Artifact 연결, 재빌드가 이전 산출물을 바꾸지 않음 (TASK-G04)."""

import asyncio
import hashlib
import os
import subprocess
import uuid
import zipfile
from pathlib import Path

import pytest
from helpers import fresh_dir
from sqlalchemy import select

from ttakkari.config import get_settings
from ttakkari.db import sessionmaker
from ttakkari.games import builds
from ttakkari.games.adapter import (
    BuildResult,
    GameEngineAdapter,
    PhaserAdapter,
    TestResult,
)
from ttakkari.games.builds import (
    build_game,
    bundle_digest,
    bundle_path,
    delete_snapshot_files,
    verify_snapshot,
)
from ttakkari.games.contract import (
    GameContractError,
    ProjectManifest,
    approve_lock,
    game_dir,
    read_manifest,
    register_game_project,
)
from ttakkari.games.isolation import IsolatedBuildRunner, IsolationLimits, probe_runtime
from ttakkari.models import (
    Artifact,
    ChatSession,
    GameBuild,
    GameProject,
    Run,
    Workspace,
)

RUNTIME = probe_runtime()
if not RUNTIME.available and os.environ.get("TTAKKARI_REQUIRE_GAME_RUNTIME") == "1":
    pytest.fail(f"TTAKKARI_REQUIRE_GAME_RUNTIME=1 인데 컨테이너 런타임을 쓸 수 없다: {RUNTIME.reason}", pytrace=False)
requires_runtime = pytest.mark.skipif(not RUNTIME.available, reason=f"컨테이너 런타임 필요(격리 빌드 불가): {RUNTIME.reason}")


class FakeAdapter(GameEngineAdapter):
    """격리 러너 없이 빌드 결과만 흉내 내는 어댑터. 스냅샷 기록 규칙(실패·동시성·변조)을 빠르게 확인하는 용도다."""

    def __init__(self, work_dir: Path, *, fail: str | None = None, hook=None, files: dict[str, str] | None = None):
        self.work_dir = work_dir
        self.fail, self.hook = fail, hook
        self.files = files or {"index.html": "<!doctype html><title>fake</title>", "assets/app.js": "console.log(1)"}

    engine_id = "web-phaser"

    async def scaffold_project(self, project_path, manifest): ...
    async def run_automated_test(self, project_path, build_id, scenario):
        return TestResult(False, supported=False)

    def get_runtime_serve_path(self, project_path, build_id):
        return self.work_dir / build_id / "out"

    def extract_scene_metadata(self, project_path):
        return {}

    async def build(self, project_path: Path, build_id: str) -> BuildResult:
        if self.hook:
            await self.hook(project_path)
        if self.fail:
            return BuildResult(False, build_id, log="tsc: error TS2304", error=self.fail)
        out = self.work_dir / build_id / "out"
        for rel, text in self.files.items():
            (out / rel).parent.mkdir(parents=True, exist_ok=True)
            (out / rel).write_text(text)
        return BuildResult(True, build_id, out, sorted(self.files), log="ok")


def git(root: Path, *args: str) -> str:
    env = {**os.environ, "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null"}
    return subprocess.run(["git", "-C", str(root), "-c", "user.name=t", "-c", "user.email=t@t", *args], capture_output=True, text=True,
                          check=True, env=env).stdout.strip()


def manifest(slug: str, **over) -> ProjectManifest:
    base = {"id": slug, "name": "Snap Demo", "genre": "arcade", "engine": "web-phaser", "version": "0.1.0", "entrypoint": "index.html",
            "locked_decisions": ("2D", "터치 우선")}
    return ProjectManifest(**{**base, **over})


@pytest.fixture(autouse=True)
def _cleanup_snapshots():
    yield
    root = get_settings().game_builds_dir
    if root.exists():
        for child in root.iterdir():
            delete_snapshot_files(child)


async def make_game(slug: str, *, approve: bool = True, real: bool = False) -> tuple[uuid.UUID, Path]:
    m = manifest(slug)
    root = fresh_dir(game_dir(slug))  # 빈 디렉터리에는 scaffold 할 수 있다
    if real:
        await PhaserAdapter().scaffold_project(root, m)
    else:
        (root / "ttakkari-game.json").write_text(__import__("json").dumps(m.to_json(), ensure_ascii=False))
        (root / "a.txt").write_text("v1")
        git(root, "init", "-q", "-b", "main")
        git(root, "add", "-A")
        git(root, "commit", "-q", "-m", "init")
    async with sessionmaker()() as db:
        game = await register_game_project(db, read_manifest(root)[0])
        if approve:
            await approve_lock(db, game, m)
        await db.commit()
        return game.id, root


async def make_run(workspace_id: uuid.UUID) -> uuid.UUID:
    async with sessionmaker()() as db:
        sess = ChatSession(workspace_id=workspace_id, title="game", engine="claude")
        db.add(sess)
        await db.flush()
        run = Run(session_id=sess.id, workspace_id=workspace_id, prompt="build", engine="claude")
        db.add(run)
        await db.commit()
        return run.id


async def load(game_id: uuid.UUID):
    async with sessionmaker()() as db:
        return await db.get(GameProject, game_id)


# ---- 빠른 검사 (격리 러너 불필요) ----------------------------------------------------------------


async def test_successful_build_records_commit_hash_artifact_and_run(tmp_path: Path):
    game_id, root = await make_game("snap-one")
    head = git(root, "rev-parse", "HEAD")
    async with sessionmaker()() as db:
        game = await db.get(GameProject, game_id)
        run_id = await make_run(game.workspace_id)
        b = await build_game(db, game, adapter=FakeAdapter(tmp_path / "w"), run_id=run_id)
        assert (b.status, b.seq, b.source_commit, b.source_dirty, b.lock_verified) == ("succeeded", 1, head, False, True)
        assert b.run_id == run_id and len(b.manifest_sha256) == 64
        bundle = bundle_path(b)
        assert bundle is not None and bundle.parent.name == str(b.id) and bundle.parent.parent.name == str(game_id)
        assert b.bundle_files == 2 and b.bundle_bytes == sum(f.stat().st_size for f in bundle.rglob("*") if f.is_file())
        assert b.bundle_sha256 == bundle_digest(bundle)[0] and verify_snapshot(b)
        art = await db.get(Artifact, b.artifact_id)
        assert art.source == "game_build" and art.run_id == run_id and art.workspace_id == game.workspace_id
        with zipfile.ZipFile(art.stored_path) as z:
            assert sorted(z.namelist()) == ["assets/app.js", "index.html"]
        assert hashlib.sha256(Path(art.stored_path).read_bytes()).hexdigest() == art.sha256
    assert not (tmp_path / "w" / str(b.id)).exists()  # 스테이징 정리


async def test_bundle_is_read_only_and_tamper_is_detected(tmp_path: Path):
    game_id, _ = await make_game("snap-ro")
    async with sessionmaker()() as db:
        b = await build_game(db, await db.get(GameProject, game_id), adapter=FakeAdapter(tmp_path / "w"))
        bundle = bundle_path(b)
        with pytest.raises(PermissionError):
            (bundle / "index.html").write_text("hacked")
        with pytest.raises(PermissionError):
            (bundle / "new.js").write_text("x")
        with pytest.raises(PermissionError):
            (bundle / "assets" / "app.js").unlink()
        # 권한을 풀고 바꾸면 해시 검증이 실패한다
        builds._thaw(bundle)
        (bundle / "index.html").write_text("hacked")
        assert not verify_snapshot(b)


async def test_rebuild_creates_new_snapshot_and_keeps_previous_untouched(tmp_path: Path):
    game_id, root = await make_game("snap-two")
    adapter = FakeAdapter(tmp_path / "w")
    async with sessionmaker()() as db:
        game = await db.get(GameProject, game_id)
        first = await build_game(db, game, adapter=adapter)
        first_digest, first_commit = first.bundle_sha256, first.source_commit
        (root / "a.txt").write_text("v2")
        git(root, "commit", "-qam", "v2")
        adapter.files = {"index.html": "<!doctype html><title>v2</title>"}
        second = await build_game(db, game, adapter=adapter)
        assert (first.seq, second.seq) == (1, 2) and first.id != second.id
        assert second.source_commit != first_commit and second.bundle_sha256 != first_digest
        assert bundle_path(first) != bundle_path(second)
        assert verify_snapshot(first) and verify_snapshot(second)
        assert first.bundle_sha256 == first_digest and bundle_digest(bundle_path(first))[0] == first_digest
        assert "v2" not in (bundle_path(first) / "index.html").read_text()
        assert first.artifact_id != second.artifact_id


async def test_dirty_tree_is_recorded_honestly(tmp_path: Path):
    game_id, root = await make_game("snap-dirty")
    (root / "a.txt").write_text("uncommitted")
    async with sessionmaker()() as db:
        b = await build_game(db, await db.get(GameProject, game_id), adapter=FakeAdapter(tmp_path / "w"))
        assert b.status == "succeeded" and b.source_dirty is True and b.source_commit == git(root, "rev-parse", "HEAD")


async def test_unapproved_lock_is_flagged_and_changed_decisions_fail_verification(tmp_path: Path):
    game_id, root = await make_game("snap-lock", approve=False)
    async with sessionmaker()() as db:
        game = await db.get(GameProject, game_id)
        assert (await build_game(db, game, adapter=FakeAdapter(tmp_path / "w"))).lock_verified is False
        await approve_lock(db, game, read_manifest(root)[0])
        await db.commit()
        assert (await build_game(db, game, adapter=FakeAdapter(tmp_path / "w"))).lock_verified is True
        m = manifest("snap-lock", locked_decisions=("전부 3D",))  # 에이전트가 저장소 파일의 잠금 결정을 바꿈
        (root / "ttakkari-game.json").write_text(__import__("json").dumps(m.to_json(), ensure_ascii=False))
        with pytest.raises(GameContractError) as err:  # 승인본과 다른 잠금 결정은 빌드 자체가 거부된다(G08)
            await build_game(db, game, adapter=FakeAdapter(tmp_path / "w"))
        assert err.value.code == "lock_violation"
        assert len((await db.scalars(select(GameBuild).where(GameBuild.game_id == game_id))).all()) == 2  # 빌드 행도 만들지 않았다


async def test_failed_build_is_recorded_without_snapshot(tmp_path: Path):
    game_id, _ = await make_game("snap-fail")
    async with sessionmaker()() as db:
        game = await db.get(GameProject, game_id)
        b = await build_game(db, game, adapter=FakeAdapter(tmp_path / "w", fail="빌드 실패(exit 2)"))
        assert (b.status, b.error, b.bundle_rel_path, b.artifact_id) == ("failed", "빌드 실패(exit 2)", None, None)
        assert "TS2304" in b.log_tail and b.finished_at is not None and bundle_path(b) is None and not verify_snapshot(b)
        assert not (get_settings().game_builds_dir / str(game_id) / str(b.id)).exists()
        ok = await build_game(db, game, adapter=FakeAdapter(tmp_path / "w"))  # 실패 뒤에도 순번은 이어진다
        assert (b.seq, ok.seq, ok.status) == (1, 2, "succeeded")


async def test_adapter_exception_marks_build_failed_and_reraises(tmp_path: Path):
    game_id, _ = await make_game("snap-boom")

    async def boom(_):
        raise RuntimeError("adapter crashed")

    async with sessionmaker()() as db:
        game = await db.get(GameProject, game_id)
        with pytest.raises(RuntimeError):
            await build_game(db, game, adapter=FakeAdapter(tmp_path / "w", hook=boom))
    async with sessionmaker()() as db:
        row = (await db.scalars(select(GameBuild).where(GameBuild.game_id == game_id))).one()
        assert row.status == "failed" and "adapter crashed" in row.error and row.finished_at is not None


async def test_source_changed_during_build_fails_the_snapshot(tmp_path: Path):
    game_id, _ = await make_game("snap-race")

    async def mutate(project: Path):
        (project / "a.txt").write_text("changed mid-build")

    async with sessionmaker()() as db:
        b = await build_game(db, await db.get(GameProject, game_id), adapter=FakeAdapter(tmp_path / "w", hook=mutate))
        assert b.status == "failed" and "소스가 변경" in b.error and b.bundle_rel_path is None


async def test_concurrent_build_is_rejected_and_stale_one_is_closed(tmp_path: Path):
    game_id, _ = await make_game("snap-conc")
    release = asyncio.Event()

    async def slow(_):
        await release.wait()

    async def first():
        async with sessionmaker()() as db:
            return await build_game(db, await db.get(GameProject, game_id), adapter=FakeAdapter(tmp_path / "w1", hook=slow))

    task = asyncio.create_task(first())
    for _ in range(100):
        await asyncio.sleep(0.05)
        async with sessionmaker()() as db:
            if (await db.scalars(select(GameBuild).where(GameBuild.status == "building"))).first():
                break
    async with sessionmaker()() as db:
        with pytest.raises(GameContractError) as e:
            await build_game(db, await db.get(GameProject, game_id), adapter=FakeAdapter(tmp_path / "w2"))
        assert e.value.code == "build_in_progress"
    release.set()
    assert (await task).status == "succeeded"
    # 서버가 죽어 building 으로 남은 오래된 행은 다음 빌드가 실패로 닫는다
    async with sessionmaker()() as db:
        from datetime import timedelta

        from ttakkari.models import utcnow

        db.add(GameBuild(game_id=game_id, seq=99, status="building", created_at=utcnow() - timedelta(hours=3)))
        await db.commit()
        nxt = await build_game(db, await db.get(GameProject, game_id), adapter=FakeAdapter(tmp_path / "w3"))
        assert nxt.status == "succeeded" and nxt.seq == 100
        assert (await db.scalar(select(GameBuild.status).where(GameBuild.seq == 99))) == "failed"


async def test_build_rejects_workspace_outside_game_dir_and_unknown_run(tmp_path: Path):
    game_id, _ = await make_game("snap-guard")
    async with sessionmaker()() as db:
        game = await db.get(GameProject, game_id)
        with pytest.raises(GameContractError) as e:
            await build_game(db, game, adapter=FakeAdapter(tmp_path / "w"), run_id=uuid.uuid4())
        assert e.value.code == "no_run"
        assert (await db.scalars(select(GameBuild))).all() == []  # 거부된 요청은 빌드 행을 만들지 않는다
        ws = await db.get(Workspace, game.workspace_id)
        ws.root_path = str(get_settings().allowed_roots[0] / "somewhere-else")
        await db.commit()
        with pytest.raises(GameContractError):
            await build_game(db, game, adapter=FakeAdapter(tmp_path / "w"))


# ---- 실제 Phaser 템플릿 격리 빌드 → 스냅샷 -------------------------------------------------------


@requires_runtime
async def test_real_phaser_build_snapshot_and_rebuild_immutability():
    game_id, root = await make_game("snap-real", real=True)
    runner = IsolatedBuildRunner(limits=IsolationLimits(install_timeout=300, build_timeout=180))
    async with sessionmaker()() as db:
        game = await db.get(GameProject, game_id)
        run_id = await make_run(game.workspace_id)
        first = await build_game(db, game, runner=runner, run_id=run_id)
        assert first.status == "succeeded", first.error
        b1 = bundle_path(first)
        assert (b1 / "index.html").is_file() and first.source_commit == git(root, "rev-parse", "HEAD")
        assert first.lock_verified and not first.source_dirty and verify_snapshot(first)
        assert any(p.suffix == ".js" and p.stat().st_size > 500_000 for p in (b1 / "assets").iterdir())
        before = bundle_digest(b1)
        # 소스를 고쳐 커밋한 뒤 재빌드
        main_ts = root / "src" / "main.ts"
        main_ts.write_text(main_ts.read_text().replace("Ttakkari Phaser Game'", "Ttakkari Phaser Game v2'"))
        git(root, "commit", "-qam", "v2 title")
        second = await build_game(db, game, runner=runner)
        assert second.status == "succeeded", second.error
        assert second.seq == 2 and second.source_commit == git(root, "rev-parse", "HEAD") != first.source_commit
        assert second.bundle_sha256 != first.bundle_sha256
        assert bundle_digest(b1) == before and verify_snapshot(first) and verify_snapshot(second)
        art = await db.get(Artifact, second.artifact_id)
        with zipfile.ZipFile(art.stored_path) as z:
            assert "index.html" in z.namelist()
