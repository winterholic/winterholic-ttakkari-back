"""v2 asset adapter for existing managed Comfy, Supertonic, and local ACE-Step paths."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import subprocess
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

from PIL import Image

from ttakkari.config import get_settings
from ttakkari.creative.director import default_director_store
from ttakkari.creative.render import ffmpeg, inspect_media
from ttakkari.host_resource import ResourceLeaseManager

_logger = logging.getLogger(__name__)

ASSET_LAB_ROOT = Path(
    os.environ.get(
        "TTAKKARI_CREATIVE_LAB_ROOT",
        str(Path.home() / "development/projects/ttakkari-studio-lab"),
    )
)
ACESTEP_ROOT = ASSET_LAB_ROOT / "audio/refs/ACE-Step-1.5"
ACESTEP_JOB_RUNNER = ASSET_LAB_ROOT / "audio/run_acestep_job.py"
ACESTEP_PYTHON = ACESTEP_ROOT / ".venv/bin/python"
ACESTEP_TIMEOUT_SECONDS = 1800
LEASE_TTL_SECONDS = 300
# 앞 엔진은 성공을 기록한 뒤 런타임을 멈추고 lease 를 해제한다(Comfy 정지는 수 초에서 수십 초). 같은 작업의 다음 엔진은
# 그 인계 구간을 기다려야 하며, 기다려도 풀리지 않는 외부 점유만 실패로 올린다.
LEASE_HANDOFF_WAIT_SECONDS = 180
SCENE_NEGATIVE_PROMPT = (
    "text, letters, words, captions, subtitles, typography, watermark, watermarks, "
    "logo, logos, signage, signs, writing, symbols"
)


class ManagedAssetGenerator:
    """R21 scene assets via the existing managed Comfy, local TTS and ACE-Step paths."""

    engine_id = "creative-v2.managed-comfy+supertonic+acestep"
    version = "wan22-5b-draft+local-supertonic+acestep-v15-turbo+scene-brief-v3"

    def __init__(self, *, controller=None, store=None, clock=time.monotonic, loop=None):
        self._controller = controller
        self._store = store
        self._clock = clock
        # generate()는 worker thread 에서 돈다. TTS 실행 예산(generation.work, DB 풀)은 서버 이벤트 루프에 묶여 있어
        # 새 루프(asyncio.run)에서 쓰면 "attached to a different loop" 로 실패하므로 그 루프에서 실행한다.
        self._loop = loop

    @property
    def controller(self):
        if self._controller is None:
            from ttakkari.managed_comfy import default_controller

            self._controller = default_controller(get_settings().data_dir)
        return self._controller

    @property
    def store(self):
        if self._store is None:
            self._store = default_director_store()
        return self._store

    def supported_recipes(self) -> set[str]:
        # R21 is the currently dispatchable image slideshow baseline. Other recipes
        # need generator semantics (character, location, product) that this bridge
        # does not claim to satisfy.
        return {"R21"}

    def blockers(self, plan: dict[str, Any]) -> list[str]:
        reasons: list[str] = []
        try:
            from ttakkari import managed_comfy
            from ttakkari.managed_comfy_receipt import model_entry

            profile = managed_comfy.PROFILES["wan22-5b-draft"]
            models_root = managed_comfy.COMFY_ROOT / "ComfyUI" / "models"
            models = [model_entry(name, models_root, hash_limit=0) for name in profile.models]
            missing_models = [model["name"] for model in models if not model.get("found")]
        except Exception as exc:  # noqa: BLE001 - probe errors are blockers, never a fallback
            reasons.append(f"managed Comfy 사전 점검 실패: {type(exc).__name__}: {exc}")
        else:
            if missing_models:
                reasons.append(
                    "managed Comfy 필수 모델 파일이 없습니다: " + ", ".join(missing_models)
                )
        torch_probe = self._probe_comfy_torch()
        if torch_probe.get("error"):
            reasons.append(f"Comfy PyTorch 장치 사전 점검 실패: {torch_probe['error']}")
        elif not (torch_probe.get("mps_available") or torch_probe.get("cuda_available")):
            reasons.append(
                "Comfy PyTorch에서 CUDA 또는 MPS 장치를 사용할 수 없습니다. "
                "현재 실행 환경에서 생성 작업을 시작할 수 없습니다."
            )
        try:
            from ttakkari.tts.service import get_provider

            provider = get_provider()
            availability = provider.availability()
        except Exception as exc:  # noqa: BLE001
            reasons.append(f"Supertonic 음성 사전 점검 실패: {type(exc).__name__}: {exc}")
        else:
            if not availability.ready:
                reasons.append(
                    "Supertonic 음성 모델을 사용할 수 없습니다: " + str(availability.reason)
                )
        required = (
            ACESTEP_JOB_RUNNER,
            ACESTEP_PYTHON,
            ACESTEP_ROOT / "checkpoints/acestep-v15-turbo/model.safetensors",
        )
        missing = [str(path) for path in required if not path.is_file()]
        if missing:
            reasons.append("ACE-Step 로컬 음악 생성 파일이 없습니다: " + ", ".join(missing))
        if plan.get("aspect") != "16:9":
            reasons.append("현재 자동 생성 경로는 16:9 계획만 지원합니다.")
        missing_narration = [
            scene["scene_id"]
            for scene in plan["scenes"]
            if not str(scene.get("narration") or "").strip()
        ]
        if missing_narration:
            reasons.append("장면별 음성 대본이 없습니다: " + ", ".join(missing_narration))
        for scene in plan["scenes"]:
            try:
                self._prompt(plan, scene)
            except ValueError as exc:
                reasons.append(str(exc))
        return reasons

    @staticmethod
    def _probe_comfy_torch() -> dict[str, Any]:
        """Check the exact Python runtime Comfy will launch, before reserving a task."""
        from ttakkari import managed_comfy

        python = managed_comfy.COMFY_ROOT / "venv/bin/python"
        code = (
            "import json, torch; "
            "print(json.dumps({'cuda_available': torch.cuda.is_available(), "
            "'mps_available': torch.backends.mps.is_available()}))"
        )
        try:
            result = subprocess.run(
                [str(python), "-c", code],
                capture_output=True,
                text=True,
                check=False,
                timeout=15,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return {"error": f"{type(exc).__name__}: {exc}"}
        if result.returncode != 0:
            detail = (result.stderr or result.stdout).strip().splitlines()
            return {"error": f"Comfy Python rc={result.returncode}: {detail[-1] if detail else '출력 없음'}"}
        try:
            value = json.loads(result.stdout.strip().splitlines()[-1])
        except (json.JSONDecodeError, IndexError) as exc:
            return {"error": f"Comfy PyTorch 장치 결과를 읽지 못했습니다: {type(exc).__name__}"}
        if not isinstance(value, dict):
            return {"error": "Comfy PyTorch 장치 결과 형식이 올바르지 않습니다."}
        return value

    def generate(self, plan: dict[str, Any], *, workdir: Path, progress) -> dict[str, Any]:
        workdir.mkdir(parents=True, exist_ok=True, mode=0o700)
        source = self.store.create_production(plan["brief"])
        source_id = source["manifest"]["production_id"]
        engine_runs: list[dict[str, Any]] = []
        scenes: dict[str, dict[str, Any]] = {}
        for index, scene in enumerate(plan["scenes"]):
            scene_id = scene["scene_id"]
            prompt = self._prompt(plan, scene)
            seed = int.from_bytes(
                hashlib.sha256(f"{plan['id']}:{plan['revision']}:{scene_id}".encode()).digest()[:4],
                "big",
            )
            job = self._run_comfy(prompt, seed, plan, scene_id, workdir, progress)
            engine_receipt = job["receipt"]
            output = next(
                (item for item in engine_receipt.get("outputs", []) if not item.get("intermediate")),
                None,
            )
            if output is None:
                raise RuntimeError(f"Comfy 장면 {scene_id} receipt에 최종 영상이 없습니다.")
            output_sha = output["sha256"]
            video_path = Path(output["source_path"])
            video_bytes = video_path.read_bytes()
            if hashlib.sha256(video_bytes).hexdigest() != output_sha:
                raise RuntimeError(f"Comfy 장면 {scene_id} 산출물 sha256 검증에 실패했습니다.")
            source_video = workdir / f"{index:03d}-{scene_id}.mp4"
            source_video.write_bytes(video_bytes)
            image_path = workdir / f"{index:03d}-{scene_id}.png"
            self._extract_frame(source_video, image_path)
            scenes[scene_id] = {
                "path": image_path,
                "name": f"{scene_id}.png",
                "meta": {
                    "width": 1280,
                    "height": 720,
                    "source_engine": "managed-comfy",
                    "source_profile": job["profile"],
                    "source_version": engine_receipt.get("workflow_revision_hash"),
                    "source_output_sha256": output_sha,
                    "source_production_id": source_id,
                    "source_task_id": job["task_id"],
                    "source_runner_job_id": job.get("runner_job_id"),
                    "prompt_sha256": hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
                },
            }
            engine_runs.append(
                {
                    "scene_id": scene_id,
                    "engine": "managed-comfy",
                    "profile": job["profile"],
                    "real_execution": True,
                    "prompt": prompt,
                    "prompt_sha256": hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
                    "task_id": job["task_id"],
                    "runner_job_id": job.get("runner_job_id"),
                    "output_sha256": output_sha,
                    "receipt": engine_receipt,
                }
            )

        voices: dict[str, dict[str, Any]] = {}
        voice_receipts: list[dict[str, Any]] = []
        for index, scene in enumerate(plan["scenes"]):
            scene_id = scene["scene_id"]
            voice_source, speech_receipt = self._synthesize_voice(
                scene["narration"].strip(), workdir / "tts" / scene_id
            )
            voice_bytes = voice_source.read_bytes()
            voice_sha = hashlib.sha256(voice_bytes).hexdigest()
            voice_path = workdir / f"{index:03d}-{scene_id}-voice.wav"
            voice_path.write_bytes(voice_bytes)
            voice_media = inspect_media(voice_path)
            scene_seconds = float(scene["end_time"] - scene["start_time"])
            voice_seconds = float(voice_media.get("duration") or 0)
            if not voice_media.get("has_audio") or voice_seconds <= 0:
                raise RuntimeError(f"장면 {scene_id} 음성 결과를 오디오로 검사하지 못했습니다.")
            if voice_seconds > scene_seconds + 0.2:
                raise RuntimeError(
                    f"장면 {scene_id} 음성 길이 {voice_seconds:.2f}초가 장면 길이 {scene_seconds:.2f}초를 넘습니다."
                )
            voices[scene_id] = {
                "path": voice_path,
                "name": f"{scene_id}-voice.wav",
                "meta": {
                    **voice_media,
                    "scene_id": scene_id,
                    "engine": speech_receipt["engine"],
                    "profile": speech_receipt["model_revision"],
                    "output_sha256": voice_sha,
                },
            }
            voice_receipts.append({
                "scene_id": scene_id,
                "engine": speech_receipt["engine"],
                "model_revision": speech_receipt["model_revision"],
                "real_execution": speech_receipt["real_execution"],
                "output_sha256": voice_sha,
                "cached_chunks": speech_receipt["cached_chunks"],
                "receipt": speech_receipt,
            })
            progress({
                "stage": "scene_voice_succeeded",
                "scene_id": scene_id,
                "engine": speech_receipt["engine"],
                "model_revision": speech_receipt["model_revision"],
                "execution_mode": speech_receipt["execution_mode"],
                "output_sha256": voice_sha,
            })

        total_seconds = float(plan["scenes"][-1]["end_time"])
        generated_music = self._generate_music(plan, workdir, total_seconds)
        progress({
            "stage": "music_succeeded",
            "engine": generated_music["engine"],
            "receipt": generated_music["receipt"],
        })
        master = workdir / "master-mix.wav"
        self._mix_tracks(generated_music["path"], plan["scenes"], voices, master, total_seconds)
        media = inspect_media(master)
        if not media.get("has_audio") or float(media.get("duration") or 0) + 0.1 < total_seconds:
            raise RuntimeError(
                f"음악·음성 mix 길이가 계획보다 짧습니다: {media.get('duration')}초 < {total_seconds:g}초"
            )
        return {
            "scenes": scenes,
            "voices": voices,
            "music": {
                "path": master,
                "name": "generated-music-and-voice.wav",
                "meta": {**media, "engine": "ffmpeg.mix", "music_engine": generated_music["engine"]},
            },
            "receipt": {
                "execution_mode": "real",
                "source_production_id": source_id,
                "plan_id": plan["id"],
                "plan_revision": plan["revision"],
                "engine_runs": engine_runs,
                "voices": voice_receipts,
                "music": generated_music["receipt"],
                "mix": {"engine": "ffmpeg", "version": self._ffmpeg_version()},
                "duration_seconds": total_seconds,
            },
        }

    @staticmethod
    def _prompt(plan: dict[str, Any], scene: dict[str, Any]) -> str:
        brief = str(plan.get("brief") or "").strip()
        match = re.fullmatch(r"s(\d+)", str(scene.get("scene_id") or ""))
        scene_number = int(match.group(1)) if match else None
        headings = list(re.finditer(r"(?:^|\s)(장면|scene)\s+(\d+)\s*:\s*", brief, re.IGNORECASE))
        shot_direction = ""
        plan_name = str(plan.get("name") or "").strip()
        context = (
            plan_name
            if plan_name and not brief.startswith(plan_name)
            else "a short Korean documentary"
        )
        if headings and scene_number is not None:
            for index, heading in enumerate(headings):
                if int(heading.group(2)) != scene_number:
                    continue
                start = heading.end()
                end = headings[index + 1].start() if index + 1 < len(headings) else len(brief)
                shot_direction = brief[start:end].strip(" .。\n\t")
                context = brief[: headings[0].start()].strip(" .。\n\t")
                break
        if not shot_direction:
            shot_direction = str(scene.get("narration") or scene.get("narrative_goal") or scene["title"]).strip()
        else:
            shot_direction = re.sub(
                r"(?im)^\s*(?:내레이션|narration)\s*:\s*.*$", "", shot_direction
            ).strip()
        context = " ".join(
            sentence.strip()
            for sentence in re.split(r"(?<=[.!?])\s+|(?<=。)\s*", context)
            if sentence.strip()
            and not re.search(r"내레이션|자막|narration|caption|subtitle|줌|패닝|생성형 영상 모션", sentence, re.IGNORECASE)
        )
        # The full brief may include mutually exclusive visuals for every shot. Keep only shared
        # context plus this scene's own instruction so each Comfy request has one visual target.
        value = (
            "Create one cinematic documentary shot, not a collage. "
            f"Shared project context: {context or 'a Korean documentary short'}. "
            f"Required scene {scene.get('scene_id', '')} ({scene['title']}): {shot_direction}. "
            "Preserve the named place, subjects, action, time and lighting. "
            "Make the named subjects large and clear in the foreground; keep key action above the bottom 25%, "
            "leaving that band visually simple and unobstructed. Natural photographic detail."
        )
        if len(value) > 1000:
            raise ValueError(
                f"장면 {scene.get('scene_id', '')} 시각 지시가 managed Comfy 한도 1000자를 넘습니다. "
                "장면 대본이나 시각 지시를 줄여 주세요."
            )
        return value

    def _run_comfy(
        self,
        prompt: str,
        seed: int,
        plan: dict[str, Any],
        scene_id: str,
        workdir: Path,
        progress: Callable[[dict[str, Any]], None],
    ) -> dict[str, Any]:
        checkpoint = workdir / f"{scene_id}-managed-comfy.json"
        identity = hashlib.sha256(
            json.dumps(
                {"plan_id": plan["id"], "revision": plan["revision"], "scene_id": scene_id,
                 "prompt": prompt, "negative_prompt": SCENE_NEGATIVE_PROMPT, "seed": seed},
                ensure_ascii=False,
                sort_keys=True,
            ).encode()
        ).hexdigest()
        prompt_sha256 = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
        negative_prompt_sha256 = hashlib.sha256(SCENE_NEGATIVE_PROMPT.encode("utf-8")).hexdigest()
        prior = json.loads(checkpoint.read_text()) if checkpoint.is_file() else None
        attempts = list(prior.get("attempts", [])) if prior and prior.get("identity") == identity else []
        job = None
        if attempts:
            try:
                job = self.controller.get(attempts[-1]["task_id"])
            except FileNotFoundError:
                job = None
            if job and job.get("status") == "needs_reconcile":
                raise RuntimeError(
                    f"Comfy scene {scene_id} task {job['task_id']} needs reconciliation; 자동 재제출하지 않습니다."
                )
            if job and job.get("status") in {"failed", "cancelled"}:
                job = None  # only a confirmed terminal failure permits a new managed task
        if job is None:
            job = self.controller.prepare(
                "wan22-5b-draft",
                prompt=prompt,
                negative_prompt=SCENE_NEGATIVE_PROMPT,
                seed=seed,
                owner=f"creative-v2:{plan['id']}:{plan['revision']}:{scene_id}",
                lease_wait_seconds=LEASE_HANDOFF_WAIT_SECONDS,
            )
            attempts.append({"task_id": job["task_id"], "submitted_at": time.time()})
            self._write_checkpoint(checkpoint, {"identity": identity, "attempts": attempts})
            progress({"stage": "scene_comfy_submitted", "scene_id": scene_id, "task_id": job["task_id"]})
            self.controller.start(job["task_id"])
        deadline = self._clock() + 3600
        while job.get("status") not in {"succeeded", "failed", "cancelled", "needs_reconcile"}:
            if self._clock() >= deadline:
                raise TimeoutError(f"Comfy scene {scene_id} exceeded the 60-minute wait limit.")
            time.sleep(1)
            job = self.controller.get(job["task_id"])
        if job["status"] != "succeeded":
            progress({
                "stage": f"scene_comfy_{job['status']}",
                "scene_id": scene_id,
                "task_id": job["task_id"],
                "error": job.get("error") or job.get("receipt_error"),
            })
            raise RuntimeError(
                f"Comfy scene {scene_id} ended as {job['status']}: {job.get('error') or job.get('receipt_error')}"
            )
        if not isinstance(job.get("receipt"), dict) or not job["receipt"].get("outputs"):
            raise RuntimeError(f"Comfy scene {scene_id} succeeded without a verifiable output receipt.")
        received_prompt_hash = job["receipt"].get("prompt_sha256")
        if received_prompt_hash != prompt_sha256:
            raise RuntimeError(f"Comfy scene {scene_id} receipt prompt hash does not match the submitted prompt.")
        received_negative_prompt_hash = job["receipt"].get("negative_prompt_sha256")
        if received_negative_prompt_hash != negative_prompt_sha256:
            raise RuntimeError(
                f"Comfy scene {scene_id} receipt negative prompt hash does not match the submitted negative prompt."
            )
        attempts[-1].update(
            status="succeeded",
            prompt_sha256=prompt_sha256,
            runner_job_id=job.get("runner_job_id"),
            receipt=job["receipt"],
        )
        self._write_checkpoint(checkpoint, {"identity": identity, "attempts": attempts})
        progress({
            "stage": "scene_comfy_succeeded",
            "scene_id": scene_id,
            "task_id": job["task_id"],
            "prompt_sha256": prompt_sha256,
            "runner_job_id": job.get("runner_job_id"),
            "output_sha256": next((x.get("sha256") for x in job["receipt"]["outputs"] if not x.get("intermediate")), None),
            "workflow_revision": job["receipt"].get("workflow_revision"),
        })
        return job

    @staticmethod
    def _write_checkpoint(path: Path, value: dict[str, Any]) -> None:
        # 제출한 Comfy task id 는 재시도 때 중복 제출을 막는 유일한 근거다. 전원·프로세스 종료 뒤에도 이전 checkpoint 이거나
        # 완전한 새 checkpoint 여야 하므로 내용과 디렉터리 항목을 모두 디스크에 반영한 뒤 교체한다.
        temporary = path.with_suffix(path.suffix + ".tmp")
        try:
            with temporary.open("w", encoding="utf-8") as handle:
                handle.write(json.dumps(value, ensure_ascii=False, sort_keys=True))
                handle.flush()
                os.fsync(handle.fileno())
            temporary.replace(path)
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)

    def _synthesize_voice(self, text: str, staging_root: Path) -> tuple[Path, dict[str, Any]]:
        """Run the existing Supertonic provider under its shared concurrency budget."""
        from ttakkari.tts import cache, service

        async def run() -> tuple[Path, dict[str, Any]]:
            settings = get_settings()
            if len(text) > settings.local_tts_max_text_chars:
                raise RuntimeError(f"장면 대본이 Supertonic 글자 수 제한 {settings.local_tts_max_text_chars}자를 넘습니다: {len(text)}")
            plan = service.make_plan(text, None, None, settings=settings)
            staging = staging_root / uuid.uuid4().hex
            staging.mkdir(parents=True, exist_ok=False, mode=0o700)
            async with service.budget(600) as outcome:
                if outcome != "ok":
                    raise RuntimeError(f"Supertonic 실행 예산을 얻지 못했습니다: {outcome}")
                result = await plan.provider.synthesize(
                    plan.chunks,
                    voice=plan.voice,
                    speed=plan.speed,
                    lang=plan.lang,
                    out_dir=str(staging),
                )
            chunk_paths = [Path(chunk.path) for chunk in result.chunks]
            if len(chunk_paths) != len(plan.chunks) or not chunk_paths:
                raise RuntimeError("Supertonic이 요청한 음성 청크 집합을 모두 반환하지 않았습니다.")
            if len(chunk_paths) == 1:
                path = chunk_paths[0]
                duration = result.chunks[0].duration_seconds
            else:
                path = staging / "joined.wav"
                duration = cache.concat_wavs(chunk_paths, path)
            voice_bytes = path.read_bytes()
            return path, {
                "engine": plan.provider.id,
                "model_revision": plan.provider.model_revision(),
                "execution_mode": "real",
                "real_execution": True,
                "cached_chunks": 0,
                "synthesized_chunks": len(plan.chunks),
                "duration_seconds": duration,
                "chunks": [
                    {"index": item.index, "sample_rate": item.sample_rate, "bytes": item.bytes}
                    for item in result.chunks
                ],
                "output_sha256": hashlib.sha256(voice_bytes).hexdigest(),
            }

        loop = self._loop
        if loop is not None and loop.is_running():
            return asyncio.run_coroutine_threadsafe(run(), loop).result()
        return asyncio.run(run())

    @staticmethod
    def _extract_frame(source: Path, target: Path) -> None:
        result = subprocess.run(
            [
                ffmpeg(), "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
                "-ss", "1", "-i", str(source), "-frames:v", "1",
                "-vf", "scale=1280:720:force_original_aspect_ratio=decrease,pad=1280:720:(ow-iw)/2:(oh-ih)/2",
                str(target),
            ],
            capture_output=True,
            text=True,
            timeout=90,
            check=False,
        )
        if result.returncode or not target.is_file():
            raise RuntimeError("Comfy 영상에서 장면 이미지를 추출하지 못했습니다: " + result.stderr[-300:])
        with Image.open(target) as image:
            image.verify()
        with Image.open(target) as image:
            if image.size != (1280, 720):
                raise RuntimeError(f"추출 이미지 해상도가 잘못됐습니다: {image.size}")

    def _generate_music(self, plan: dict[str, Any], workdir: Path, duration: float) -> dict[str, Any]:
        job_id = uuid.uuid4().hex
        output = workdir / "acestep"
        job = {
            "out": str(output),
            "task_type": "text2music",
            "caption": (
                "Instrumental cinematic background music for a Korean short-form video, "
                f"supportive and unobtrusive, matching this subject: {plan['brief'][:300]}"
            ),
            "lyrics": "",
            "language": "ko",
            "duration": duration,
            "seed": int.from_bytes(hashlib.sha256(f"{plan['id']}:{plan['revision']}:music".encode()).digest()[:4], "big"),
            "bpm": None,
            "thinking": False,
            "use_lm": False,
            "dit": "acestep-v15-turbo",
            "steps": 8,
            "format": "wav",
        }
        job_path = workdir / "acestep-job.json"
        job_path.write_text(json.dumps(job, ensure_ascii=False), encoding="utf-8")
        settings = get_settings()
        lease_manager = ResourceLeaseManager(data_dir=settings.data_dir)
        lease = lease_manager.acquire(
            owner_task=f"creative-v2-assets:{plan['id']}",
            owner_attempt=job_id,
            runtime_instance=f"acestep:{job_id}",
            resource_family="gpu",
            claim={"engine": "ACE-Step 1.5", "mode": "text2music", "duration_seconds": duration},
            ttl_seconds=LEASE_TTL_SECONDS,
            wait_seconds=LEASE_HANDOFF_WAIT_SECONDS,
        )
        process = None
        last_heartbeat = self._clock()
        log_path = workdir / "acestep.log"
        try:
            lease.mark_running()
            with log_path.open("wb") as log:
                process = subprocess.Popen(
                    [str(ACESTEP_PYTHON), str(ACESTEP_JOB_RUNNER), str(job_path)],
                    cwd=ACESTEP_ROOT,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    stdin=subprocess.DEVNULL,
                )
                deadline = self._clock() + ACESTEP_TIMEOUT_SECONDS
                while process.poll() is None:
                    if self._clock() >= deadline:
                        process.terminate()
                        try:
                            process.wait(timeout=20)
                        except subprocess.TimeoutExpired:
                            process.kill()
                            process.wait(timeout=10)
                        raise TimeoutError("ACE-Step 음악 생성 시간이 30분을 넘었습니다.")
                    if self._clock() - last_heartbeat >= 60:
                        lease.heartbeat(ttl_seconds=LEASE_TTL_SECONDS)
                        last_heartbeat = self._clock()
                    time.sleep(1)
                return_code = process.returncode
            if return_code != 0:
                tail = log_path.read_text(errors="replace")[-1200:]
                raise RuntimeError(f"ACE-Step exit={return_code}: {tail}")
            metrics = json.loads((output / "metrics.json").read_text())
            audio_path = Path(metrics.get("audio_path") or "")
            if not audio_path.is_file() or audio_path.stat().st_size == 0:
                raise RuntimeError("ACE-Step이 성공 exit 후에도 음악 파일을 반환하지 않았습니다.")
            media = inspect_media(audio_path)
            if not media.get("has_audio") or float(media.get("duration") or 0) + 0.1 < duration:
                raise RuntimeError(
                    f"ACE-Step 음악 길이가 계획보다 짧습니다: {media.get('duration')}초 < {duration:g}초"
                )
            lease.release(
                proof={
                    "runtime_status": "stopped",
                    "evidence": {"runtime_instance": lease.runtime_instance, "exit_code": return_code},
                }
            )
            return {
                "path": audio_path,
                "engine": "ACE-Step 1.5",
                "receipt": {
                    "version": "acestep-v15-turbo",
                    "job_id": job_id,
                    "seed": job["seed"],
                    "duration_seconds": media["duration"],
                    "sha256": hashlib.sha256(audio_path.read_bytes()).hexdigest(),
                    "resource_lease_id": lease.lease_id,
                    "execution_mode": "real",
                },
            }
        except BaseException:
            if process is not None and process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=20)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=10)
            try:
                lease.release(
                    proof={
                        "runtime_status": "stopped",
                        "evidence": {
                            "runtime_instance": lease.runtime_instance,
                            "exit_code": None if process is None else process.poll(),
                        },
                    }
                )
            except Exception:
                _logger.exception("ACE-Step 종료 뒤 GPU lease 반납에 실패했습니다: %s", lease.lease_id)
            raise

    @staticmethod
    def _mix(music: Path, voice: Path, target: Path, duration: float) -> None:
        result = subprocess.run(
            [
                ffmpeg(), "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
                "-i", str(music), "-i", str(voice),
                "-filter_complex", "[0:a]volume=0.22[m];[1:a]volume=1.0[v];[m][v]amix=inputs=2:duration=longest:normalize=0:dropout_transition=0,atrim=0:"
                + f"{duration:.3f}[out]",
                "-map", "[out]", "-t", f"{duration:.3f}", "-ar", "48000", "-ac", "2", "-c:a", "pcm_s16le", str(target),
            ],
            capture_output=True,
            text=True,
            timeout=180,
            check=False,
        )
        if result.returncode or not target.is_file():
            raise RuntimeError("음성·음악 mix에 실패했습니다: " + result.stderr[-300:])

    @staticmethod
    def _mix_tracks(
        music: Path,
        scenes: list[dict[str, Any]],
        voices: dict[str, dict[str, Any]],
        target: Path,
        duration: float,
    ) -> None:
        inputs: list[str] = ["-i", str(music)]
        filters = ["[0:a]volume=0.22[music]"]
        labels = ["[music]"]
        for index, scene in enumerate(scenes, 1):
            scene_id = scene["scene_id"]
            inputs.extend(("-i", str(voices[scene_id]["path"])))
            seconds = float(scene["end_time"] - scene["start_time"])
            delay_ms = round(float(scene["start_time"]) * 1000)
            filters.append(
                f"[{index}:a]volume=1.0,apad=pad_dur={seconds:.3f},atrim=0:{seconds:.3f},adelay={delay_ms}|{delay_ms}[voice{index}]"
            )
            labels.append(f"[voice{index}]")
        filters.append(
            "".join(labels)
            + f"amix=inputs={len(labels)}:duration=longest:normalize=0:dropout_transition=0,atrim=0:{duration:.3f}[out]"
        )
        result = subprocess.run(
            [
                ffmpeg(), "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
                *inputs, "-filter_complex", ";".join(filters), "-map", "[out]",
                "-t", f"{duration:.3f}", "-ar", "48000", "-ac", "2", "-c:a", "pcm_s16le", str(target),
            ],
            capture_output=True,
            text=True,
            timeout=240,
            check=False,
        )
        if result.returncode or not target.is_file():
            raise RuntimeError("장면별 음성·음악 mix에 실패했습니다: " + result.stderr[-300:])

    @staticmethod
    def _ffmpeg_version() -> str | None:
        try:
            result = subprocess.run([ffmpeg(), "-version"], capture_output=True, text=True, timeout=10, check=False)
            return (result.stdout.splitlines() or [None])[0]
        except (OSError, subprocess.SubprocessError):
            return None
