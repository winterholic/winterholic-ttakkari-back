"""Original algorithmic music and code-native illustration. No AI claim or downloads."""

from __future__ import annotations

import hashlib
import random
import tempfile
import time
import wave
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw

from ttakkari.creative.render import dimensions
from ttakkari.creative.service import now, uid


def illustration(
    path: Path,
    prompt: str,
    variant: int = 0,
    width: int = 1280,
    height: int = 720,
):
    rng = random.Random(hashlib.sha256(f"{prompt}:{variant}".encode()).hexdigest())
    palettes = [
        ("#111d3a", "#334877", "#72c5c3"),
        ("#17384b", "#46707a", "#97e2cf"),
        ("#66576e", "#c69587", "#f5d299"),
        ("#1e1b4b", "#3730a3", "#a5b4fc"),
        ("#0f172a", "#1e293b", "#38bdf8"),
    ]
    bg, building, glow = palettes[variant % len(palettes)]
    im = Image.new("RGB", (width, height), bg)
    d = ImageDraw.Draw(im)
    for _ in range(120):
        x, y = rng.randrange(width), rng.randrange(round(height * 0.6))
        d.ellipse((x, y, x + 2, y + 2), fill=glow)
    moon_x, moon_y = round(width * 0.7), round(height * 0.15)
    moon_r = round(min(width, height) * 0.08)
    d.ellipse(
        (moon_x - moon_r, moon_y - moon_r, moon_x + moon_r, moon_y + moon_r), fill=glow
    )
    b_step = max(30, round(width / 16))
    for x in range(-20, width + 40, b_step):
        y = rng.randrange(round(height * 0.45), round(height * 0.8))
        d.rounded_rectangle((x, y, x + b_step - 8, height), 4, fill=building)
        for yy in range(y + 14, height - 20, 24):
            for xx in range(x + 8, x + b_step - 16, 16):
                if rng.random() > 0.45:
                    d.rectangle((xx, yy, xx + 5, yy + 9), fill=glow)
    # Identical original robot geometry scaled to canvas.
    x_pos = [round(width * 0.7), round(width * 0.5), round(width * 0.3)][variant % 3]
    ground_y = round(height * 0.9)
    d.rectangle((0, ground_y, width, height), fill="#13202e")
    scale_fac = min(width, height) / 720.0
    for radius in (
        round(110 * scale_fac),
        round(160 * scale_fac),
        round(210 * scale_fac),
    ):
        d.arc(
            (
                x_pos - radius,
                round(365 * scale_fac) - radius,
                x_pos + radius,
                round(365 * scale_fac) + radius,
            ),
            210,
            330,
            fill=glow,
            width=max(2, round(3 * scale_fac)),
        )
    body_y = round(height * 0.72)
    d.rounded_rectangle(
        (
            x_pos - round(42 * scale_fac),
            body_y,
            x_pos + round(42 * scale_fac),
            body_y + round(105 * scale_fac),
        ),
        round(22 * scale_fac),
        fill="#a9ded2",
    )
    head_y = body_y - round(92 * scale_fac)
    d.rounded_rectangle(
        (
            x_pos - round(57 * scale_fac),
            head_y,
            x_pos + round(57 * scale_fac),
            body_y + round(10 * scale_fac),
        ),
        round(34 * scale_fac),
        fill="#c8eee3",
    )
    d.rounded_rectangle(
        (
            x_pos - round(39 * scale_fac),
            head_y + round(25 * scale_fac),
            x_pos + round(39 * scale_fac),
            head_y + round(70 * scale_fac),
        ),
        round(20 * scale_fac),
        fill="#152b3b",
    )
    d.ellipse(
        (
            x_pos - round(13 * scale_fac),
            head_y + round(35 * scale_fac),
            x_pos + round(13 * scale_fac),
            head_y + round(61 * scale_fac),
        ),
        fill="#ffd68a",
    )
    d.line(
        (x_pos, head_y, x_pos, head_y - round(24 * scale_fac)),
        fill="#c8eee3",
        width=max(3, round(6 * scale_fac)),
    )
    d.ellipse(
        (
            x_pos - round(6 * scale_fac),
            head_y - round(32 * scale_fac),
            x_pos + round(6 * scale_fac),
            head_y - round(20 * scale_fac),
        ),
        fill="#ffd68a",
    )
    im.save(path)


def music(path: Path, duration: float):
    sr = 24000
    audio = np.zeros(round(sr * duration), dtype=np.float64)
    rng = np.random.default_rng(712)

    def note(start, length, frequency, volume, voice="soft"):
        offset = round(start * sr)
        count = min(round(length * sr), len(audio) - offset)
        if count <= 0:
            return
        t = np.arange(count) / sr
        env = np.minimum(t / 0.015, 1) * np.minimum((count / sr - t) / 0.08, 1)
        if voice == "kick":
            v = np.sin(2 * np.pi * (45 * t + 7 * (1 - np.exp(-25 * t)))) * np.exp(
                -12 * t
            )
        elif voice == "hat":
            v = rng.uniform(-1, 1, count) * np.exp(-60 * t)
        else:
            v = (
                np.sin(2 * np.pi * frequency * t)
                + 0.20 * np.sin(4 * np.pi * frequency * t)
            ) * np.exp(-1.7 * t)
        audio[offset : offset + count] += v * env * volume

    beat = 0.5  # 120 BPM; four bars follow a four-chord progression.
    chords = [(48, 52, 55), (43, 47, 50), (45, 48, 52), (41, 45, 48)]
    motif = [0, 2, 1, 2, 0, 1, 2, 1]
    for b in range(int(duration / beat)):
        c = chords[(b // 8) % 4]
        start = b * beat
        if b % 2 == 0:
            note(start, 0.4, 0, 0.30, "kick")
        note(start + 0.25, 0.1, 0, 0.065, "hat")
        note(start, 0.45, 440 * 2 ** ((c[0] - 69) / 12), 0.13)
        midi = c[motif[b % 8]] + 24
        note(start, 0.4, 440 * 2 ** ((midi - 69) / 12), 0.105)
        if b % 4 == 0:
            for pitch in c:
                note(start, 1.8, 440 * 2 ** ((pitch + 12 - 69) / 12), 0.045)
    fade = np.minimum(np.arange(len(audio)) / (sr * 0.2), 1) * np.minimum(
        np.arange(len(audio))[::-1] / (sr * 0.7), 1
    )
    audio = np.clip(audio * fade, -1, 1)
    with wave.open(str(path), "wb") as f:
        f.setnchannels(1)
        f.setsampwidth(2)
        f.setframerate(sr)
        f.writeframes((audio * 26000).astype("<i2").tobytes())


def generate_illustration(studio, project_id, scene_id):
    p = studio.get(project_id)
    index = next((i for i, s in enumerate(p["scenes"]) if s["id"] == scene_id), None)
    if index is None:
        raise FileNotFoundError("장면이 없습니다.")
    job_id, start = uid(), time.monotonic()
    scene = p["scenes"][index]
    width, height = dimensions(p.get("aspect", "16:9"))
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "illustration.png"
        illustration(
            path,
            scene["prompt"],
            index + len(p["generation_jobs"]),
            width=width,
            height=height,
        )
        asset = studio.add_asset(
            project_id,
            path,
            name=f"장면 {index + 1} 그래픽",
            kind="image",
            provenance="procedural-illustration-v1 (not AI)",
            generation_job_id=job_id,
        )
    with studio.transaction(project_id) as data:
        data["generation_jobs"].append(
            {
                "id": job_id,
                "model": "procedural-illustration-v1",
                "status": "succeeded",
                "prompt": scene["prompt"],
                "scene_id": scene_id,
                "reference_assets": [],
                "settings": {"variant": index + len(p["generation_jobs"])},
                "elapsed_seconds": round(time.monotonic() - start, 3),
                "output_asset_id": asset["id"],
                "created_at": now(),
                "error": None,
            }
        )
        # Prompt edits that raced this generation do not get overwritten.
        target = next((s for s in data["scenes"] if s["id"] == scene_id), None)
        if target and target["prompt"] == scene["prompt"]:
            target["asset_id"] = asset["id"]
            data["revision"] += 1
    return studio.get(project_id)


def make_demo(studio, project_id):
    p = studio.get(project_id)
    start, job_id = time.monotonic(), uid()
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "little-signal.wav"
        music(path, p["duration"])
        audio = studio.add_asset(
            project_id,
            path,
            name="작은 신호 · 120 BPM 합성 반주",
            kind="audio",
            provenance="Original procedural composition; not a neural music model",
            generation_job_id=job_id,
            metadata={
                "duration": p["duration"],
                "bpm": 120,
                "beats": [i * 0.5 for i in range(int(p["duration"] * 2))],
                "vocal": False,
            },
        )
    with studio.transaction(project_id) as data:
        data["generation_jobs"].append(
            {
                "id": job_id,
                "model": "procedural-synth-v1",
                "status": "succeeded",
                "prompt": "original 120 BPM instrumental",
                "settings": {"duration": p["duration"], "seed": 712},
                "reference_assets": [],
                "elapsed_seconds": round(time.monotonic() - start, 3),
                "output_asset_id": audio["id"],
                "created_at": now(),
                "error": None,
            }
        )
        data["music_asset_id"] = audio["id"]
        fmt_id = data.get("content_type", "music_video")
        captions_map = {
            "shorts_9_16": [
                "3초 만에 끝내는 영상 조립",
                "포맷과 템플릿 부품 결합",
                "규격 위반은 코드가 원천 차단",
                "2분 이상 장편도 분할 렌더",
                "따까리 AI Studio 지금 확인",
            ],
            "tech_explainer": [
                "Creative Studio 아키텍처\n3층 정형화와 분할 렌더 파이프라인",
                "Format: 화면비·길이·시퀀스 규칙 강제",
                "Template & Tool: 재사용 장면과 렌더 프리미티브",
                "장면 단위 분할 캐시로 OOM 및 렌더 병목 해소",
                "ADR 0065에 설계 영속화",
            ],
            "narration_story": [
                "새벽의 스튜디오, 작은 신호의 시작",
                "수많은 모듈이 서로의 언어로 연결되고",
                "분할된 장면들이 차례로 렌더 큐를 통과한다",
                "무거운 모델을 매번 다시 돌리지 않고",
                "캐시된 프레임들이 퍼즐처럼 이어붙는다",
                "단 하나의 수정은 오직 그 장면만을 다시 그린다",
                "시간과 메모리의 한계를 넘어선 파이프라인",
                "장편의 호흡으로 완성되는 따까리의 콘텐츠",
                "지속 가능한 로컬 스튜디오의 미래",
            ],
            "music_video": [
                "작은 신호가 밤을 깨우면",
                "낯선 도시도 리듬이 되고",
                "우리는 함께 새벽으로",
                "별빛 너머로 번지는 멜로디",
            ],
        }
        presets = captions_map.get(fmt_id, captions_map["music_video"])
        for i, scene in enumerate(data["scenes"]):
            if not scene["caption"]:
                scene["caption"] = presets[i % len(presets)]
        data["revision"] += 1
    for scene in p["scenes"]:
        if not scene["asset_id"]:
            generate_illustration(studio, project_id, scene["id"])
    return studio.get(project_id)
