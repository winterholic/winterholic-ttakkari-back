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

from ttakkari.creative.service import now, uid


def illustration(path: Path, prompt: str, variant: int = 0):
    rng = random.Random(hashlib.sha256(f"{prompt}:{variant}".encode()).hexdigest())
    palettes = [
        ("#111d3a", "#334877", "#72c5c3"),
        ("#17384b", "#46707a", "#97e2cf"),
        ("#66576e", "#c69587", "#f5d299"),
    ]
    bg, building, glow = palettes[variant % 3]
    im = Image.new("RGB", (1280, 720), bg)
    d = ImageDraw.Draw(im)
    for _ in range(100):
        x, y = rng.randrange(1280), rng.randrange(400)
        d.ellipse((x, y, x + 2, y + 2), fill=glow)
    d.ellipse((780, 72, 910, 202), fill=glow)
    for x in range(-20, 1300, 64):
        y = rng.randrange(330, 580)
        d.rounded_rectangle((x, y, x + 56, 720), 4, fill=building)
        for yy in range(y + 14, 690, 24):
            for xx in range(x + 10, x + 48, 16):
                if rng.random() > 0.45:
                    d.rectangle((xx, yy, xx + 5, yy + 9), fill=glow)
    # Identical original robot geometry across distinct sets.
    x = [850, 580, 330][variant % 3]
    d.rectangle((0, 657, 1280, 720), fill="#13202e")
    for radius in (110, 160, 210):
        d.arc(
            (x - radius, 365 - radius, x + radius, 365 + radius),
            210,
            330,
            fill=glow,
            width=3,
        )
    d.rounded_rectangle((x - 42, 525, x + 42, 630), 22, fill="#a9ded2")
    d.rounded_rectangle((x - 57, 433, x + 57, 535), 34, fill="#c8eee3")
    d.rounded_rectangle((x - 39, 458, x + 39, 503), 20, fill="#152b3b")
    d.ellipse((x - 13, 468, x + 13, 494), fill="#ffd68a")
    d.line((x, 433, x, 409), fill="#c8eee3", width=6)
    d.ellipse((x - 6, 401, x + 6, 413), fill="#ffd68a")
    for delta in (-27, 27):
        d.rounded_rectangle((x + delta - 9, 621, x + delta + 9, 656), 7, fill="#a9ded2")
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
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "illustration.png"
        illustration(path, scene["prompt"], index + len(p["generation_jobs"]))
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
        for i, scene in enumerate(data["scenes"]):
            if not scene["caption"]:
                scene["caption"] = [
                    "작은 신호가 밤을 깨우면",
                    "낯선 도시도 리듬이 되고",
                    "우리는 함께 새벽으로",
                ][i % 3]
        data["revision"] += 1
    for scene in p["scenes"]:
        if not scene["asset_id"]:
            generate_illustration(studio, project_id, scene["id"])
    return studio.get(project_id)
