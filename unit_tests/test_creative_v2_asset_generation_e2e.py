"""자동 소재 생성(ManagedAssetGenerator)을 실제 `Settings()`로 끝까지 통과시키는 회귀 테스트.

운영 2026-10-23 07:59Z: TTS 단계에서 `'Settings' object has no attribute 'local_tts_max_chars'`로 실패했다.
기존 테스트는 엔진 경계를 통째로 가짜로 바꿔 Settings 참조를 한 번도 실행하지 않았다. 여기서는 엔진 호출(Comfy 작업,
Supertonic 워커, ACE-Step 자식 프로세스)만 대체하고 Settings·tts.service·lease·ffmpeg mix·자산 등록·dispatch 는 실제로 쓴다.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import math
import struct
import subprocess
import sys
import textwrap
import wave
from contextlib import asynccontextmanager
from pathlib import Path

import pytest
from PIL import Image, ImageDraw, ImageFont

from ttakkari import generation
from ttakkari.config import Settings
from ttakkari.creative.director import DirectorStore
from ttakkari.creative.render import ffmpeg, inspect_media, scan
from ttakkari.creative.service import Studio
from ttakkari.creative.v2 import asset_generation, dispatch
from ttakkari.creative.v2 import asset_generation_managed as managed
from ttakkari.creative.v2.intent import InfoItem, IntentSpec, Selection
from ttakkari.creative.v2.plans import PlanStore, build_plan
from ttakkari.creative.v2.registry import Registry
from ttakkari.host_resource import ResourceLeaseManager
from ttakkari.tts import service as tts_service
from ttakkari.tts.providers import (
    ProviderAvailability,
    ProviderState,
    TtsChunkResult,
    TtsResult,
    TtsVoice,
)
from ttakkari.tts.supertonic import SupertonicProvider

LONG_NARRATION = (
    "첫 번째 장면에서는 준비한 풍경을 천천히 보여 주고 핵심만 짧게 설명합니다. "
    "두 번째 문장은 청크 경계를 넘기기 위해 일부러 길게 적어서 합성 결과를 하나로 이어 붙이는 경로를 지나가게 합니다."
)


def _intent() -> IntentSpec:
    return IntentSpec(
        selections=[
            Selection(entry_id="C08.three_act", role="primary", confidence=1.0),
            Selection(entry_id="C10.dissolve", role="secondary", confidence=1.0),
        ],
        info_items=[
            InfoItem(id=f"scene-{i}", title=f"장면 {i}", narration=LONG_NARRATION if i == 0 else f"장면 {i}의 설명")
            for i in range(3)
        ],
    )


def _wav(path: Path, seconds: float, rate: int = 22050) -> Path:
    with wave.open(str(path), "wb") as stream:
        stream.setnchannels(1)
        stream.setsampwidth(2)
        stream.setframerate(rate)
        stream.writeframes(
            b"".join(struct.pack("<h", int(9000 * math.sin(2 * math.pi * 220 * n / rate)))
                     for n in range(int(rate * seconds)))
        )
    return path


class _FakeSupertonic(SupertonicProvider):
    """워커 프로세스 호출만 대체한다. 설정 참조·청크 분할·캐시 키는 실제 service 코드가 한다."""

    def availability(self):
        return ProviderAvailability(ProviderState.ready, "test")

    def model_revision(self):
        return "supertonic-test"

    def voices_sync(self):
        return [TtsVoice(id="M1", name="남성 1", lang=("ko", "en"), gender="male")]

    async def synthesize(self, chunks, *, voice, speed, lang, out_dir):
        items = []
        for index, _text in enumerate(chunks):
            path = _wav(Path(out_dir) / f"chunk-{index}.wav", 0.4)
            items.append(TtsChunkResult(index=index, path=str(path), duration_seconds=0.4,
                                        sample_rate=22050, bytes=path.stat().st_size))
        return TtsResult(chunks=items)


class _FakeController:
    """managed Comfy 작업 하나를 즉시 성공 receipt 로 돌려준다. receipt 의 영상은 실제 mp4 다."""

    def __init__(self, root: Path):
        self.root = root
        self.jobs: dict[str, dict] = {}
        self.waits: list[float] = []
        self.prompts: list[str] = []

    def prepare(self, profile, *, prompt, seed, owner, lease_wait_seconds=0):
        self.waits.append(lease_wait_seconds)
        self.prompts.append(prompt)
        task_id = hashlib.sha256(f"{owner}:{seed}".encode()).hexdigest()[:32]
        video = self.root / f"{task_id}.mp4"
        subprocess.run(
            [ffmpeg(), "-hide_banner", "-loglevel", "error", "-nostdin", "-y", "-f", "lavfi",
             "-i", "testsrc=size=1280x720:rate=24:duration=3", "-pix_fmt", "yuv420p", str(video)],
            check=True, capture_output=True, timeout=60,
        )
        sha = hashlib.sha256(video.read_bytes()).hexdigest()
        self.jobs[task_id] = {
            "task_id": task_id, "profile": profile, "status": "succeeded", "runner_job_id": f"runner-{task_id[:6]}",
            "receipt": {"workflow_revision_hash": "rev-test", "workflow_revision": "rev-test",
                        "prompt_sha256": hashlib.sha256(prompt.encode()).hexdigest(),
                        "outputs": [{"sha256": sha, "source_path": str(video), "intermediate": False}]},
        }
        return {"task_id": task_id, "status": "starting"}

    def start(self, _task_id):
        pass

    def get(self, task_id):
        return self.jobs[task_id]


_ACESTEP_STUB = textwrap.dedent(
    """
    import json, math, struct, sys, wave
    from pathlib import Path
    job = json.load(open(sys.argv[1]))
    out = Path(job["out"]); out.mkdir(parents=True, exist_ok=True)
    rate = 22050
    audio = out / "music.wav"
    with wave.open(str(audio), "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(rate)
        w.writeframes(b"".join(struct.pack("<h", int(6000 * math.sin(2 * math.pi * 330 * n / rate)))
                               for n in range(int(rate * (float(job["duration"]) + 0.5)))))
    (out / "metrics.json").write_text(json.dumps({"audio_path": str(audio)}))
    """
)


@pytest.fixture
def pipeline(tmp_path, monkeypatch):
    settings = Settings(_env_file=None, home=tmp_path / "home")
    for module in (managed, tts_service):
        monkeypatch.setattr(module, "get_settings", lambda: settings)
    monkeypatch.setattr(tts_service, "get_provider", lambda s=None: _FakeSupertonic(s or settings))
    lab = tmp_path / "acestep-root"
    lab.mkdir()
    runner = tmp_path / "run_acestep_job.py"
    runner.write_text(_ACESTEP_STUB)
    monkeypatch.setattr(managed, "ACESTEP_ROOT", lab)
    monkeypatch.setattr(managed, "ACESTEP_PYTHON", Path(sys.executable))
    monkeypatch.setattr(managed, "ACESTEP_JOB_RUNNER", runner)

    plans = PlanStore(tmp_path / "plans", registry=Registry())
    plan = plans.create(build_plan("가을 산책로를 소개하는 짧은 영상", _intent(), registry=Registry()))
    plan = plans.approve(plan["id"], plan["revision"])
    controller = _FakeController(tmp_path)
    generator = managed.ManagedAssetGenerator(controller=controller, store=DirectorStore(tmp_path / "productions"))
    monkeypatch.setattr(generator, "blockers", lambda _plan: [])  # 호스트 모델·장치 점검은 호스트 preflight 의 몫
    return settings, plans, plan, generator, controller, Studio(tmp_path / "studio")


def _run_renderer(studio: Studio) -> None:
    for _ in range(40):
        pending = [j for p in studio.list() for j in p["render_jobs"] if j["status"] in ("queued", "running")]
        if not pending:
            return
        asyncio.run(scan(studio))
    raise AssertionError("렌더러가 큐를 비우지 못했습니다.")


def test_generator_runs_every_stage_with_real_settings_and_feeds_dispatch(pipeline):
    settings, plans, plan, generator, controller, studio = pipeline

    result = asset_generation.generate_assets(plans, plan["id"], plan["revision"], generator)

    assert result["status"] == "succeeded", result.get("error")
    scene_ids = [scene["scene_id"] for scene in plan["scenes"]]
    assert set(result["assets"]["scenes"]) == set(scene_ids) == set(result["assets"]["voices"])
    assert result["receipt"]["execution_mode"] == "real"
    assert [run["scene_id"] for run in result["receipt"]["engine_runs"]] == scene_ids
    assert controller.waits == [managed.LEASE_HANDOFF_WAIT_SECONDS] * 3
    assert len(result["receipt"]["voices"]) == 3
    assert result["receipt"]["music"]["execution_mode"] == "real"
    # ACE-Step 자식이 끝난 뒤 lease 가 반납돼 다음 작업이 바로 얻을 수 있어야 한다.
    assert ResourceLeaseManager(data_dir=settings.data_dir).current() is None
    json.dumps(result["receipt"])  # production.json 에 그대로 기록 가능한 receipt

    stored = dispatch.ProductionStore(plans).read(plan["id"])
    assert stored["asset_generations"][-1]["status"] == "succeeded"
    music = stored["assets"]["music"]
    assert music["meta"]["music_engine"] == "ACE-Step 1.5"
    assert music["meta"]["duration"] + 0.1 >= plan["scenes"][-1]["end_time"]

    # 생성 결과가 dispatch 입력 조건(장면 이미지 전부 + 전체 길이 음악)을 실제로 만족해 렌더까지 간다.
    dispatch.dispatch(plans, studio, plan["id"], plan["revision"], caption_mode="narration")
    dispatch_record = dispatch.ProductionStore(plans).read(plan["id"])["dispatches"][-1]
    assert dispatch_record["caption_mode"] == "narration"
    assert dispatch_record["caption_timing"].endswith("no word-level alignment")
    assert set(dispatch_record["caption_sources"].values()) == {"narration"}
    assert dispatch.view(plans, studio, plan["id"])["state"] == "ready_to_render"
    dispatch.request_render(plans, studio, plan["id"], plan["revision"])
    _run_renderer(studio)
    done = dispatch.view(plans, studio, plan["id"])
    assert done["state"] == "succeeded", done
    path, _name = dispatch.final_media(plans, plan["id"], done["final"]["id"])
    info = inspect_media(path)
    assert info["has_video"] and info["has_audio"]
    project_id = done["segments"][0]["project_id"]
    rendered_project = studio.get(project_id)
    assert [cue["text"] for cue in rendered_project["timeline"]["captions"]] == [
        cue["text"] for scene in plan["scenes"]
        for cue in rendered_project["scenes"][plan["scenes"].index(scene)]["caption_segments"]
    ]
    srt = (studio.path(project_id) / "renders" / f"{done['segments'][0]['job_id']}.srt").read_text()
    first_cue_end = rendered_project["scenes"][0]["caption_segments"][0]["end"]
    expected_end_ms = round(first_cue_end * 1000)
    assert "첫 번째 장면에서는" in srt and f"00:00:00,{expected_end_ms % 1000:03}" in srt


def test_scene_prompt_is_specific_and_receipt_keeps_the_exact_comfy_input(pipeline):
    _settings, _plans, plan, generator, controller, _studio = pipeline

    prompts = [generator._prompt(plan, scene) for scene in plan["scenes"]]

    assert "장면 2:" not in prompts[0]
    assert "장면 3:" not in prompts[0]
    assert "장면 2의 설명" not in prompts[0]
    assert "장면 1의 설명" in prompts[1]
    assert "장면 2의 설명" not in prompts[1]
    assert "장면 2의 설명" in prompts[2]
    result = asset_generation.generate_assets(_plans, plan["id"], plan["revision"], generator)
    runs = result["receipt"]["engine_runs"]
    assert [run["prompt"] for run in runs] == prompts
    assert all(run["prompt_sha256"] == hashlib.sha256(run["prompt"].encode()).hexdigest() for run in runs)
    assert all(run["receipt"]["prompt_sha256"] == run["prompt_sha256"] for run in runs)
    assert controller.prompts == prompts


def test_scene_prompt_refuses_to_silently_truncate_required_visual_details(pipeline):
    _settings, _plans, _plan, generator, _controller, _studio = pipeline
    plan = {"brief": "도시 다큐멘터리", "name": "도시 다큐멘터리"}
    scene = {"scene_id": "s1", "title": "골목", "narration": "푸른 새벽빛과 가로등의 상세 묘사 " * 90}

    with pytest.raises(ValueError, match="1000자를 넘습니다"):
        generator._prompt(plan, scene)


def test_seoul_sample_scene_brief_keeps_each_visual_target_separate(pipeline):
    _settings, _plans, plan, generator, _controller, _studio = pipeline
    plan = dict(plan)
    plan["brief"] = (
        "서울 새벽 3장면. 장면 1: 푸른 새벽빛 골목과 꺼지는 가로등. "
        "장면 2: 첫 버스와 환경미화원이 거리를 지나간다. "
        "장면 3: 한강 수면에 떠오르는 해와 금빛 윤슬."
    )
    scenes = [
        {"scene_id": "s1", "title": "새벽 골목", "narration": "가로등이 하나둘 꺼집니다."},
        {"scene_id": "s2", "title": "도시가 깨어난다", "narration": "첫 버스와 청소 인력이 거리를 엽니다."},
        {"scene_id": "s3", "title": "한강의 일출", "narration": "한강 수면에 금빛 윤슬이 번집니다."},
    ]

    prompts = [generator._prompt(plan, scene) for scene in scenes]

    assert "푸른 새벽빛 골목" in prompts[0] and "첫 버스" not in prompts[0]
    assert "첫 버스와 환경미화원" in prompts[1] and "한강 수면" not in prompts[1]
    assert "한강 수면" in prompts[2] and "꺼지는 가로등" not in prompts[2]


def test_caption_compiler_wraps_korean_for_selected_style_and_uses_estimated_interval():
    from ttakkari.creative.render import compile_caption_segments

    text = "한강 위로 해가 떠오르며 금빛 윤슬이 번집니다. 서울의 하루가 시작되고 강변의 도시는 천천히 깨어납니다."
    width = 720

    cues = compile_caption_segments(text, 4.2, width, style="center_highlight")

    assert "".join(cue["text"].replace("\n", "") for cue in cues).replace(" ", "") == text.replace(" ", "")
    assert cues[0]["start"] == 0
    assert cues[-1]["end"] == pytest.approx(4.2)
    assert all(len(cue["text"].splitlines()) <= 2 for cue in cues)
    font = ImageFont.truetype("/System/Library/Fonts/AppleSDGothicNeo.ttc", round(width * 0.052))
    draw = ImageDraw.Draw(Image.new("RGB", (1, 1)))
    assert all(draw.textlength(line, font=font) <= width * 0.85 for cue in cues for line in cue["text"].splitlines())


def test_dispatch_narration_captions_use_scene_voice_duration_and_title_only_is_explicit(pipeline):
    _settings, plans, plan, generator, _controller, studio = pipeline
    generated = asset_generation.generate_assets(plans, plan["id"], plan["revision"], generator)

    dispatch.dispatch(plans, studio, plan["id"], plan["revision"], caption_mode="narration")
    narration_pid = dispatch._create_project(
        studio,
        dispatch.ProductionStore(plans),
        plan,
        plan["render_plan"]["segments"][0],
        dispatch.ProductionStore(plans).read(plan["id"]),
        {"applied": {}},
        caption_mode="narration",
    )
    narration_project = studio.get(narration_pid)
    for plan_scene, rendered_scene in zip(plan["scenes"], narration_project["scenes"], strict=True):
        voice_seconds = generated["assets"]["voices"][plan_scene["scene_id"]]["meta"]["duration"]
        compiled = "".join(segment["text"].replace("\n", "") for segment in rendered_scene["caption_segments"])
        assert compiled.replace(" ", "") == plan_scene["narration"].replace(" ", "")
        assert rendered_scene["caption_segments"]
        assert rendered_scene["caption_segments"][-1]["end"] == pytest.approx(voice_seconds)

    title_pid = dispatch._create_project(
        studio,
        dispatch.ProductionStore(plans),
        plan,
        plan["render_plan"]["segments"][0],
        dispatch.ProductionStore(plans).read(plan["id"]),
        {"applied": {}},
        caption_mode="title_only",
    )
    title_project = studio.get(title_pid)
    assert [scene["caption"] for scene in title_project["scenes"]] == [scene["title"] for scene in plan["scenes"]]
    assert all(not scene["caption_segments"] for scene in title_project["scenes"])

    no_narration_plan = {**plan, "scenes": [dict(scene, narration="") for scene in plan["scenes"]]}
    fallback_pid = dispatch._create_project(
        studio,
        dispatch.ProductionStore(plans),
        no_narration_plan,
        plan["render_plan"]["segments"][0],
        dispatch.ProductionStore(plans).read(plan["id"]),
        {"applied": {}},
        caption_mode="narration",
    )
    fallback_project = studio.get(fallback_pid)
    assert [scene["caption"] for scene in fallback_project["scenes"]] == [scene["title"] for scene in plan["scenes"]]
    assert all(not scene["caption_segments"] for scene in fallback_project["scenes"])


def test_tts_scene_script_limit_uses_a_real_settings_field(pipeline):
    settings, _plans, _plan, generator, _controller, _studio = pipeline
    assert hasattr(settings, "local_tts_max_text_chars")

    with pytest.raises(RuntimeError, match=str(settings.local_tts_max_text_chars)):
        generator._synthesize_voice("가" * (settings.local_tts_max_text_chars + 1), Path("/nonexistent"))


def test_blockers_runs_against_real_settings_without_attribute_errors(pipeline, monkeypatch):
    _settings, _plans, plan, _generator, _controller, _studio = pipeline
    generator = managed.ManagedAssetGenerator()
    monkeypatch.setattr(generator, "_probe_comfy_torch", lambda: {"mps_available": True})

    reasons = generator.blockers(plan)

    assert isinstance(reasons, list)
    assert not any("AttributeError" in reason for reason in reasons)


class _LoopBoundRuntime:
    """운영 세대 모드의 generation.current 처럼 work() 가 만든 자원이 서버 루프에만 유효하다(DB 풀 대용)."""

    def __init__(self, loop):
        self.loop = loop

    @asynccontextmanager
    async def work(self, _name):
        if asyncio.get_running_loop() is not self.loop:
            raise RuntimeError("Future attached to a different loop")
        yield True


async def test_tts_budget_runs_on_the_server_loop_when_generation_runtime_is_active(pipeline, monkeypatch, tmp_path):
    """generate()는 worker thread 에서 돈다. 새 루프에서 서버 DB 풀을 쓰면 'attached to a different loop' 로 실패한다."""
    _settings, _plans, _plan, _generator, _controller, _studio = pipeline
    server_loop = asyncio.get_running_loop()
    monkeypatch.setattr(generation, "current", _LoopBoundRuntime(server_loop))
    generator = managed.ManagedAssetGenerator(loop=server_loop)

    path, receipt = await asyncio.to_thread(generator._synthesize_voice, "안녕하세요. 테스트 문장입니다.", tmp_path / "tts")

    assert path.is_file() and receipt["real_execution"] is True


def test_caption_contract_distinguishes_title_only_from_narration_subtitles():
    scenes = [
        {"scene_id": "s1", "title": "새벽 골목", "narration": "가로등이 꺼집니다."},
        {"scene_id": "s2", "title": "도시가 깨어난다", "narration": ""},
    ]
    plan = {"scenes": scenes, "match": {"selections": [{"entry_id": "C10.subtitles"}]}}

    title_only = dispatch._caption_contract(plan, "title_only")
    assert title_only["plan_selects_subtitles"] is True
    assert title_only["title_caption_scene_ids"] == ["s1", "s2"]
    assert "C10.subtitles" in title_only["notices"][0] and "narration" in title_only["notices"][0]

    narration = dispatch._caption_contract(plan, "narration")
    assert narration["narration_scene_ids"] == ["s1"]
    assert narration["title_caption_scene_ids"] == ["s2"]
    assert "s2" in narration["notices"][0]

    silent = dispatch._caption_contract({"scenes": [dict(s, narration="") for s in scenes], "match": {}}, "narration")
    assert silent["plan_selects_subtitles"] is False
    assert any("모든 장면" in notice for notice in silent["notices"])


def test_title_only_dispatch_renders_and_stays_current_and_late_mode_change_is_reported(pipeline):
    _settings, plans, plan, generator, _controller, studio = pipeline
    asset_generation.generate_assets(plans, plan["id"], plan["revision"], generator)

    first = dispatch.dispatch(plans, studio, plan["id"], plan["revision"], caption_mode="title_only")
    assert first["caption_contract"]["mode"] == "title_only"
    assert first["caption_contract"]["notices"]
    assert "caption_mode_request_ignored" not in first

    again = dispatch.dispatch(plans, studio, plan["id"], plan["revision"], caption_mode="narration")
    assert again["caption_mode"] == "title_only"
    assert "title_only" in again["caption_mode_request_ignored"]

    dispatch.request_render(plans, studio, plan["id"], plan["revision"])
    _run_renderer(studio)
    done = dispatch.view(plans, studio, plan["id"])
    # title_only run_key 와 같은 방식으로 stale 여부를 계산해야 성공 렌더가 다시 렌더 대상으로 보이지 않는다.
    assert done["outdated_segments"] == []
    assert done["state"] == "succeeded", done
