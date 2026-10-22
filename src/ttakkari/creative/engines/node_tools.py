"""Node 기반 엔진(Remotion·Three)이 공유하는 작업 폴더 준비와 도구 탐색.

도구 체인(node_modules)은 설치된 lab 프로젝트의 것을 읽기 전용으로 링크해 쓰고, 렌더 코드는 이 저장소의 assets만 복사한다.
작업 폴더는 시도마다 새로 만들고 끝나면 지워서 lab 프로젝트나 다른 시도와 파일을 공유하지 않는다.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

LAB_ROOT = Path(
    os.environ.get(
        "TTAKKARI_STUDIO_LAB",
        "/Users/winterholic/development/projects/ttakkari-studio-lab",
    )
)
ASSETS = Path(__file__).parent / "assets"
CHROME_PATH = Path(
    os.environ.get(
        "TTAKKARI_CHROME",
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    )
)


def find_node() -> str | None:
    override = os.environ.get("TTAKKARI_NODE")
    candidates = [override] if override else []
    candidates += [
        shutil.which("node"),
        "/opt/homebrew/bin/node",
        "/usr/local/bin/node",
    ]
    for item in candidates:
        if item and Path(item).is_file() and os.access(item, os.X_OK):
            return item
    return None


def node_version(node: str) -> str:
    result = subprocess.run(
        [node, "--version"], capture_output=True, text=True, timeout=15, check=False
    )
    if result.returncode != 0:
        raise RuntimeError("node --version 실행에 실패했습니다.")
    return result.stdout.strip()


def package_version(node: str, node_modules: Path, package: str) -> str:
    """node_modules 안 패키지의 package.json 버전을 node로 읽는다(실제 실행으로 확인)."""
    script = (
        "const fs=require('fs'),p=require('path');"
        "const f=p.join(process.argv[1],process.argv[2],'package.json');"
        "process.stdout.write(JSON.parse(fs.readFileSync(f,'utf8')).version)"
    )
    result = subprocess.run(
        [node, "-e", script, str(node_modules), package],
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    if result.returncode != 0 or not result.stdout.strip():
        raise RuntimeError(f"{package} 버전을 읽지 못했습니다.")
    return result.stdout.strip()


def stage_project(asset_name: str, workdir: Path, node_modules: Path) -> Path:
    """assets/<asset_name>을 작업 폴더로 복사하고 node_modules를 읽기 전용 링크로 연결한다."""
    source = ASSETS / asset_name
    target = workdir / asset_name
    shutil.copytree(source, target, symlinks=False)
    link = target / "node_modules"
    link.symlink_to(node_modules, target_is_directory=True)
    return target
