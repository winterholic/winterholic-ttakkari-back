"""미디어 도구 실행 파일 탐색. 이름이 같아도 진짜 바이너리인지 확인한다.

호스트 `~/bin/ffprobe`는 `ffmpeg -i` 출력을 정규식으로 읽는 Python shim이라 `-show_entries stream=nb_frames`를
무시한다. 이 shim을 쓰면 HyperFrames가 "ffprobe returned no frame count"로 렌더 마지막 단계에서 실패한다
(2026-11-21 실측). 그래서 후보 경로마다 실제 ffprobe 빌드인지 검사하고 shim은 건너뛴다.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from functools import lru_cache
from pathlib import Path

LAB_ROOT_ENV = "TTAKKARI_LAB_ROOT"
FFPROBE_ENV = "TTAKKARI_FFPROBE_PATH"
# Remotion compositor가 번들한 진짜 ffprobe 래퍼. 호스트에서 진짜 ffprobe는 현재 이것뿐이다.
LAB_FFPROBE = Path("remotion/bin/ffprobe")


def _lab_root() -> Path:
    env = os.environ.get(LAB_ROOT_ENV)
    return Path(env) if env else Path.home() / "development/projects/ttakkari-studio-lab"


@lru_cache(maxsize=32)
def _is_real(path: str, mtime_ns: int) -> bool:
    """`-version` 출력에 빌드 설정(configuration:)이 있어야 진짜다. shim은 첫 줄 하나만 낸다."""
    del mtime_ns  # 파일이 바뀌면 캐시를 무효화하려는 키
    try:
        out = subprocess.run(
            [path, "-version"], capture_output=True, text=True, timeout=15, check=False
        )
    except (OSError, subprocess.SubprocessError):
        return False
    text = out.stdout or ""
    return out.returncode == 0 and text.startswith("ffprobe version") and "configuration:" in text


def is_real_ffprobe(path: str | Path) -> bool:
    p = Path(path)
    if not p.is_file() or not os.access(p, os.X_OK):
        return False
    try:
        return _is_real(str(p.resolve(strict=True)), p.stat().st_mtime_ns)
    except OSError:
        return False


def ffprobe_candidates() -> list[Path]:
    seen: list[Path] = []

    def add(value: str | Path | None) -> None:
        if value and Path(value) not in seen:
            seen.append(Path(value))

    add(os.environ.get(FFPROBE_ENV))
    add(os.environ.get("HYPERFRAMES_FFPROBE_PATH"))
    for directory in os.environ.get("PATH", "").split(os.pathsep):
        if directory:
            add(Path(directory) / "ffprobe")
    add(shutil.which("ffprobe"))
    add(_lab_root() / LAB_FFPROBE)
    return seen


def find_ffprobe() -> str | None:
    """진짜 ffprobe 경로. 없으면 None(shim으로 조용히 대체하지 않는다)."""
    for candidate in ffprobe_candidates():
        if is_real_ffprobe(candidate):
            return str(candidate)
    return None


def ffprobe_status() -> tuple[str | None, list[str]]:
    """탐침과 진단용: 찾은 경로와 건너뛴 shim 후보."""
    found = find_ffprobe()
    skipped = [
        str(c)
        for c in ffprobe_candidates()
        if c.is_file() and os.access(c, os.X_OK) and not is_real_ffprobe(c)
    ]
    return found, skipped
