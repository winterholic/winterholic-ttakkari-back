"""게임 엔진 어댑터 (TASK-G03, docs/design/game-studio.md §3.1, ADR 0046).

공통 인터페이스는 설계서대로 두고 구현은 Web/Phaser 3 하나뿐이다. 이 엔진이 못 하는 기능은 예외나 거짓 성공이 아니라
`supported=False` 와 사유를 담은 결과로 돌려준다(Phase 2 이후 다른 엔진 어댑터가 같은 계약을 채운다).
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ttakkari.config import get_settings
from ttakkari.games.contract import (
    MANIFEST_FILE,
    GameContractError,
    ProjectManifest,
    game_dir,
    read_manifest,
)
from ttakkari.games.isolation import BUILD_ID_RE, IsolatedBuildRunner, IsolationError

TEMPLATES_DIR = Path(__file__).with_name("templates")
GIT_ENV_KEYS = ("PATH", "HOME", "TMPDIR")


@dataclass
class BuildResult:
    success: bool
    build_id: str
    output_dir: Path | None = None
    bundle_files: list[str] = field(default_factory=list)
    log: str = ""
    error: str | None = None


@dataclass
class TestResult:
    __test__ = False  # pytest 가 수집하지 않게 한다
    passed: bool
    supported: bool = True
    fps_average: float = 0.0
    error_logs: list[str] = field(default_factory=list)
    screenshots: list[str] = field(default_factory=list)
    game_state_dump: dict[str, Any] = field(default_factory=dict)


class GameEngineAdapter(ABC):
    """모든 게임 엔진 어댑터가 구현하는 표준 인터페이스."""

    @property
    @abstractmethod
    def engine_id(self) -> str:
        """엔진 식별자(예: web-phaser)."""

    @abstractmethod
    async def scaffold_project(self, project_path: Path, manifest: ProjectManifest) -> None: ...

    @abstractmethod
    async def build(self, project_path: Path, build_id: str) -> BuildResult: ...

    @abstractmethod
    async def run_automated_test(self, project_path: Path, build_id: str, scenario: str) -> TestResult: ...

    @abstractmethod
    def get_runtime_serve_path(self, project_path: Path, build_id: str) -> Path: ...

    @abstractmethod
    def extract_scene_metadata(self, project_path: Path) -> dict[str, Any]: ...


def resolve_project_path(project_path: Path) -> Path:
    """어댑터가 건드릴 수 있는 경로는 games_root/<slug> 하나뿐이다.

    임의 절대경로, `..`, 심볼릭 링크, 하위 디렉터리, 다른 게임의 경로를 모두 거부한다. 등록(contract.game_dir)과 같은 규칙이다.
    """
    raw = Path(project_path)
    if not raw.is_absolute():
        raise GameContractError("프로젝트 경로는 절대 경로여야 합니다.", "bad_path")
    if ".." in raw.parts:
        raise GameContractError("프로젝트 경로에 '..' 를 쓸 수 없습니다.", "bad_path")
    expected = game_dir(raw.name)
    if raw != expected:
        raise GameContractError("프로젝트 경로가 게임 저장소 위치(games_root/<slug>)가 아닙니다.", "outside_root")
    if raw.is_symlink() or (raw.exists() and raw.resolve() != raw):
        raise GameContractError("프로젝트 경로가 심볼릭 링크를 포함합니다.", "bad_path")
    return raw


async def _git(cwd: Path, *args: str) -> str:
    """게임 저장소 git 호출. 훅을 끄고 사용자·시스템 설정을 읽지 않아 저장소 안 코드가 실행되지 않게 한다."""
    env = {k: os.environ[k] for k in GIT_ENV_KEYS if k in os.environ}
    env |= {"GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null", "GIT_TERMINAL_PROMPT": "0"}
    base = ["git", "-c", "core.hooksPath=/dev/null", "-c", "user.name=Ttakkari Game Studio", "-c", "user.email=game-studio@ttakkari.local"]
    proc = await asyncio.create_subprocess_exec(
        *base, *args, cwd=cwd, env=env, stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
    out, _ = await asyncio.wait_for(proc.communicate(), 60)
    if proc.returncode != 0:
        raise GameContractError(f"git {args[0]} 실패: {out.decode('utf-8', 'replace')[-300:]}", "git_failed")
    return out.decode("utf-8", "replace").strip()


class PhaserAdapter(GameEngineAdapter):
    ENGINE_ID = "web-phaser"
    TEMPLATE = TEMPLATES_DIR / "web-phaser"

    def __init__(self, runner: IsolatedBuildRunner | None = None, work_dir: Path | None = None):
        self._runner = runner
        self._work_dir = work_dir

    @property
    def engine_id(self) -> str:
        return self.ENGINE_ID

    @property
    def work_dir(self) -> Path:
        """어댑터 빌드 결과 위치. 에이전트가 쓸 수 없는 data_dir 아래다."""
        return self._work_dir or get_settings().game_builds_dir / "_adapter"

    @property
    def runner(self) -> IsolatedBuildRunner:
        if self._runner is None:
            self._runner = IsolatedBuildRunner()
        return self._runner

    def _manifest_for(self, project_path: Path) -> ProjectManifest:
        root = resolve_project_path(project_path)
        manifest, _ = read_manifest(root)
        if manifest.engine != self.ENGINE_ID:
            raise GameContractError(f"이 어댑터는 {self.ENGINE_ID} 만 빌드합니다(매니페스트 engine={manifest.engine}).", "wrong_engine")
        if manifest.id != root.name:
            raise GameContractError("매니페스트 id 와 디렉터리 이름이 다릅니다.", "bad_manifest")
        return manifest

    async def scaffold_project(self, project_path: Path, manifest: ProjectManifest) -> None:
        if manifest.engine != self.ENGINE_ID:
            raise GameContractError(f"이 어댑터는 {self.ENGINE_ID} 만 생성합니다.", "wrong_engine")
        root = resolve_project_path(project_path)
        if root.name != manifest.id:
            raise GameContractError("매니페스트 id 와 디렉터리 이름이 다릅니다.", "bad_manifest")
        if manifest.entrypoint != "index.html":
            raise GameContractError("Phaser 템플릿의 entrypoint 는 index.html 입니다.", "bad_manifest")
        if root.exists() and any(root.iterdir()):
            raise GameContractError("이미 내용이 있는 디렉터리에는 scaffold 할 수 없습니다.", "exists")
        root.parent.mkdir(parents=True, exist_ok=True)

        def _copy() -> None:
            shutil.copytree(self.TEMPLATE, root, dirs_exist_ok=True, ignore=shutil.ignore_patterns("node_modules", "dist"))
            (root / MANIFEST_FILE).write_text(json.dumps(manifest.to_json(), ensure_ascii=False, indent=2) + "\n")

        await asyncio.to_thread(_copy)
        await _git(root, "init", "-q", "-b", "main")
        await _git(root, "add", "-A")
        await _git(root, "commit", "-q", "-m", f"scaffold {self.ENGINE_ID} project")

    async def build(self, project_path: Path, build_id: str) -> BuildResult:
        if not BUILD_ID_RE.fullmatch(build_id):
            raise GameContractError("빌드 ID 형식이 올바르지 않습니다.", "bad_build_id")
        manifest = self._manifest_for(project_path)
        root = resolve_project_path(project_path)
        self.work_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            res = await self.runner.build_project(root, self.work_dir, build_id)
        except IsolationError as e:
            return BuildResult(False, build_id, error=str(e), log=f"{e.code}: {e}")
        if res.success and res.output_dir is not None and not (res.output_dir / manifest.entrypoint).is_file():
            return BuildResult(False, build_id, log=res.log, error=f"빌드 산출물에 entrypoint({manifest.entrypoint})가 없습니다.")
        return BuildResult(res.success, build_id, res.output_dir, res.bundle_files, res.log, res.error)

    async def run_automated_test(self, project_path: Path, build_id: str, scenario: str) -> TestResult:
        resolve_project_path(project_path)
        return TestResult(False, supported=False, error_logs=["미지원: Phaser 어댑터의 무인 플레이 테스트는 TASK-G13 에서 구현한다."])

    def get_runtime_serve_path(self, project_path: Path, build_id: str) -> Path:
        resolve_project_path(project_path)
        if not BUILD_ID_RE.fullmatch(build_id):
            raise GameContractError("빌드 ID 형식이 올바르지 않습니다.", "bad_build_id")
        out = self.work_dir / build_id / "out"
        if not (out / "index.html").is_file():
            raise GameContractError("해당 빌드의 번들이 없습니다.", "no_build")
        return out

    def extract_scene_metadata(self, project_path: Path) -> dict[str, Any]:
        resolve_project_path(project_path)
        return {"supported": False, "reason": "미지원: 씬·엔티티 계측 브리지는 Phase 3 에서 구현한다.", "scenes": [], "entities": []}


_ADAPTERS: dict[str, type[GameEngineAdapter]] = {PhaserAdapter.ENGINE_ID: PhaserAdapter}


def get_adapter(engine_id: str, **kwargs: Any) -> GameEngineAdapter:
    cls = _ADAPTERS.get(engine_id)
    if cls is None:
        raise GameContractError(f"지원하지 않는 엔진입니다: {engine_id}", "unsupported_engine")
    return cls(**kwargs)  # type: ignore[call-arg]

