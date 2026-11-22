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
from typing import Any, ClassVar

from PIL import Image

from ttakkari.config import get_settings
from ttakkari.creative.director import default_director_store
from ttakkari.creative.render import ffmpeg, inspect_media
from ttakkari.creative.v2 import asset_routing

_logger = logging.getLogger(__name__)

ASSET_LAB_ROOT = Path(
    os.environ.get(
        "TTAKKARI_CREATIVE_LAB_ROOT",
        str(Path.home() / "development/projects/ttakkari-studio-lab"),
    )
)
# 앞 엔진은 성공을 기록한 뒤 런타임을 멈추고 lease 를 해제한다(Comfy 정지는 수 초에서 수십 초). 같은 작업의 다음 엔진은
# 그 인계 구간을 기다려야 하며, 기다려도 풀리지 않는 외부 점유만 실패로 올린다.
LEASE_HANDOFF_WAIT_SECONDS = 180
SCENE_NEGATIVE_PROMPT = (
    "text, letters, words, captions, subtitles, typography, watermark, watermarks, "
    "logo, logos, signage, signs, writing, symbols"
)


KEYFRAME_SIZE = (1280, 720)


class StepFailed(RuntimeError):
    """관리형 작업이 failed 로 확정됐다. 제출이 확정 실패했을 때만 다음 생성 단계로 내려간다."""


class ManagedAssetGenerator:
    """R21 scene assets via the existing managed Comfy, local TTS and ACE-Step paths."""

    engine_id = "creative-v2.managed-comfy+supertonic+acestep"
    version = "scene-routing-v1+flux2-klein+wan22-5b+local-supertonic+acestep-v15-turbo+scene-brief-v3+scene-cache-v2"

    def __init__(self, *, controller=None, store=None, clock=time.monotonic, loop=None):
        self._controller = controller
        self._store = store
        self._clock = clock
        # generate()는 worker thread 에서 돈다. TTS 실행 예산(generation.work, DB 풀)은 서버 이벤트 루프에 묶여 있어
        # 새 루프(asyncio.run)에서 쓰면 "attached to a different loop" 로 실패하므로 그 루프에서 실행한다.
        self._loop = loop
        self._unavailable: dict[str, str] = {}

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
        models_root = None
        try:
            from ttakkari import managed_comfy

            models_root = managed_comfy.COMFY_ROOT / "ComfyUI" / "models"
            profile_ids = {
                profile_id
                for scene in plan["scenes"]
                for step in asset_routing.route_scene(scene).ladder
                if (profile_id := asset_routing.STEP_PROFILES[step][0])
            }
            self._unavailable = {
                profile_id: why
                for profile_id in sorted(profile_ids)
                if (why := asset_routing.profile_blockers(profile_id, models_root=models_root))
            }
        except Exception as exc:  # noqa: BLE001 - probe errors are blockers, never a fallback
            reasons.append(f"managed Comfy 사전 점검 실패: {type(exc).__name__}: {exc}")
            self._unavailable = {}
        else:
            for scene in plan["scenes"]:
                route = asset_routing.route_scene(scene)
                if not self._usable_steps(route):
                    reasons.append(
                        f"장면 {route.scene_id}에 쓸 수 있는 생성 프로필이 없습니다: "
                        + "; ".join(f"{pid}: {why}" for pid, why in self._unavailable.items())
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
        music = asset_routing.MUSIC_PROFILE
        if models_root is not None:
            why = asset_routing.profile_blockers(music, models_root=models_root)
            if why:
                reasons.append("ACE-Step 로컬 음악 생성 파일이 없습니다: " + "; ".join(why))
        total = float(plan["scenes"][-1]["end_time"]) if plan["scenes"] else 0.0
        limit = self._music_limit_seconds()
        if total > limit:
            reasons.append(f"음악 길이 {total:g}초가 ACE-Step 프로필 한도 {limit:g}초를 넘습니다.")
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
    def _music_limit_seconds() -> float:
        from ttakkari import managed_comfy

        return float(managed_comfy.PROFILES[asset_routing.MUSIC_PROFILE].seconds[2])

    def _usable_steps(self, route: asset_routing.SceneRoute) -> list[str]:
        """사다리에서 프로필이 막히지 않은 단계. camera_motion 은 키프레임 프로필이 있어야 한다."""
        unavailable = getattr(self, "_unavailable", {})
        return [step for step in route.ladder if asset_routing.STEP_PROFILES[step][0] not in unavailable]

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
            route = asset_routing.route_scene(scene)
            # Revision is deliberately absent: editing a different scene must not
            # regenerate successful Comfy media. Prompt/style/route/source code
            # changes invalidate this scene's checkpoint. The generator's lock
            # serializes work; cache receipts are published atomically last.
            cache_key = hashlib.sha256(json.dumps({
                "plan_id": plan["id"], "scene_id": scene_id, "prompt": prompt,
                "negative_prompt": SCENE_NEGATIVE_PROMPT, "aspect": plan["aspect"], "route": route.record(),
                "version": self.version, "code": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            }, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
            cache = workdir.parent / "scene-cache" / cache_key
            checkpoint = cache / "checkpoint.json"
            if checkpoint.is_file():
                entry = json.loads(checkpoint.read_text())
                cached_source = cache / entry.get("source_name", "source.mp4")
                cached_preview = cache / "preview.png"
                has_preview = entry.get("preview_sha256") is not None
                if (cached_source.is_file() and (cached_preview.is_file() or not has_preview)
                    and hashlib.sha256(cached_source.read_bytes()).hexdigest() == entry["sha256"]
                    and (not has_preview
                         or hashlib.sha256(cached_preview.read_bytes()).hexdigest() == entry["preview_sha256"])):
                    scenes[scene_id] = {**entry["asset"], "path": cached_source,
                                        "preview_path": cached_preview if has_preview else None}
                    engine_runs.append({**entry["engine_run"], "cache_hit": True, "cache_key": cache_key})
                    progress({"stage": "scene_reused", "scene_id": scene_id, "output_sha256": entry["sha256"], "cache_key": cache_key})
                    continue
                progress({"stage": "scene_cache_invalid", "scene_id": scene_id, "cache_key": cache_key})
            seed = int.from_bytes(
                hashlib.sha256(cache_key.encode()).digest()[:4],
                "big",
            )
            asset, engine_run = self._produce_scene(
                plan, scene, index, route, prompt, seed, workdir, progress, source_id)
            scenes[scene_id] = asset
            engine_runs.append(engine_run)
            source_path, preview_path = Path(asset["path"]), asset.get("preview_path")
            source_sha = hashlib.sha256(source_path.read_bytes()).hexdigest()
            cache.mkdir(parents=True, exist_ok=True)
            source_name = f"source{source_path.suffix}"
            (cache / source_name).write_bytes(source_path.read_bytes())
            if preview_path:
                (cache / "preview.png").write_bytes(Path(preview_path).read_bytes())
            entry = {
                "sha256": source_sha, "source_name": source_name,
                "preview_sha256": hashlib.sha256(Path(preview_path).read_bytes()).hexdigest() if preview_path else None,
                "asset": {k: v for k, v in asset.items() if k not in {"path", "preview_path"}},
                "engine_run": engine_run,
            }
            temp = cache / f"{uuid.uuid4()}.tmp"
            temp.write_text(json.dumps(entry, ensure_ascii=False, indent=2))
            temp.replace(checkpoint)

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
        generated_music = self._generate_music(plan, workdir, total_seconds, progress)
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

    _STYLE_ENTRY_DIRECTIVES: ClassVar[dict[str, tuple[str, str]]] = {
        "C03.pixel_art": (
            "Create one crisp 2D pixel-art animation shot, not a collage.",
            "Crisp pixel-art sprites and retro palette detail.",
        ),
        "C03.lowpoly_3d": (
            "Create one stylized low-poly 3D animated shot, not a collage.",
            "Clean low-poly 3D geometry and soft shading detail.",
        ),
        "C03.cel_shaded_3d": (
            "Create one cel-shaded 3D animated shot, not a collage.",
            "Stylized 3D cel-shading and crisp contour detail.",
        ),
        "C03.game_engine_3d": (
            "Create one real-time 3D game-engine cinematic shot, not a collage.",
            "Dynamic 3D spatial depth and atmospheric lighting detail.",
        ),
        "C03.miniature_diorama": (
            "Create one tactile miniature diorama shot, not a collage.",
            "Handcrafted miniature texture and shallow depth of field.",
        ),
        "C03.clay_animation": (
            "Create one tactile claymation stop-motion shot, not a collage.",
            "Sculpted clay surface texture and studio stop-motion lighting.",
        ),
        "C03.stop_motion": (
            "Create one handcrafted stop-motion animation shot, not a collage.",
            "Tactile physical material detail and studio lighting.",
        ),
        "C03.flat_2d_illustration": (
            "Create one expressive 2D animated illustration shot, not a collage.",
            "Clean 2D illustration linework and cohesive flat color palette.",
        ),
        "C03.2d_rig_cutout": (
            "Create one layered 2D cutout animation shot, not a collage.",
            "Consistent 2D character silhouettes and crisp layer separation.",
        ),
        "C03.western_cartoon": (
            "Create one expressive 2D cartoon animation shot, not a collage.",
            "Bold cartoon linework, lively posing and vibrant cel colors.",
        ),
        "C03.comic_2d": (
            "Create one dynamic 2D motion-comic shot, not a collage.",
            "Graphic ink linework and dramatic comic shading.",
        ),
        "C03.korean_webtoon": (
            "Create one expressive Korean webtoon animation shot, not a collage.",
            "Polished digital manhwa linework and atmospheric webtoon coloring.",
        ),
        "C03.japanese_anime": (
            "Create one expressive 2D anime cinematic shot, not a collage.",
            "Clean anime cel shading, expressive lighting and painted background.",
        ),
        "C03.vector_motion_graphics": (
            "Create one clean motion-graphics visual shot, not a collage.",
            "Sharp vector graphic clarity and structured visual hierarchy.",
        ),
        "C03.ui_animation": (
            "Create one clean interface motion visual shot, not a collage.",
            "Modern graphic layout and smooth visual hierarchy.",
        ),
        "C03.infographic": (
            "Create one structured data-visualization motion shot, not a collage.",
            "Clear visual diagram composition and high-contrast graphic detail.",
        ),
        "C03.ink_wash": (
            "Create one traditional East Asian ink-wash painting shot, not a collage.",
            "Expressive sumi-e brush strokes, ink gradients and rice-paper texture.",
        ),
        "C03.watercolor": (
            "Create one painterly watercolor animation shot, not a collage.",
            "Soft watercolor pigment bleed and textured paper grain.",
        ),
        "C03.oil_painting": (
            "Create one painterly oil-on-canvas animation shot, not a collage.",
            "Rich impasto brushwork and classical tonal depth.",
        ),
    }

    _WORLD_ENTRY_LABELS: ClassVar[dict[str, str]] = {
        "C02.cyberpunk": "a cyberpunk neon world",
        "C02.space_sf": "a deep-space sci-fi world",
        "C02.near_future": "a near-future sci-fi setting",
        "C02.fantasy": "a high-fantasy world",
        "C02.joseon": "a historical Joseon dynasty setting",
        "C02.three_kingdoms": "a Three Kingdoms historical setting",
        "C02.modern_history": "a modern historical setting",
        "C02.post_apocalypse": "a post-apocalyptic world",
        "C02.daily_life": "an everyday life setting",
    }

    @classmethod
    def _resolve_style_directives(cls, plan: dict[str, Any], scene: dict[str, Any]) -> tuple[str, str, str, str]:
        raw_selections = (
            (plan.get("match") or {}).get("selections")
            or (plan.get("intent") or {}).get("selections")
            or []
        )
        entry_ids: list[str] = []
        for item in raw_selections:
            eid = item.get("entry_id") if isinstance(item, dict) else getattr(item, "entry_id", None)
            if isinstance(eid, str) and eid:
                entry_ids.append(eid)

        opening = "Create one cinematic documentary shot, not a collage."
        closing = "Natural photographic detail."
        for eid in entry_ids:
            if eid in cls._STYLE_ENTRY_DIRECTIVES:
                opening, closing = cls._STYLE_ENTRY_DIRECTIVES[eid]
                break

        default_context = "a Korean documentary short"
        for eid in entry_ids:
            if eid in cls._WORLD_ENTRY_LABELS:
                default_context = cls._WORLD_ENTRY_LABELS[eid]
                break
        else:
            if "C01.animation" in entry_ids or any(e.startswith("C03.") and e not in {"C03.photoreal", "C03.cinematic_realism"} for e in entry_ids):
                default_context = "a short animated visual story"
            elif "C01.tech_explainer" in entry_ids or "C01.science_edu" in entry_ids:
                default_context = "a clear visual explainer short"

        style_obj = plan.get("style")
        dna = (style_obj.get("dna") or style_obj) if isinstance(style_obj, dict) else {}
        style_parts: list[str] = []
        if isinstance(dna, dict):
            for facet_name in ("visual_identity", "lighting", "materials", "camera_language", "environment_rules"):
                facet = dna.get(facet_name)
                text = facet.get("text") if isinstance(facet, dict) else None
                if isinstance(text, str) and text.strip():
                    cleaned = re.sub(
                        r"(?i)\b(?:no\s+text|caption|captions|subtitle|subtitles|narration)\b|자막|내레이션",
                        "",
                        text,
                    ).strip(" ,.;")
                    if cleaned:
                        style_parts.append(cleaned)
            palette = dna.get("palette")
            if isinstance(palette, list) and palette:
                swatches = [
                    f"{p.get('role', 'tone')} {p.get('hex')}"
                    for p in palette[:3]
                    if isinstance(p, dict) and p.get("hex")
                ]
                if swatches:
                    style_parts.append("palette " + ", ".join(swatches))
        style_clause = "; ".join(style_parts)[:140].strip(" ;.")
        return opening, closing, default_context, style_clause

    @classmethod
    def _prompt(cls, plan: dict[str, Any], scene: dict[str, Any]) -> str:
        brief = str(plan.get("brief") or "").strip()
        match = re.fullmatch(r"s(\d+)", str(scene.get("scene_id") or ""))
        scene_number = int(match.group(1)) if match else None
        headings = list(re.finditer(r"(?:^|\s)(장면|scene)\s+(\d+)\s*:\s*", brief, re.IGNORECASE))
        shot_direction = ""
        plan_name = str(plan.get("name") or "").strip()
        opening, closing, default_context, style_clause = cls._resolve_style_directives(plan, scene)
        context = (
            plan_name
            if plan_name and not brief.startswith(plan_name)
            else default_context
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
        style_sentence = f" Visual style DNA: {style_clause}." if style_clause else ""
        # The full brief may include mutually exclusive visuals for every shot. Keep only shared
        # context plus this scene's own instruction so each Comfy request has one visual target.
        value = (
            f"{opening} "
            f"Shared project context: {context or default_context}. "
            f"Required scene {scene.get('scene_id', '')} ({scene['title']}): {shot_direction}.{style_sentence} "
            "Preserve the named place, subjects, action, time and lighting. "
            "Make the named subjects large and clear in the foreground; keep key action above the bottom 25%, "
            f"leaving that band visually simple and unobstructed. {closing}"
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
        return self._run_profile(
            "wan22-5b-draft", prompt, seed, plan, scene_id, workdir, progress,
            negative_prompt=SCENE_NEGATIVE_PROMPT,
        )

    def _run_profile(
        self,
        profile_id: str,
        prompt: str,
        seed: int,
        plan: dict[str, Any],
        scene_id: str,
        workdir: Path,
        progress: Callable[[dict[str, Any]], None],
        *,
        step: str | None = None,
        negative_prompt: str | None = None,
        options: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """프로필 하나를 제출하고 끝날 때까지 기다린다. 같은 입력의 이전 task 가 있으면 재제출하지 않고 이어 받는다.

        실패(failed)는 StepFailed 로 올려 호출자가 다음 단계로 내려가게 하고, cancelled/needs_reconcile 은 자동 재제출이나
        우회 없이 RuntimeError 로 멈춘다.
        """
        options = dict(options or {})
        label = scene_id if step is None else f"{scene_id}-{step}"
        checkpoint = workdir / f"{label}-managed-comfy.json"
        identity = hashlib.sha256(
            json.dumps(
                {"plan_id": plan["id"], "revision": plan["revision"], "scene_id": scene_id,
                 "prompt": prompt, "negative_prompt": negative_prompt, "seed": seed,
                 **({"profile": profile_id, "options": options} if step is not None else {})},
                ensure_ascii=False,
                sort_keys=True,
            ).encode()
        ).hexdigest()
        prompt_sha256 = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
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
                    f"Comfy scene {label} task {job['task_id']} needs reconciliation; 자동 재제출하지 않습니다."
                )
            if job and job.get("status") in {"failed", "cancelled"}:
                job = None  # only a confirmed terminal failure permits a new managed task
        if job is None:
            kwargs: dict[str, Any] = {"prompt": prompt, "seed": seed,
                                      "owner": f"creative-v2:{plan['id']}:{plan['revision']}:{scene_id}",
                                      "lease_wait_seconds": LEASE_HANDOFF_WAIT_SECONDS, **options}
            if negative_prompt is not None:
                kwargs["negative_prompt"] = negative_prompt
            job = self.controller.prepare(profile_id, **kwargs)
            attempts.append({"task_id": job["task_id"], "submitted_at": time.time()})
            self._write_checkpoint(checkpoint, {"identity": identity, "attempts": attempts})
            progress({"stage": "scene_comfy_submitted", "scene_id": scene_id, "task_id": job["task_id"],
                      **({"step": step, "profile": profile_id} if step else {})})
            self.controller.start(job["task_id"])
        deadline = self._clock() + 3600
        while job.get("status") not in {"succeeded", "failed", "cancelled", "needs_reconcile"}:
            if self._clock() >= deadline:
                raise TimeoutError(f"Comfy scene {label} exceeded the 60-minute wait limit.")
            time.sleep(1)
            job = self.controller.get(job["task_id"])
        if job["status"] != "succeeded":
            progress({
                "stage": f"scene_comfy_{job['status']}",
                "scene_id": scene_id,
                "task_id": job["task_id"],
                "error": job.get("error") or job.get("receipt_error"),
                **({"step": step, "profile": profile_id} if step else {}),
            })
            message = f"Comfy scene {label} ended as {job['status']}: {job.get('error') or job.get('receipt_error')}"
            if job["status"] == "failed":
                raise StepFailed(message)
            raise RuntimeError(message)
        if not isinstance(job.get("receipt"), dict) or not job["receipt"].get("outputs"):
            raise RuntimeError(f"Comfy scene {label} succeeded without a verifiable output receipt.")
        received_prompt_hash = job["receipt"].get("prompt_sha256")
        if received_prompt_hash != prompt_sha256:
            raise RuntimeError(f"Comfy scene {label} receipt prompt hash does not match the submitted prompt.")
        if negative_prompt is not None:
            negative_prompt_sha256 = hashlib.sha256(negative_prompt.encode("utf-8")).hexdigest()
            if job["receipt"].get("negative_prompt_sha256") != negative_prompt_sha256:
                raise RuntimeError(
                    f"Comfy scene {label} receipt negative prompt hash does not match the submitted negative prompt."
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
            **({"step": step, "profile": profile_id} if step else {}),
        })
        return job

    @staticmethod
    def _final_output(job: dict[str, Any], label: str) -> tuple[dict[str, Any], bytes]:
        """receipt 의 최종 출력 하나를 읽고 sha256 이 receipt 와 같은지 확인한다."""
        output = next(
            (item for item in job["receipt"].get("outputs", []) if not item.get("intermediate")),
            None,
        )
        if output is None:
            raise RuntimeError(f"Comfy 장면 {label} receipt에 최종 산출물이 없습니다.")
        data = Path(output["source_path"]).read_bytes()
        if hashlib.sha256(data).hexdigest() != output["sha256"]:
            raise RuntimeError(f"Comfy 장면 {label} 산출물 sha256 검증에 실패했습니다.")
        return output, data

    def _keyframe(self, state: dict[str, Any], plan: dict[str, Any], scene_id: str, index: int, prompt: str,
                  seed: int, workdir: Path, progress) -> dict[str, Any]:
        """장면당 한 번만 만든다. 이미지→영상과 카메라 움직임 폴백이 같은 키프레임을 쓴다."""
        if "keyframe" in state:
            return state["keyframe"]
        suffix = " No visible text, letters, logos or signage."
        job = self._run_profile(
            asset_routing.KEYFRAME_PROFILE, prompt if len(prompt) + len(suffix) > 1000 else prompt + suffix, seed,
            plan, scene_id, workdir, progress, step="keyframe",
            options={"width": KEYFRAME_SIZE[0], "height": KEYFRAME_SIZE[1]},
        )
        output, data = self._final_output(job, f"{scene_id}-keyframe")
        path = workdir / f"{index:03d}-{scene_id}-keyframe.png"
        path.write_bytes(data)
        with Image.open(path) as image:
            image.verify()
        state["keyframe"] = {"job": job, "path": path, "sha256": output["sha256"]}
        return state["keyframe"]

    def _produce_scene(self, plan: dict[str, Any], scene: dict[str, Any], index: int,
                       route: asset_routing.SceneRoute, prompt: str, seed: int, workdir: Path, progress,
                       source_id: str) -> tuple[dict[str, Any], dict[str, Any]]:
        """사다리를 위에서부터 시도해 첫 성공을 쓴다. 막힌 단계와 실패한 단계의 사유를 모두 route 에 남긴다."""
        scene_id = scene["scene_id"]
        usable = self._usable_steps(route)
        attempts: list[dict[str, Any]] = [
            {"step": step, "profile": asset_routing.STEP_PROFILES[step][0], "status": "blocked",
             "reason": getattr(self, "_unavailable", {}).get(asset_routing.STEP_PROFILES[step][0])}
            for step in route.ladder if step not in usable
        ]
        state: dict[str, Any] = {}
        for step in usable:
            profile_id, generative = asset_routing.STEP_PROFILES[step]
            attempt: dict[str, Any] = {"step": step, "profile": profile_id}
            attempts.append(attempt)
            try:
                produced = self._run_step(step, state, plan, scene, index, route, prompt, seed, workdir, progress,
                                          source_id)
            except StepFailed as exc:
                attempt.update(status="failed", reason=str(exc)[:500])
                progress({"stage": "scene_step_failed", "scene_id": scene_id, "step": step, "error": str(exc)[:500]})
                continue
            except ValueError as exc:
                # 이미 배포된 컨트롤러에 프로필이 없거나 입력이 거부된 경우. 제출되지 않았으므로 다음 단계로 내려간다.
                attempt.update(status="rejected", reason=str(exc)[:500])
                progress({"stage": "scene_step_rejected", "scene_id": scene_id, "step": step, "error": str(exc)[:500]})
                continue
            attempt["status"] = "succeeded"
            fallback = None
            if route.need == "video" and not generative:
                failed = [a for a in attempts if a["status"] in {"blocked", "failed", "rejected"}]
                fallback = ("생성형 영상을 만들 수 없어 키프레임 이미지에 카메라 움직임을 입혔다: "
                            + "; ".join(f"{a['step']} {a['status']}: {a.get('reason')}" for a in failed))
            asset, engine_run = produced
            route_record = {**route.record(), "attempts": attempts, "selected": step,
                            "generative_motion": bool(generative and route.need == "video"),
                            "fallback_reason": fallback}
            asset["meta"]["generation_route"] = route_record
            if fallback:
                asset["meta"]["fallback_reason"] = fallback
            engine_run["route"] = route_record
            return asset, engine_run
        raise RuntimeError(
            f"장면 {scene_id}의 모든 생성 단계가 실패했습니다: "
            + "; ".join(f"{a['step']} {a['status']}: {a.get('reason')}" for a in attempts)
        )

    def _run_step(self, step: str, state: dict[str, Any], plan: dict[str, Any], scene: dict[str, Any], index: int,
                  route: asset_routing.SceneRoute, prompt: str, seed: int, workdir: Path, progress,
                  source_id: str) -> tuple[dict[str, Any], dict[str, Any]]:
        scene_id = scene["scene_id"]
        if step == "keyframe":
            keyframe = self._keyframe(state, plan, scene_id, index, prompt, seed, workdir, progress)
            return self._still_asset(keyframe, plan, scene_id, index, prompt, source_id)
        if step == "camera_motion":
            keyframe = self._keyframe(state, plan, scene_id, index, prompt, seed, workdir, progress)
            return self._camera_motion_asset(keyframe, plan, scene, scene_id, index, prompt, source_id)
        if step == "keyframe_i2v":
            keyframe = self._keyframe(state, plan, scene_id, index, prompt, seed, workdir, progress)
            suffix = " The camera moves slowly and smoothly; subtle natural motion."
            motion_prompt = prompt if len(prompt) + len(suffix) > 1000 else prompt + suffix
            job = self._run_profile(
                asset_routing.I2V_PROFILE, motion_prompt, seed, plan, scene_id, workdir, progress, step="i2v",
                negative_prompt=SCENE_NEGATIVE_PROMPT,
                options={"image_task_id": keyframe["job"]["task_id"],
                         **asset_routing.clip_options(asset_routing.I2V_PROFILE, route.clip_seconds)},
            )
            return self._video_asset(job, workdir, scene_id, index, motion_prompt, source_id, label="i2v",
                                     keyframe=keyframe)
        if step == "t2v":
            job = self._run_profile(
                asset_routing.T2V_LONG_PROFILE, prompt, seed, plan, scene_id, workdir, progress, step="t2v",
                negative_prompt=SCENE_NEGATIVE_PROMPT,
                options=asset_routing.clip_options(asset_routing.T2V_LONG_PROFILE, route.clip_seconds),
            )
            return self._video_asset(job, workdir, scene_id, index, prompt, source_id, label="t2v")
        if step == "t2v_draft":
            job = self._run_profile(
                asset_routing.T2V_DRAFT_PROFILE, prompt, seed, plan, scene_id, workdir, progress, step="t2v-draft",
                negative_prompt=SCENE_NEGATIVE_PROMPT,
            )
            return self._video_asset(job, workdir, scene_id, index, prompt, source_id, label="t2v-draft")
        raise ValueError(f"알 수 없는 생성 단계: {step}")

    def _video_asset(self, job: dict[str, Any], workdir: Path, scene_id: str, index: int, prompt: str,
                     source_id: str, *, label: str, keyframe: dict[str, Any] | None = None):
        engine_receipt = job["receipt"]
        output, video_bytes = self._final_output(job, f"{scene_id}-{label}")
        output_sha = output["sha256"]
        source_video = workdir / f"{index:03d}-{scene_id}.mp4"
        source_video.write_bytes(video_bytes)
        image_path = source_video.with_suffix(".png")
        self._extract_frame(source_video, image_path)
        video_media = inspect_media(source_video)
        prompt_sha = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
        asset = {
            "path": source_video,
            "preview_path": image_path,
            "name": f"{scene_id}.mp4",
            "kind": "video",
            "meta": {
                # inspect_media 는 길이만 준다. 해상도·fps 는 receipt 가 컨테이너에서 읽은 실측값을 우선한다.
                "width": int(output.get("width") or video_media.get("width") or 1280),
                "height": int(output.get("height") or video_media.get("height") or 720),
                "duration": float(video_media.get("duration") or output.get("duration") or 0.0),
                "fps": output.get("fps") or video_media.get("fps"),
                "has_video": bool(video_media.get("has_video", True)),
                "media_kind": "video",
                "has_motion": True,
                "motion_source": "generative_video",
                "preview_image": image_path.name,
                "source_engine": "managed-comfy",
                "source_profile": job["profile"],
                "source_version": engine_receipt.get("workflow_revision_hash"),
                "source_output_sha256": output_sha,
                "source_production_id": source_id,
                "source_task_id": job["task_id"],
                "source_runner_job_id": job.get("runner_job_id"),
                "prompt_sha256": prompt_sha,
                **({"keyframe_task_id": keyframe["job"]["task_id"], "keyframe_sha256": keyframe["sha256"]}
                   if keyframe else {}),
            },
        }
        engine_run = {
            "scene_id": scene_id,
            "engine": "managed-comfy",
            "profile": job["profile"],
            "real_execution": True,
            "prompt": prompt,
            "prompt_sha256": prompt_sha,
            "task_id": job["task_id"],
            "runner_job_id": job.get("runner_job_id"),
            "output_sha256": output_sha,
            "receipt": engine_receipt,
            **({"keyframe": {"task_id": keyframe["job"]["task_id"], "sha256": keyframe["sha256"],
                             "profile": keyframe["job"]["profile"]}} if keyframe else {}),
        }
        return asset, engine_run

    def _still_asset(self, keyframe: dict[str, Any], plan: dict[str, Any], scene_id: str, index: int, prompt: str,
                     source_id: str):
        job, path = keyframe["job"], keyframe["path"]
        with Image.open(path) as image:
            width, height = image.size
        prompt_sha = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
        asset = {
            "path": path, "preview_path": None, "name": f"{scene_id}.png", "kind": "image",
            "meta": {
                "width": width, "height": height, "media_kind": "image", "has_motion": False,
                "motion_source": "none",
                "source_engine": "managed-comfy", "source_profile": job["profile"],
                "source_version": job["receipt"].get("workflow_revision_hash"),
                "source_output_sha256": keyframe["sha256"], "source_production_id": source_id,
                "source_task_id": job["task_id"], "source_runner_job_id": job.get("runner_job_id"),
                "prompt_sha256": prompt_sha,
            },
        }
        engine_run = {
            "scene_id": scene_id, "engine": "managed-comfy", "profile": job["profile"], "real_execution": True,
            "prompt": prompt, "prompt_sha256": prompt_sha, "task_id": job["task_id"],
            "runner_job_id": job.get("runner_job_id"), "output_sha256": keyframe["sha256"],
            "receipt": job["receipt"],
        }
        return asset, engine_run

    def _camera_motion_asset(self, keyframe: dict[str, Any], plan: dict[str, Any], scene: dict[str, Any],
                             scene_id: str, index: int, prompt: str, source_id: str):
        job, still = keyframe["job"], keyframe["path"]
        seconds = min(max(float(scene.get("seconds") or 4.0), 1.0), 60.0)
        target = still.with_name(f"{index:03d}-{scene_id}.mp4")
        motion = "push_in" if index % 2 == 0 else "pull_out"
        self._render_camera_motion(still, target, seconds, motion)
        media = inspect_media(target)
        preview = target.with_suffix(".png")
        self._extract_frame(target, preview)
        sha = hashlib.sha256(target.read_bytes()).hexdigest()
        prompt_sha = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
        asset = {
            "path": target, "preview_path": preview, "name": f"{scene_id}.mp4", "kind": "video",
            "meta": {
                "width": 1280, "height": 720,  # _render_camera_motion 이 zoompan s=1280x720 으로 고정
                "duration": float(media.get("duration") or 0.0), "fps": 24,
                "has_video": True, "media_kind": "video", "has_motion": True,
                "motion_source": "image_camera_motion", "camera_motion": motion,
                "preview_image": preview.name,
                "source_engine": "managed-comfy+ffmpeg.camera_motion", "source_profile": job["profile"],
                "source_version": job["receipt"].get("workflow_revision_hash"),
                "source_output_sha256": sha, "keyframe_sha256": keyframe["sha256"],
                "source_production_id": source_id, "source_task_id": job["task_id"],
                "source_runner_job_id": job.get("runner_job_id"), "prompt_sha256": prompt_sha,
            },
        }
        engine_run = {
            "scene_id": scene_id, "engine": "managed-comfy+ffmpeg.camera_motion", "profile": job["profile"],
            "real_execution": True, "prompt": prompt, "prompt_sha256": prompt_sha, "task_id": job["task_id"],
            "runner_job_id": job.get("runner_job_id"), "output_sha256": sha, "receipt": job["receipt"],
            "camera_motion": {"motion": motion, "seconds": seconds, "keyframe_sha256": keyframe["sha256"]},
        }
        return asset, engine_run

    @staticmethod
    def _render_camera_motion(still: Path, target: Path, seconds: float, motion: str) -> None:
        frames = max(1, round(seconds * 24))
        zoom = (f"1+0.12*on/{frames}" if motion == "push_in" else f"1.12-0.12*on/{frames}")
        result = subprocess.run(
            [
                ffmpeg(), "-hide_banner", "-loglevel", "error", "-nostdin", "-y", "-i", str(still),
                "-vf", (f"scale=2560:1440,zoompan=z='{zoom}':x='(iw-iw/zoom)/2':y='(ih-ih/zoom)/2':"
                        f"d={frames}:s=1280x720:fps=24,format=yuv420p"),
                "-frames:v", str(frames), "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", str(target),
            ],
            capture_output=True, text=True, timeout=600, check=False,
        )
        if result.returncode or not target.is_file():
            raise RuntimeError("키프레임 카메라 움직임 클립을 만들지 못했습니다: " + result.stderr[-300:])

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

    def _generate_music(self, plan: dict[str, Any], workdir: Path, duration: float, progress=None) -> dict[str, Any]:
        """ACE-Step 를 관리형 프로필 acestep15-music 으로 돌린다(같은 GPU lease, receipt 포함)."""
        caption = (
            "Instrumental cinematic background music for a Korean short-form video, "
            f"supportive and unobtrusive, matching this subject: {plan['brief'][:300]}"
        )
        seed = int.from_bytes(hashlib.sha256(f"{plan['id']}:{plan['revision']}:music".encode()).digest()[:4], "big")
        job = self._run_profile(
            asset_routing.MUSIC_PROFILE, caption, seed, plan, "music", workdir, progress or (lambda _event: None),
            step="music", options={"duration_seconds": float(duration)},
        )
        output, data = self._final_output(job, "music")
        audio_path = workdir / "music.wav"
        audio_path.write_bytes(data)
        media = inspect_media(audio_path)
        if not media.get("has_audio") or float(media.get("duration") or 0) + 0.1 < duration:
            raise RuntimeError(
                f"ACE-Step 음악 길이가 계획보다 짧습니다: {media.get('duration')}초 < {duration:g}초"
            )
        return {
            "path": audio_path,
            "engine": "ACE-Step 1.5",
            "receipt": {
                "version": "acestep-v15-turbo",
                "profile": job["profile"],
                "task_id": job["task_id"],
                "seed": seed,
                "duration_seconds": media["duration"],
                "requested_seconds": float(duration),
                "sha256": output["sha256"],
                "resource_lease_id": (job["receipt"].get("lease") or {}).get("lease_id"),
                "execution_mode": "real",
            },
        }

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
