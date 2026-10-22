"""오디오 선행 타임라인용 분석: 길이, 온셋, 박자 격자, 에너지 구간을 정수 샘플로 구하고 정수 프레임으로 변환한다.

원시 시점은 항상 샘플 정수로 남기고 프레임은 `TimelineRevision.sample_to_frame`(정수 반올림, 누적 float 없음)으로만 변환한다.
방법·신뢰도를 값마다 남기며 `validated=false`다. 일반 음악의 비트 정확도는 이번 구현에서 검증하지 않았다(ADR §2: 미검증).
"""

from __future__ import annotations

import subprocess
from itertools import pairwise
from typing import Any

import numpy as np

from ttakkari.creative.director import TimelineRevision
from ttakkari.creative.engines import media

N_FFT = 2048
HOP = 512
MAX_BEATS = 400
ANALYSIS_VERSION = "audio-analysis/v1"


def decode_pcm(data: bytes, sample_rate: int) -> np.ndarray:
    """어떤 컨테이너든 ffmpeg로 모노 16비트 PCM(sample_rate)으로 풀어 numpy 배열로 돌려준다."""
    proc = subprocess.run(
        [
            media.ffmpeg_path(),
            "-hide_banner",
            "-nostdin",
            "-v",
            "error",
            "-i",
            "pipe:0",
            "-vn",
            "-ac",
            "1",
            "-ar",
            str(sample_rate),
            "-f",
            "s16le",
            "pipe:1",
        ],
        input=data,
        capture_output=True,
        timeout=300,
        check=False,
    )
    if proc.returncode != 0 or len(proc.stdout) < 2:
        raise ValueError(
            "오디오를 디코드할 수 없습니다: "
            + proc.stderr.decode("utf-8", "replace")[-200:]
        )
    return np.frombuffer(proc.stdout[: len(proc.stdout) // 2 * 2], dtype=np.int16)


def _flux(samples: np.ndarray) -> np.ndarray:
    x = samples.astype(np.float32) / 32768.0
    if len(x) < N_FFT:
        x = np.pad(x, (0, N_FFT - len(x)))
    window = np.hanning(N_FFT).astype(np.float32)
    count = 1 + (len(x) - N_FFT) // HOP
    frames = np.lib.stride_tricks.sliding_window_view(x, N_FFT)[::HOP][:count] * window
    mag = np.abs(np.fft.rfft(frames, axis=1))
    mag = np.log1p(10 * mag)
    flux = np.maximum(mag[1:] - mag[:-1], 0).sum(axis=1)
    return np.concatenate([[0.0], flux]).astype(np.float32)


def _center(index: int) -> int:
    """STFT 프레임 인덱스를 프레임 중심 샘플로 바꾼다(창 시작이 아니라 중심이 소리 시점에 가깝다)."""
    return index * HOP + N_FFT // 2


def _pick_onsets(flux: np.ndarray, sample_rate: int) -> list[int]:
    if len(flux) < 4 or float(flux.max()) <= 0:
        return []
    env = flux / (flux.max() + 1e-9)
    width = max(3, int(0.5 * sample_rate / HOP))
    kernel = np.ones(width, dtype=np.float32) / width
    local = np.convolve(env, kernel, mode="same")
    threshold = local + 0.12
    min_gap = max(1, int(0.12 * sample_rate / HOP))
    picks: list[int] = []
    for i in range(1, len(env) - 1):
        if env[i] >= env[i - 1] and env[i] > env[i + 1] and env[i] > threshold[i]:
            if not picks or i - picks[-1] >= min_gap:
                picks.append(i)
            elif env[i] > env[picks[-1]]:
                picks[-1] = i
    return picks


def _tempo(flux: np.ndarray, sample_rate: int) -> tuple[float | None, float, int]:
    """자기상관으로 60~180 BPM 범위의 템포와 격자 위상을 고른다. (bpm, confidence 0~1, 위상 hop 인덱스)"""
    if len(flux) < int(2.5 * sample_rate / HOP):
        return None, 0.0, 0
    env = flux - flux.mean()
    corr = np.correlate(env, env, mode="full")[len(env) - 1 :]
    if corr[0] <= 0:
        return None, 0.0, 0
    corr = corr / corr[0]
    lo = int(60 / 180 * sample_rate / HOP)
    hi = int(60 / 60 * sample_rate / HOP)
    window = corr[lo : hi + 1]
    if len(window) < 3:
        return None, 0.0, 0
    lag = int(np.argmax(window)) + lo
    bpm = 60 * sample_rate / HOP / lag
    confidence = float(
        np.clip(
            (window.max() - np.median(window)) / (1 - np.median(window) + 1e-9), 0, 1
        )
    )
    scores = [float(env[phase::lag].sum()) for phase in range(lag)]
    return float(bpm), confidence, int(np.argmax(scores))


def _sections(samples: np.ndarray, sample_rate: int) -> list[dict[str, Any]]:
    """RMS 에너지가 눈에 띄게 바뀌는 지점으로 나눈 거친 구간. 의미 라벨을 붙이지 않는다."""
    window = sample_rate * 2
    if len(samples) < window * 2:
        return [
            {
                "start_sample": 0,
                "end_sample": len(samples),
                "label": "section-1",
                "confidence": 0.2,
                "method": "single",
            }
        ]
    x = samples.astype(np.float32) / 32768.0
    blocks = len(x) // window
    rms = np.array(
        [
            np.sqrt(np.mean(x[i * window : (i + 1) * window] ** 2)) + 1e-6
            for i in range(blocks)
        ]
    )
    cuts = [0]
    for i in range(1, blocks):
        ratio = rms[i] / rms[i - 1]
        if (ratio > 1.6 or ratio < 1 / 1.6) and i * window - cuts[-1] >= window * 2:
            cuts.append(i * window)
    cuts.append(len(samples))
    return [
        {
            "start_sample": int(a),
            "end_sample": int(b),
            "label": f"section-{n + 1}",
            "confidence": 0.4,
            "method": "rms-novelty",
        }
        for n, (a, b) in enumerate(pairwise(cuts))
    ]


def analyze_audio(
    data: bytes, *, sample_rate: int, fps_num: int, fps_den: int
) -> dict[str, Any]:
    samples = decode_pcm(data, sample_rate)
    total = len(samples)

    def frame(sample: int) -> int:
        return TimelineRevision.sample_to_frame(
            sample, sample_rate=sample_rate, fps_num=fps_num, fps_den=fps_den
        )

    flux = _flux(samples)
    env = flux / (flux.max() + 1e-9) if float(flux.max()) > 0 else flux
    onsets = [
        {
            "sample": _center(i),
            "frame": frame(_center(i)),
            "strength": round(float(env[i]), 4),
            "method": "spectral-flux",
        }
        for i in _pick_onsets(flux, sample_rate)
    ]
    bpm, confidence, phase = _tempo(flux, sample_rate)
    beats: list[dict[str, Any]] = []
    if bpm is not None and confidence >= 0.25:
        lag = round(60 * sample_rate / HOP / bpm)
        for i in range(phase, len(flux), lag):
            sample = _center(i)
            beats.append(
                {
                    "sample": sample,
                    "frame": frame(sample),
                    "confidence": round(confidence, 3),
                    "method": "autocorr-grid",
                }
            )
            if len(beats) >= MAX_BEATS:
                break
    sections = [
        {**s, "start_f": frame(s["start_sample"]), "end_f": frame(s["end_sample"])}
        for s in _sections(samples, sample_rate)
    ]
    peak = int(np.max(np.abs(samples.astype(np.int32)))) if total else 0
    return {
        "schema": "ttakkari.audio-analysis.v1",
        "analysis_version": ANALYSIS_VERSION,
        "sample_rate": sample_rate,
        "total_samples": total,
        "duration_frames": max(1, frame(total)),
        "peak_amplitude": round(peak / 32768.0, 4),
        "onsets": onsets,
        "tempo": {
            "bpm": None if bpm is None else round(bpm, 2),
            "confidence": round(confidence, 3),
            "method": "autocorr-60-180bpm",
        },
        "beats": beats,
        "sections": sections,
        "validated": False,
        "note": "자동 분석 결과다. 비트 정확도와 구간 의미는 사람 청취로 검증되지 않았고 감독이 수동 보정할 수 있다.",
    }


def timeline_cues(
    analysis: dict[str, Any],
    speech: dict[str, Any] | None,
    *,
    fps_num: int,
    fps_den: int,
    max_beats: int = 64,
) -> list[dict[str, Any]]:
    """분석과 발화 timing을 타임라인 cue로 합친다. 원시 샘플·방법·신뢰도를 그대로 둔다."""
    rate = analysis["sample_rate"]
    cues: list[dict[str, Any]] = [
        {
            "kind": "beat",
            "frame": b["frame"],
            "sample": b["sample"],
            "label": "beat",
            "confidence": b["confidence"],
            "method": b["method"],
        }
        for b in analysis["beats"][:max_beats]
    ]
    if speech:
        if speech["sample_rate"] != rate:
            raise ValueError("발화 timing과 분석의 샘플레이트가 다릅니다.")
        for line in speech["lines"]:
            start = TimelineRevision.sample_to_frame(
                line["start_sample"], sample_rate=rate, fps_num=fps_num, fps_den=fps_den
            )
            end = TimelineRevision.sample_to_frame(
                line["end_sample"], sample_rate=rate, fps_num=fps_num, fps_den=fps_den
            )
            cues.append(
                {
                    "kind": "speech_start",
                    "frame": start,
                    "sample": line["start_sample"],
                    "end_frame": end,
                    "end_sample": line["end_sample"],
                    "label": line["text"][:60],
                    "confidence": speech.get("confidence", 1.0),
                    "method": speech.get("method", "tts-chunk-concatenation"),
                }
            )
    return sorted(cues, key=lambda c: (c["frame"], c["kind"]))


def rescale_timing(timing: dict[str, Any], target_rate: int) -> dict[str, Any]:
    """발화 timing의 샘플 시점을 정수 연산으로 다른 샘플레이트로 옮긴다(반올림은 반을 올림). 원래 값은 `source_*`에 남긴다."""
    source = timing["sample_rate"]
    if source == target_rate:
        return timing

    def move(sample: int) -> int:
        return (2 * sample * target_rate + source) // (2 * source)

    return {
        **timing,
        "sample_rate": target_rate,
        "total_samples": move(timing["total_samples"]),
        "source_sample_rate": source,
        "lines": [
            {
                **line,
                "start_sample": move(line["start_sample"]),
                "end_sample": move(line["end_sample"]),
                "source_start_sample": line["start_sample"],
                "source_end_sample": line["end_sample"],
            }
            for line in timing["lines"]
        ],
    }
