"""제작 엔진 실행 계약과 실제 미디어 QA.

엔진 어댑터는 레지스트리의 제공자 항목(`skill.v1_ffmpeg_slideshow` 등)과 1:1로 묶인다.
계획의 필수 제공자가 모두 어떤 어댑터가 맡는 항목이고, 그 어댑터의 사전 점검(preflight)이 통과해야 디스패치한다.
실행 기록에는 어댑터 id·버전·입력/출력 식별자를 남긴다. 어댑터가 없는 제공자(ComfyUI, Remotion 등)는 디스패치하지 않는다.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, ClassVar, Protocol

from ttakkari.creative.render import dimensions, ffmpeg

CAPTION_FONT = Path("/System/Library/Fonts/AppleSDGothicNeo.ttc")


@dataclass(frozen=True)
class Limits:
    """어댑터가 한 번의 실행(구간)에서 받는 규격."""

    aspects: tuple[str, ...]
    min_scenes: int
    max_scenes: int
    max_scene_seconds: float
    max_segment_seconds: float
    min_segment_seconds: float


@dataclass(frozen=True)
class Preflight:
    ok: bool
    version: str | None
    missing: list[str] = field(default_factory=list)


class EngineAdapter(Protocol):
    id: str
    providers: frozenset[str]
    limits: Limits
    # 스타일 DNA에서 이 엔진이 실제로 반영하는 매개변수 이름과 허용 값.
    style_params: dict[str, tuple[str, ...]]

    def preflight(self) -> Preflight: ...


class V1SlideshowAdapter:
    """스튜디오 v1 FFmpeg 슬라이드쇼 렌더러(`creative/render.py`)."""

    id = "skill.v1_ffmpeg_slideshow"
    providers = frozenset({"skill.v1_ffmpeg_slideshow", "tool.ffmpeg"})
    # creative/catalog.py FORMATS['music_video']와 service.Scene 한도.
    limits = Limits(
        aspects=("16:9", "1:1"),
        min_scenes=3,
        max_scenes=40,
        max_scene_seconds=60.0,
        max_segment_seconds=240.0,
        min_segment_seconds=3.0,
    )
    style_params: ClassVar[dict[str, tuple[str, ...]]] = {
        "animation": ("still", "zoom_in", "zoom_out", "pan", "pan_down", "pulse"),
        "transition": ("fade", "cut"),
        "caption_style": ("bottom_card", "center_highlight", "title_card", "code_overlay"),
    }

    def preflight(self) -> Preflight:
        missing: list[str] = []
        version: str | None = None
        try:
            exe = ffmpeg()
            out = subprocess.run(
                [exe, "-version"], capture_output=True, text=True, timeout=15, check=False
            )
            first = (out.stdout or "").splitlines()[0] if out.stdout else ""
            if out.returncode != 0 or not first:
                missing.append("ffmpeg 실행 파일이 버전을 응답하지 않습니다.")
            else:
                version = first.strip()
        except (OSError, RuntimeError, subprocess.SubprocessError) as e:
            missing.append(f"ffmpeg 실행 파일을 쓸 수 없습니다: {e}")
        if not CAPTION_FONT.is_file():
            missing.append(f"한국어 자막 글꼴이 없습니다: {CAPTION_FONT}")
        return Preflight(ok=not missing, version=version, missing=missing)


ADAPTERS: list[EngineAdapter] = [V1SlideshowAdapter()]


def adapter_for(providers: set[str]) -> EngineAdapter | None:
    """필수 제공자를 모두 맡는 어댑터. 하나라도 맡지 못하면 None."""
    for adapter in ADAPTERS:
        if providers and providers <= adapter.providers:
            return adapter
    return None


# --- 실제 미디어 QA ---------------------------------------------------------


def _run_ffmpeg(args: list[str], timeout: int = 300) -> str:
    result = subprocess.run(
        [ffmpeg(), "-hide_banner", "-nostdin", *args],
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    return result.stderr


def probe(path: Path) -> dict[str, Any]:
    text = _run_ffmpeg(["-i", str(path)], timeout=30)
    duration = re.search(r"Duration: (\d+):(\d+):(\d+(?:\.\d+)?)", text)
    video = re.search(r"Video: [^\n]*?, (\d{2,5})x(\d{2,5})", text)
    fps = re.search(r"(\d+(?:\.\d+)?) fps", text)
    return {
        "duration": (
            int(duration.group(1)) * 3600
            + int(duration.group(2)) * 60
            + float(duration.group(3))
            if duration
            else None
        ),
        "width": int(video.group(1)) if video else None,
        "height": int(video.group(2)) if video else None,
        "fps": float(fps.group(1)) if fps else None,
        "has_video": "Video:" in text,
        "has_audio": "Audio:" in text,
    }


def black_ratio(path: Path, duration: float) -> float:
    text = _run_ffmpeg(
        ["-i", str(path), "-vf", "blackdetect=d=0.5:pix_th=0.10", "-an", "-f", "null", "-"]
    )
    black = sum(float(m) for m in re.findall(r"black_duration:(\d+(?:\.\d+)?)", text))
    return black / duration if duration else 1.0


def volume(path: Path) -> dict[str, float | None]:
    text = _run_ffmpeg(["-i", str(path), "-af", "volumedetect", "-vn", "-f", "null", "-"])
    mean = re.search(r"mean_volume: (-?\d+(?:\.\d+)?) dB", text)
    peak = re.search(r"max_volume: (-?\d+(?:\.\d+)?) dB", text)
    return {
        "mean_db": float(mean.group(1)) if mean else None,
        "max_db": float(peak.group(1)) if peak else None,
    }


DURATION_TOLERANCE_SECONDS = 0.35
SILENT_PEAK_DB = -60.0
MOSTLY_BLACK = 0.9


def qa_media(
    path: Path,
    *,
    aspect: str,
    planned_seconds: float,
    tolerance: float = DURATION_TOLERANCE_SECONDS,
) -> dict[str, Any]:
    """결과 미디어를 실제로 읽어 측정한다. 항목마다 측정값과 통과 여부를 남긴다."""
    info = probe(path)
    expected = dimensions(aspect)
    checks: list[dict[str, Any]] = []

    def add(check_id: str, passed: bool, detail: str, measured: Any) -> None:
        checks.append({"id": check_id, "passed": bool(passed), "detail": detail, "measured": measured})

    add("video_track", info["has_video"], "영상 트랙", info["has_video"])
    add("audio_track", info["has_audio"], "오디오 트랙", info["has_audio"])
    resolution = (info["width"], info["height"])
    add(
        "resolution",
        resolution == expected,
        f"기대 {expected[0]}x{expected[1]}",
        f"{resolution[0]}x{resolution[1]}",
    )
    duration = info["duration"]
    add(
        "duration",
        duration is not None
        and abs(duration - planned_seconds) <= tolerance,
        f"계획 {planned_seconds:g}초 ±{tolerance:g}초",
        duration,
    )
    if info["has_video"] and duration:
        ratio = black_ratio(path, duration)
        add("not_black", ratio < MOSTLY_BLACK, f"검은 화면 비율 {MOSTLY_BLACK:.0%} 미만", round(ratio, 3))
    if info["has_audio"]:
        level = volume(path)
        add(
            "audible",
            level["max_db"] is not None and level["max_db"] > SILENT_PEAK_DB,
            f"오디오 최대 음량 {SILENT_PEAK_DB:g} dB 초과",
            level,
        )
    return {"passed": all(c["passed"] for c in checks), "checks": checks, "probe": info}
