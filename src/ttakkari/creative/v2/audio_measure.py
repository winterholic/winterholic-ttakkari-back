"""Audio measurements shared by the local review evaluator and the audio remix repair.

Everything here is a measurement of decoded samples; none of it is a listening judgement.
Policy numbers are acceptance thresholds for a music-only or voice-over mix, not quality scores.
"""

from __future__ import annotations

import math
import re
import subprocess
from pathlib import Path
from typing import Any

import numpy as np

from ttakkari.creative.render import ffmpeg

SAMPLE_RATE = 48000
CHANNELS = 2
# Integrated loudness window a scene must sit in to count as audible and not blasting.
LUFS_FLOOR = -26.0
LUFS_CEILING = -9.0
TRUE_PEAK_CEILING = -1.0
TARGET_LUFS = -18.0
# A level dip this deep (dB below the interior median) that lasts at least `DIP_MIN_SECONDS` is a seam.
DIP_DEPTH_DB = 6.0
DIP_MIN_SECONDS = 0.15
_HOP = 0.02


def decode_pcm(path: Path, *, sample_rate: int = SAMPLE_RATE) -> np.ndarray:
    """Float32 samples shaped (n, CHANNELS), resampled/remixed by ffmpeg."""
    result = subprocess.run(
        [ffmpeg(), "-hide_banner", "-loglevel", "error", "-nostdin", "-i", str(path), "-vn",
         "-f", "f32le", "-ar", str(sample_rate), "-ac", str(CHANNELS), "-"],
        capture_output=True, timeout=300, check=False,
    )
    if result.returncode:
        raise RuntimeError(f"audio decode failed ({result.returncode}): {result.stderr[-400:].decode(errors='replace')}")
    data = np.frombuffer(result.stdout, dtype="<f4")
    return data[: len(data) // CHANNELS * CHANNELS].reshape(-1, CHANNELS).astype(np.float32)


def _number(text: str) -> float:
    return float("-inf") if text.strip().startswith("-inf") else float(text)


def loudness(samples: np.ndarray, sample_rate: int = SAMPLE_RATE) -> dict[str, float] | None:
    """EBU R128 integrated loudness and true peak of the given samples; None if shorter than 0.4 s."""
    if len(samples) < sample_rate * 0.4:
        return None
    result = subprocess.run(
        [ffmpeg(), "-hide_banner", "-nostdin", "-f", "f32le", "-ar", str(sample_rate), "-ac", str(CHANNELS),
         "-i", "pipe:0", "-af", "ebur128=peak=true", "-f", "null", "-"],
        input=np.ascontiguousarray(samples, dtype="<f4").tobytes(), capture_output=True, timeout=300, check=False,
    )
    summary = result.stderr.decode(errors="replace").rsplit("Summary:", 1)[-1]
    integrated = re.search(r"\bI:\s+(-?inf|-?[\d.]+) LUFS", summary)
    peak = re.search(r"\bPeak:\s+(-?inf|-?[\d.]+) dBFS", summary)
    if result.returncode or not integrated or not peak:
        raise RuntimeError(f"loudness measurement failed ({result.returncode}): {summary[-300:]}")
    return {"lufs": _number(integrated.group(1)), "true_peak_db": _number(peak.group(1))}


def envelope_db(samples: np.ndarray, sample_rate: int = SAMPLE_RATE) -> np.ndarray:
    """Smoothed 20 ms RMS level in dB (about 100 ms smoothing), one value per hop."""
    mono = samples.mean(axis=1) if samples.ndim == 2 else samples
    win = int(_HOP * sample_rate)
    frames = len(mono) // win
    if frames < 5:
        return np.full(max(frames, 1), -120.0)
    rms = np.sqrt((mono[: frames * win].reshape(frames, win).astype(np.float64) ** 2).mean(axis=1))
    db = 20 * np.log10(np.maximum(rms, 1e-6))
    return np.convolve(np.pad(db, 2, mode="edge"), np.ones(5) / 5, mode="valid")


def find_dips(samples: np.ndarray, sample_rate: int = SAMPLE_RATE) -> list[dict[str, float]]:
    """Level dips inside the track, ignoring the intended fade-in and fade-out at both ends."""
    env = envelope_db(samples, sample_rate)
    seconds = len(env) * _HOP
    guard = int(min(2.0, seconds * 0.1) / _HOP)
    interior = env[guard : len(env) - guard]
    if len(interior) < 10:
        return []
    reference = float(np.median(interior))
    if reference < -70:
        return []
    below = interior < reference - DIP_DEPTH_DB
    dips, index = [], 0
    while index < len(below):
        if not below[index]:
            index += 1
            continue
        end = index
        while end < len(below) and below[end]:
            end += 1
        if (end - index) * _HOP >= DIP_MIN_SECONDS:
            low = index + int(np.argmin(interior[index:end]))
            dips.append({
                "start": round((guard + index) * _HOP, 3), "end": round((guard + end) * _HOP, 3),
                "at": round((guard + low) * _HOP, 3), "depth_db": round(reference - float(interior[low]), 1),
                "reference_db": round(reference, 1),
            })
        index = end
    return dips


def clipped_fraction(samples: np.ndarray) -> float:
    return float((np.abs(samples) >= 0.999).mean()) if len(samples) else 0.0


def level_verdict(measured: dict[str, float] | None) -> tuple[str, str]:
    """('fail'|'ok'|'unknown', reason) for one window's loudness measurement."""
    if measured is None:
        return "unknown", "구간이 0.4초보다 짧아 라우드니스를 측정하지 못했다"
    lufs, peak = measured["lufs"], measured["true_peak_db"]
    if not math.isfinite(lufs) or lufs < LUFS_FLOOR:
        return "fail", f"통합 라우드니스 {lufs:.1f} LUFS가 하한 {LUFS_FLOOR:g} LUFS보다 낮다 (true peak {peak:.1f} dBFS)"
    if lufs > LUFS_CEILING or peak > TRUE_PEAK_CEILING:
        return "fail", f"통합 {lufs:.1f} LUFS, true peak {peak:.1f} dBFS가 상한({LUFS_CEILING:g} LUFS, {TRUE_PEAK_CEILING:g} dBFS)을 넘는다"
    return "ok", f"통합 {lufs:.1f} LUFS, true peak {peak:.1f} dBFS"


def scene_levels(samples: np.ndarray, scenes: list[dict[str, Any]], sample_rate: int = SAMPLE_RATE) -> dict[str, dict[str, float] | None]:
    out = {}
    for scene in scenes:
        start, end = int(scene["start_time"] * sample_rate), int(scene["end_time"] * sample_rate)
        out[scene["scene_id"]] = loudness(samples[start:end], sample_rate)
    return out
