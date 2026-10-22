"""FFmpeg 기반 미디어 조사와 기계 검사 도구.

ffprobe 없이 backend가 이미 쓰는 imageio-ffmpeg(ffmpeg 7.1)만으로 동작한다. 모든 호출은 argv 리스트이고 입력은 파일 경로 하나다.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any


class MediaToolError(RuntimeError):
    pass


def ffmpeg_path() -> str:
    override = os.environ.get("TTAKKARI_FFMPEG")
    if override:
        return override
    found = shutil.which("ffmpeg")
    if found:
        return found
    try:
        import imageio_ffmpeg

        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception as exc:
        raise MediaToolError(f"ffmpeg를 찾지 못했습니다: {exc}") from exc


def _run(args: list[str], *, timeout: float = 120.0) -> subprocess.CompletedProcess:
    return subprocess.run(
        [ffmpeg_path(), "-hide_banner", "-nostdin", *args],
        capture_output=True,
        text=True,
        errors="replace",
        timeout=timeout,
        check=False,
    )


def ffmpeg_version() -> str:
    result = _run(["-version"], timeout=15)
    match = re.search(r"ffmpeg version (\S+)", result.stdout)
    if result.returncode != 0 or not match:
        raise MediaToolError("ffmpeg -version 실행에 실패했습니다.")
    return match.group(1)


def probe_media(path: Path) -> dict[str, Any]:
    """컨테이너·스트림 요약. 읽을 수 없으면 MediaToolError(추정하지 않는다)."""
    text = _run(["-i", str(path)], timeout=30).stderr
    duration = re.search(
        r"Duration: (\d+):(\d+):(\d+(?:\.\d+)?), start: (-?[\d.]+)", text
    )
    if not duration:
        raise MediaToolError("읽을 수 없는 미디어이거나 길이를 확인할 수 없습니다.")
    h, m, s, start = duration.groups()
    info: dict[str, Any] = {
        "duration": round(int(h) * 3600 + int(m) * 60 + float(s), 3),
        "start_time": float(start),
        "video": None,
        "audio": None,
    }
    video = re.search(
        r"Stream #0:\d+[^:]*: Video: (\w+)[^\n]*?, (\d{2,5})x(\d{2,5})", text
    )
    if video:
        line = text[video.start() : text.find("\n", video.start())]
        fps = re.search(r"([\d.]+) fps", line)
        pix = re.search(r"Video: \w+(?: \([^)]*\))?(?: \([^)]*\))?, (\w+)", line)
        info["video"] = {
            "codec": video.group(1),
            "width": int(video.group(2)),
            "height": int(video.group(3)),
            "fps": float(fps.group(1)) if fps else None,
            "pix_fmt": pix.group(1) if pix else None,
        }
    audio = re.search(r"Stream #0:\d+[^:]*: Audio: (\w+)[^\n]*?, (\d+) Hz, (\w+)", text)
    if audio:
        info["audio"] = {
            "codec": audio.group(1),
            "sample_rate": int(audio.group(2)),
            "channels": audio.group(3),
        }
    if info["video"] is None and info["audio"] is None:
        raise MediaToolError("영상·음성 스트림이 없습니다.")
    return info


def decode_scan(
    path: Path, *, stream: str = "v:0", timeout: float = 600.0
) -> dict[str, Any]:
    """전 프레임을 끝까지 디코드한다. 오류가 있으면 -xerror로 즉시 멈추고 메시지를 돌려준다."""
    result = _run(
        [
            "-v",
            "error",
            "-xerror",
            "-i",
            str(path),
            "-map",
            f"0:{stream}",
            "-f",
            "null",
            "-",
            "-progress",
            "pipe:1",
        ],
        timeout=timeout,
    )
    frames = re.findall(r"^frame=(\d+)$", result.stdout, flags=re.MULTILINE)
    if stream.startswith("a"):
        frames = []
    errors = [line for line in result.stderr.splitlines() if line.strip()][:10]
    return {
        "ok": result.returncode == 0 and not errors,
        "frames": int(frames[-1]) if frames else None,
        "errors": errors,
        "returncode": result.returncode,
    }


def detect_black_freeze(
    path: Path, *, total_duration: float | None = None, timeout: float = 600.0
) -> dict[str, Any]:
    result = _run(
        [
            "-i",
            str(path),
            "-an",
            "-vf",
            "blackdetect=d=0.1:pic_th=0.98,freezedetect=n=-60dB:d=0.5",
            "-f",
            "null",
            "-",
        ],
        timeout=timeout,
    )
    text = result.stderr
    black = [
        {"start": float(a), "end": float(b), "duration": float(c)}
        for a, b, c in re.findall(
            r"black_start:([\d.]+) black_end:([\d.]+) black_duration:([\d.]+)", text
        )
    ]
    starts = [float(x) for x in re.findall(r"freeze_start: ([\d.]+)", text)]
    durations = [float(x) for x in re.findall(r"freeze_duration: ([\d.]+)", text)]
    ends = [float(x) for x in re.findall(r"freeze_end: ([\d.]+)", text)]
    freeze = []
    for index, start in enumerate(starts):
        duration = durations[index] if index < len(durations) else None
        end = ends[index] if index < len(ends) else None
        if end is None and total_duration is not None:
            # 클립 끝까지 이어진 정지는 freeze_end가 나오지 않는다. 끝을 클립 길이로 보고 선언 범위와 비교한다.
            end, duration = total_duration, max(total_duration - start, 0.0)
        freeze.append({"start": start, "end": end, "duration": duration})
    return {"black": black, "freeze": freeze, "returncode": result.returncode}


def audio_levels(path: Path, *, timeout: float = 300.0) -> dict[str, Any]:
    """샘플 피크·평균 음량과 무음 구간. 오디오 스트림이 없으면 has_audio=False."""
    result = _run(
        [
            "-i",
            str(path),
            "-vn",
            "-af",
            "volumedetect,silencedetect=n=-50dB:d=0.3",
            "-f",
            "null",
            "-",
        ],
        timeout=timeout,
    )
    text = result.stderr
    if "Audio:" not in text:
        return {"has_audio": False}
    peak = re.search(r"max_volume: (-?[\d.]+|-inf) dB", text)
    mean = re.search(r"mean_volume: (-?[\d.]+|-inf) dB", text)
    starts = [float(x) for x in re.findall(r"silence_start: (-?[\d.]+)", text)]
    ends = [
        (float(a), float(b))
        for a, b in re.findall(
            r"silence_end: (-?[\d.]+) \| silence_duration: ([\d.]+)", text
        )
    ]
    silence = []
    for index, start in enumerate(starts):
        end = ends[index][0] if index < len(ends) else None
        duration = ends[index][1] if index < len(ends) else None
        silence.append({"start": max(start, 0.0), "end": end, "duration": duration})

    def number(match):
        if not match:
            return None
        return float("-inf") if match.group(1) == "-inf" else float(match.group(1))

    return {
        "has_audio": True,
        "max_volume_db": number(peak),
        "mean_volume_db": number(mean),
        "silence": silence,
        "returncode": result.returncode,
    }


def extract_frames(
    path: Path, frame_numbers: list[int], out_dir: Path, *, timeout: float = 300.0
) -> dict[int, Path]:
    """0부터 센 프레임 번호의 PNG를 한 번의 디코드로 뽑는다. 뽑지 못한 번호는 결과에서 빠진다."""
    out_dir.mkdir(parents=True, exist_ok=True)
    wanted = sorted({n for n in frame_numbers if n >= 0})
    if not wanted:
        return {}
    expr = "+".join(f"eq(n\\,{n})" for n in wanted)
    pattern = out_dir / "k_%06d.png"
    _run(
        [
            "-v",
            "error",
            "-i",
            str(path),
            "-an",
            "-vf",
            f"select='{expr}'",
            "-fps_mode",
            "passthrough",
            "-start_number",
            "0",
            str(pattern),
        ],
        timeout=timeout,
    )
    produced = sorted(out_dir.glob("k_*.png"))
    return {n: p for n, p in zip(wanted, produced, strict=False)}


def gray_motion_series(
    path: Path, *, size: str = "48x27", timeout: float = 300.0
) -> dict[str, Any]:
    """프레임별 평균 휘도와 직전 프레임과의 평균 절대 차이(작은 회색 영상 기준). 모션 극값과 컷 후보를 찾는 입력이다."""
    import numpy as np

    width, height = (int(v) for v in size.split("x"))
    proc = subprocess.run(
        [
            ffmpeg_path(),
            "-hide_banner",
            "-nostdin",
            "-v",
            "error",
            "-i",
            str(path),
            "-an",
            "-vf",
            f"scale={width}:{height}:flags=area,format=gray",
            "-f",
            "rawvideo",
            "-",
        ],
        capture_output=True,
        timeout=timeout,
        check=False,
    )
    frame_bytes = width * height
    count = len(proc.stdout) // frame_bytes
    if count == 0:
        return {"frames": 0, "luma": [], "diff": []}
    data = (
        np.frombuffer(proc.stdout[: count * frame_bytes], dtype=np.uint8)
        .reshape(count, frame_bytes)
        .astype(np.float32)
    )
    luma = data.mean(axis=1)
    diff = np.zeros(count, dtype=np.float32)
    if count > 1:
        diff[1:] = np.abs(data[1:] - data[:-1]).mean(axis=1)
    return {
        "frames": count,
        "luma": luma.round(3).tolist(),
        "diff": diff.round(3).tolist(),
    }


def stream_durations(path: Path, *, timeout: float = 300.0) -> dict[str, float | None]:
    """스트림별 실제 디코드 길이(초). 컨테이너 길이와 따로 재서 음성·영상 길이 차이를 본다."""
    found: dict[str, float | None] = {"video": None, "audio": None}
    for label, stream in (("video", "v:0"), ("audio", "a:0")):
        result = _run(
            [
                "-v",
                "error",
                "-i",
                str(path),
                "-map",
                f"0:{stream}",
                "-f",
                "null",
                "-",
                "-progress",
                "pipe:1",
            ],
            timeout=timeout,
        )
        if result.returncode != 0:
            continue
        micro = re.findall(r"^out_time_us=(\d+)$", result.stdout, flags=re.MULTILINE)
        if micro:
            found[label] = int(micro[-1]) / 1_000_000
    return found
