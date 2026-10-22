"""창작 스튜디오 엔진 시험 공용 도구. 실제 ffmpeg(imageio-ffmpeg 번들, CPU)와 mock 어댑터만 쓴다."""

from __future__ import annotations

import io
import json
import wave
from pathlib import Path

import numpy as np

from ttakkari.creative.director import DirectorStore
from ttakkari.creative.engines.base import ProfileSpec
from ttakkari.creative.engines.ffmpeg_engine import FfmpegAdapter
from ttakkari.creative.engines.mock import MockEngineAdapter
from ttakkari.creative.engines.registry import EngineRegistry
from ttakkari.creative.pipeline import ProductionPipeline


def click_track(
    *, seconds: int = 4, bpm: int = 120, sample_rate: int = 48_000
) -> bytes:
    """박자마다 짧은 클릭이 있는 WAV. 비트 분석의 합성 기준이다(실제 음악 정확도 근거가 아님)."""
    signal = np.zeros(sample_rate * seconds)
    step = 60 / bpm
    for k in range(int(seconds / step)):
        start = int(k * step * sample_rate)
        n = int(0.04 * sample_rate)
        signal[start : start + n] += np.sin(
            2 * np.pi * 1000 * np.arange(n) / sample_rate
        ) * np.exp(-np.arange(n) / (0.01 * sample_rate))
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as target:
        target.setnchannels(1)
        target.setsampwidth(2)
        target.setframerate(sample_rate)
        target.writeframes((signal * 0.8 * 32767).astype(np.int16).tobytes())
    return buffer.getvalue()


def make_pipeline(
    tmp_path: Path,
    *adapters,
    allow_mock: bool = True,
    measure: bool = True,
    fresh_seconds: float = 3600,
    clock=None,
):
    store = DirectorStore(tmp_path / "productions")
    kwargs = {"fresh_seconds": fresh_seconds, "allow_mock_selection": allow_mock}
    if clock is not None:
        kwargs["clock"] = clock
    registry = EngineRegistry(
        tmp_path / "engine-registry.json", list(adapters), **kwargs
    )
    if measure:
        for adapter in adapters:
            registry.measure(adapter.engine_id)
            registry.set_policy(adapter.engine_id, "auto_eligible", "시험")
    return ProductionPipeline(store, registry), store, registry


def real_ffmpeg_pipeline(tmp_path: Path):
    """실제 ffmpeg만 측정·smoke·승격한 파이프라인(mock 선택 허용 없음)."""
    adapter = FfmpegAdapter()
    pipeline, store, registry = make_pipeline(
        tmp_path, adapter, allow_mock=False, measure=False
    )
    registry.run_smoke("ffmpeg")
    registry.set_policy("ffmpeg", "auto_eligible", "실제 smoke 통과 후 시험 승격")
    return pipeline, store, registry


def new_production(
    store: DirectorStore,
    shots: int = 3,
    frames: int = 24,
    *,
    fps: int = 24,
    cues: list[dict] | None = None,
    size=(320, 180),
):
    """타임라인과 바이블을 시험용 값으로 맞춘 제작. 샷 계획은 호출 쪽이 정한다."""
    state = store.create_production("시험 제작")
    pid = state["manifest"]["production_id"]
    store.update_bible(
        pid,
        expected_revision=1,
        values={
            "palette": ["#101418", "#f4f4ed", "#00f0b5"],
            "composition": {"size": list(size)},
        },
    )
    timeline = store.update_timeline(
        pid,
        expected_revision=1,
        values={
            "fps_num": fps,
            "fps_den": 1,
            "duration_frames": shots * frames,
            "sample_rate": 48_000,
            "shots": [
                {
                    "shot_id": f"shot-{i + 1}",
                    "start_f": i * frames,
                    "end_f": (i + 1) * frames,
                }
                for i in range(shots)
            ],
            "cues": cues or [],
        },
    )
    return pid, timeline


def ffmpeg_plans(
    shots: int = 3,
    frames: int = 24,
    profile: str = "test-pattern",
    params: dict | None = None,
    engine: str = "ffmpeg",
):
    return [
        {
            "shot_id": f"shot-{i + 1}",
            "start_f": i * frames,
            "end_f": (i + 1) * frames,
            "intent": f"장면 {i + 1}",
            "engine": engine,
            "profile": profile,
            "params": params or {},
            "seed": i + 1,
            "selection": "auto",
            "reason": "시험",
        }
        for i in range(shots)
    ]


class FakeComfyController:
    """ComfyManagedController 대역: prepare/start/get/cancel/reconcile만 흉내 낸다. 실제 생성이 아니다."""

    def __init__(
        self, tmp_path: Path, *, outcome: str = "succeeded", polls_before_done: int = 1
    ):
        self.tmp_path = tmp_path
        self.outcome = outcome
        self.polls = polls_before_done
        self.prepared: list[dict] = []
        self.started: list[str] = []
        self.cancelled: list[str] = []
        self.reconciled: list[str] = []
        self.jobs: dict[str, dict] = {}
        self.get_calls: dict[str, int] = {}
        self.input_receipt = (
            "ok"  # wrong-sha | graph-mismatch로 receipt 불일치를 흉내 낸다
        )

    def prepare(self, profile_id, *, prompt, seed, owner="owner", image=None, **extra):
        task_id = f"{len(self.prepared) + 1:032x}"
        self.prepared.append(
            {
                "task_id": task_id,
                "profile": profile_id,
                "prompt": prompt,
                "seed": seed,
                "image": image,
            }
        )
        self.jobs[task_id] = {
            "task_id": task_id,
            "status": "starting",
            "profile": profile_id,
            "prompt": prompt,
            "seed": seed,
            "image": None if image is None else image.public(),
        }
        return dict(self.jobs[task_id])

    def start(self, task_id):
        self.started.append(task_id)
        return self.jobs[task_id]

    def get(self, task_id):
        if task_id not in self.jobs:
            raise FileNotFoundError("managed Comfy task not found")
        self.get_calls[task_id] = self.get_calls.get(task_id, 0) + 1
        job = self.jobs[task_id]
        if (
            job["status"] in {"starting", "running"}
            and self.get_calls[task_id] > self.polls
            and task_id in self.started
        ):
            self._finish(job)
        return dict(job)

    def _finish(self, job):
        data = b"COMFYMOCK" + job["task_id"].encode()
        path = self.tmp_path / f"{job['task_id']}.mp4"
        path.write_bytes(data)
        import hashlib

        sha = hashlib.sha256(data).hexdigest()
        if self.outcome == "succeeded":
            job["status"] = "succeeded"
            job["receipt"] = {
                "graph_hash_consistent": True,
                "seed_consistent": True,
                "external_prompt_id": "prompt-1",
                "resolved_graph_sha256": "g" * 64,
                "seed": job["seed"],
                "model_hashes": {"m": "h"},
                "runtime": {"engine": "comfyui"},
                "lease": {"owner_task": job["task_id"]},
                "workflow_revision_hash": "a603a63d6f3a",
                **self._input_section(job),
                "peak_rss_gb": None,
                "outputs": [
                    {
                        "name": "clip.mp4",
                        "sha256": sha,
                        "bytes": len(data),
                        "media_type": "video/mp4",
                        "source_path": str(path),
                        "intermediate": False,
                    }
                ],
            }
        elif self.outcome == "inconsistent":
            job["status"] = "succeeded"
            job["receipt"] = {
                "graph_hash_consistent": False,
                "seed_consistent": True,
                "outputs": [],
            }
        elif self.outcome == "empty":
            job["status"] = "succeeded"
            job["receipt"] = {
                "graph_hash_consistent": True,
                "seed_consistent": True,
                "outputs": [],
            }
        elif self.outcome == "needs_reconcile":
            job["status"] = "needs_reconcile"
        else:
            job["status"] = self.outcome
            job["error"] = "fake"

    def _input_section(self, job):
        image = job.get("image")
        if image is None:
            return {}
        recorded = {**image, "graph_load_image": f"ttk/{image['upload_name']}"}
        if self.input_receipt == "wrong-sha":
            recorded["sha256"] = "0" * 64
        recorded["graph_consistent"] = self.input_receipt != "graph-mismatch"
        return {"input_image": recorded, "profile_verification": "prepared-unverified"}

    def cancel(self, task_id, *, owner_task=None):
        self.cancelled.append(task_id)
        self.jobs[task_id]["status"] = "cancelled"
        return self.jobs[task_id]

    def reconcile(self, task_id, *, owner_task=None):
        self.reconciled.append(task_id)
        self._finish_after_reconcile(self.jobs[task_id])
        return dict(self.jobs[task_id])

    def _finish_after_reconcile(self, job):
        self.outcome = "succeeded"
        self._finish(job)


def mock_profile_exact() -> ProfileSpec:
    return MockEngineAdapter().profiles["clip"]


def dumps(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


class FakeTtsProvider:
    id = "fake"

    class _Settings:
        local_tts_python = None

    settings = _Settings()

    def __init__(self, tmp_path, *, samples=(4410, 8820), empty_index=None):
        self.tmp_path = tmp_path
        self.samples = samples
        self.empty_index = empty_index

    def availability(self):
        from ttakkari.tts.providers import ProviderAvailability, ProviderState

        return ProviderAvailability(ProviderState.ready, "fake")

    def model_revision(self):
        return "fake-1"

    async def synthesize(self, chunks, *, voice, speed, lang, out_dir):
        import wave

        from ttakkari.tts.providers import TtsChunkResult, TtsResult

        results = []
        for index, _text in enumerate(chunks):
            path = f"{out_dir}/c{index}.wav"
            count = (
                0
                if index == self.empty_index
                else self.samples[index % len(self.samples)]
            )
            with wave.open(path, "wb") as target:
                target.setnchannels(1)
                target.setsampwidth(2)
                target.setframerate(44100)
                target.writeframes(b"\x01\x00" * count)
            results.append(
                TtsChunkResult(
                    index=index,
                    path=path,
                    duration_seconds=count / 44100,
                    sample_rate=44100,
                    bytes=count * 2,
                )
            )
        return TtsResult(chunks=results, metrics={"fake": True})


def png_bytes(
    width: int = 128, height: int = 96, color=(120, 60, 200), fmt: str = "PNG"
) -> bytes:
    """작은 시험용 이미지. 실제 FLUX 기준 이미지가 아니다."""
    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGB", (width, height), color).save(buffer, fmt)
    return buffer.getvalue()
