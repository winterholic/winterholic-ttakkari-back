"""Phaser 어댑터: scaffold, 잠금 manifest, 임의 경로 거부, 실제 템플릿 격리 빌드 (TASK-G03)."""

import hashlib
import json
import os
import subprocess
from pathlib import Path

import pytest
from helpers import fresh_dir

from ttakkari.config import get_settings
from ttakkari.games.adapter import PhaserAdapter, get_adapter, resolve_project_path
from ttakkari.games.contract import (
    MANIFEST_FILE,
    GameContractError,
    ProjectManifest,
    read_manifest,
)
from ttakkari.games.isolation import IsolatedBuildRunner, IsolationLimits, probe_runtime

RUNTIME = probe_runtime()
if not RUNTIME.available and os.environ.get("TTAKKARI_REQUIRE_GAME_RUNTIME") == "1":
    pytest.fail(f"TTAKKARI_REQUIRE_GAME_RUNTIME=1 인데 컨테이너 런타임을 쓸 수 없다: {RUNTIME.reason}", pytrace=False)
requires_runtime = pytest.mark.skipif(not RUNTIME.available, reason=f"컨테이너 런타임 필요(격리 빌드 불가): {RUNTIME.reason}")


def manifest(slug: str = "adapter-demo", **over) -> ProjectManifest:
    base = {"id": slug, "name": "Adapter Demo", "genre": "arcade", "engine": "web-phaser", "version": "0.1.0",
            "entrypoint": "index.html", "locked_decisions": ("2D 아케이드", "터치 우선")}
    return ProjectManifest(**{**base, **over})


def git_out(root: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True, check=True).stdout


@pytest.fixture
def games_root() -> Path:
    return get_settings().games_root.resolve()


@pytest.fixture
def adapter(tmp_path: Path) -> PhaserAdapter:
    return PhaserAdapter(work_dir=tmp_path / "work")


# ---- 경로 거부 (런타임 불필요) ------------------------------------------------------------------


def test_resolve_project_path_rejects_arbitrary_paths(games_root: Path, tmp_path: Path):
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    for bad in (Path("relative/demo-game"), outside, games_root, games_root / "demo-game" / "sub", games_root / "x",
                games_root / "demo-game" / ".." / "other-game", Path("/etc"), Path("/")):
        with pytest.raises(GameContractError):
            resolve_project_path(bad)
    ok = resolve_project_path(games_root / "demo-game")
    assert ok == games_root / "demo-game"


def test_resolve_project_path_rejects_symlinked_game_dir(games_root: Path, tmp_path: Path):
    target = tmp_path / "real"
    target.mkdir()
    link = games_root / "link-game"
    link.symlink_to(target)
    try:
        with pytest.raises(GameContractError) as e:
            resolve_project_path(link)
        assert e.value.code == "bad_path"
    finally:
        link.unlink()


async def test_scaffold_writes_manifest_template_and_git_repo(adapter: PhaserAdapter, games_root: Path):
    m = manifest("scaffold-one")
    root = fresh_dir(games_root / "scaffold-one")  # 빈 디렉터리에는 scaffold 할 수 있다
    await adapter.scaffold_project(root, m)
    read, sha = read_manifest(root)
    assert read == m and len(sha) == 64
    for rel in ("package.json", "package-lock.json", "index.html", "src/main.ts", "vite.config.ts", ".gitignore"):
        assert (root / rel).is_file(), rel
    assert not (root / "node_modules").exists()
    log = git_out(root, "log", "--format=%s")
    assert "scaffold web-phaser project" in log
    assert git_out(root, "status", "--porcelain") == ""
    # 이미 내용이 있는 디렉터리에는 덮어쓰지 않는다
    with pytest.raises(GameContractError) as e:
        await adapter.scaffold_project(root, m)
    assert e.value.code == "exists"


async def test_scaffold_rejects_bad_inputs(adapter: PhaserAdapter, games_root: Path, tmp_path: Path):
    with pytest.raises(GameContractError):
        await adapter.scaffold_project(tmp_path / "outside-game", manifest("outside-game"))
    with pytest.raises(GameContractError):
        await adapter.scaffold_project(games_root / "name-mismatch", manifest("another-id"))
    with pytest.raises(GameContractError) as e:
        await adapter.scaffold_project(games_root / "godot-game", manifest("godot-game", engine="godot-4"))
    assert e.value.code == "wrong_engine"
    with pytest.raises(GameContractError):
        await adapter.scaffold_project(games_root / "entry-game", manifest("entry-game", entrypoint="dist/x.html"))
    assert not (games_root / "godot-game").exists() and not (games_root / "entry-game").exists()


async def test_build_rejects_wrong_engine_missing_manifest_and_bad_ids(adapter: PhaserAdapter, games_root: Path):
    root = fresh_dir(games_root / "no-manifest")
    with pytest.raises(GameContractError) as e:
        await adapter.build(root, "b1")
    assert e.value.code == "no_manifest"
    godot = fresh_dir(games_root / "godot-demo")
    (godot / MANIFEST_FILE).write_text(json.dumps(manifest("godot-demo", engine="godot-4").to_json()))
    with pytest.raises(GameContractError) as e:
        await adapter.build(godot, "b1")
    assert e.value.code == "wrong_engine"
    mismatch = fresh_dir(games_root / "dir-mismatch")
    (mismatch / MANIFEST_FILE).write_text(json.dumps(manifest("other-name").to_json()))
    with pytest.raises(GameContractError) as e:
        await adapter.build(mismatch, "b1")
    assert e.value.code == "bad_manifest"
    for bad in ("../x", "a/b", "", "-x", "x" * 41):
        with pytest.raises(GameContractError):
            await adapter.build(root, bad)
    with pytest.raises(GameContractError):
        await adapter.build(Path("/etc"), "b1")


async def test_manifest_symlink_is_rejected(adapter: PhaserAdapter, games_root: Path, tmp_path: Path):
    root = fresh_dir(games_root / "manifest-link")
    secret = tmp_path / "other.json"
    secret.write_text(json.dumps(manifest("manifest-link").to_json()))
    (root / MANIFEST_FILE).symlink_to(secret)
    with pytest.raises(GameContractError) as e:
        await adapter.build(root, "b1")
    assert e.value.code == "bad_manifest"


def test_unsupported_features_are_explicit(adapter: PhaserAdapter, games_root: Path):
    meta = adapter.extract_scene_metadata(games_root / "demo-game")
    assert meta["supported"] is False and "미지원" in meta["reason"]
    with pytest.raises(GameContractError) as e:
        adapter.get_runtime_serve_path(games_root / "demo-game", "never-built")
    assert e.value.code == "no_build"


async def test_automated_test_reports_unsupported(adapter: PhaserAdapter, games_root: Path):
    res = await adapter.run_automated_test(games_root / "demo-game", "b1", "smoke")
    assert res.supported is False and res.passed is False and "미지원" in res.error_logs[0]


def test_get_adapter_registry():
    assert get_adapter("web-phaser").engine_id == "web-phaser"
    with pytest.raises(GameContractError) as e:
        get_adapter("godot-4")
    assert e.value.code == "unsupported_engine"


# ---- 실제 Phaser 템플릿 격리 빌드 ----------------------------------------------------------------


@requires_runtime
async def test_real_phaser_template_builds_in_isolation(games_root: Path, tmp_path: Path):
    adapter = PhaserAdapter(
        runner=IsolatedBuildRunner(limits=IsolationLimits(install_timeout=300, build_timeout=180)), work_dir=tmp_path / "work")
    m = manifest("real-phaser")
    root = games_root / "real-phaser"
    await adapter.scaffold_project(root, m)
    before = {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in root.rglob("*") if p.is_file() and ".git" not in p.parts}
    res = await adapter.build(root, "real-1")
    assert res.success, f"{res.error}\n{res.log[-2000:]}"
    out = adapter.get_runtime_serve_path(root, "real-1")
    index = out / "index.html"
    assert index.is_file() and "<script" in index.read_text()
    assert any(f.endswith(".js") for f in res.bundle_files)
    assert max((out / f).stat().st_size for f in res.bundle_files if f.endswith(".js")) > 500_000  # phaser 가 실제로 번들됨
    # 원본 저장소는 빌드가 바꾸지 않는다(.git 포함 상태, node_modules/dist 미생성)
    after = {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in root.rglob("*") if p.is_file() and ".git" not in p.parts}
    assert before == after
    assert not (root / "node_modules").exists() and not (root / "dist").exists()
    status = git_out(root, "status", "--porcelain")
    assert status == ""
