from pathlib import Path

import pytest
from PIL import Image

from ttakkari.creative.v2 import asset_generation
from ttakkari.creative.v2.asset_generation_managed import ManagedAssetGenerator
from ttakkari.creative.v2.intent import InfoItem, IntentSpec, Selection
from ttakkari.creative.v2.plans import PlanStore, build_plan
from ttakkari.creative.v2.registry import Registry

BRIEF = "3막 구조의 짧은 설명 영상"


def make_intent():
    return IntentSpec(
        selections=[
            Selection(entry_id="C08.three_act", role="primary", confidence=1.0),
            Selection(entry_id="C10.dissolve", role="secondary", confidence=1.0),
        ],
        info_items=[
            InfoItem(id=f"scene-{i}", title=f"장면 {i}", narration=f"장면 {i}의 설명")
            for i in range(3)
        ],
    )


class FakeGenerator:
    engine_id = "test.generated-scenes"
    version = "test-1"

    def __init__(self, root: Path):
        self.root = root
        self.calls = 0

    def supported_recipes(self):
        return {"R21"}

    def blockers(self, plan):
        return []

    def generate(self, plan, *, workdir, progress):
        self.calls += 1
        scenes = {}
        voices = {}
        for index, scene in enumerate(plan["scenes"]):
            progress({"stage": "scene_succeeded", "scene_id": scene["scene_id"], "task_id": f"task-{index}"})
            path = workdir / f"{scene['scene_id']}.png"
            Image.new("RGB", (320, 180), (40 + index * 30, 70, 110)).save(path)
            scenes[scene["scene_id"]] = {
                "path": path,
                "name": path.name,
                "meta": {"width": 320, "height": 180, "engine": self.engine_id},
            }
            voice = workdir / f"{scene['scene_id']}-voice.wav"
            voice.write_bytes(f"voice-{index}".encode())
            voices[scene["scene_id"]] = {
                "path": voice,
                "name": voice.name,
                "meta": {"scene_id": scene["scene_id"], "engine": "test.tts"},
            }
        music = workdir / "mix.wav"
        music.write_bytes(b"generated master mix")
        return {
            "scenes": scenes,
            "voices": voices,
            "music": {
                "path": music,
                "name": music.name,
                "meta": {"duration": plan["scenes"][-1]["end_time"], "engine": "test.music"},
            },
            "receipt": {
                "execution_mode": "real",
                "engines": [{"id": self.engine_id, "version": self.version}],
                "voice_sha256": "a" * 64,
            },
        }


def approved_plan(tmp_path):
    store = PlanStore(tmp_path / "plans", registry=Registry())
    plan = store.create(build_plan(BRIEF, make_intent(), registry=Registry()))
    assert plan["match"]["primary_recipe"] == "R21"
    return store, store.approve(plan["id"], plan["revision"])


def test_asset_generation_requires_approved_current_renderable_revision(tmp_path):
    plans = PlanStore(tmp_path / "plans", registry=Registry())
    plan = plans.create(build_plan(BRIEF, make_intent(), registry=Registry()))
    generator = FakeGenerator(tmp_path)

    with pytest.raises(asset_generation.AssetGenerationError, match="승인"):
        asset_generation.generate_assets(plans, plan["id"], plan["revision"], generator)

    assert generator.calls == 0


def test_asset_generation_blocks_unsupported_recipe_without_slideshow_fallback(tmp_path):
    plans, plan = approved_plan(tmp_path)
    updated = dict(plan)
    updated["match"] = {**plan["match"], "primary_recipe": "R01"}
    original_get = plans.get
    plans.get = lambda plan_id: updated if plan_id == plan["id"] else original_get(plan_id)
    generator = FakeGenerator(tmp_path)

    with pytest.raises(asset_generation.AssetGenerationError, match="R01"):
        asset_generation.generate_assets(plans, plan["id"], plan["revision"], generator)

    assert generator.calls == 0
    assert asset_generation.ProductionStore(plans).read(plan["id"])["asset_generations"][-1]["status"] == "failed"


def test_asset_generation_registers_every_scene_and_audio_with_engine_receipt(tmp_path):
    plans, plan = approved_plan(tmp_path)
    generator = FakeGenerator(tmp_path)

    result = asset_generation.generate_assets(plans, plan["id"], plan["revision"], generator)

    assert generator.calls == 1
    assert set(result["assets"]["scenes"]) == {scene["scene_id"] for scene in plan["scenes"]}
    assert result["assets"]["music"]["meta"]["engine"] == "test.music"
    assert set(result["assets"]["voices"]) == {scene["scene_id"] for scene in plan["scenes"]}
    assert result["receipt"]["voice_sha256"] == "a" * 64
    stored = asset_generation.ProductionStore(plans).read(plan["id"])
    assert {
        scene_id: item["sha256"] for scene_id, item in stored["assets"]["scenes"].items()
    } == {
        scene_id: item["sha256"] for scene_id, item in result["assets"]["scenes"].items()
    }
    assert stored["assets"]["music"]["sha256"] == result["assets"]["music"]["sha256"]
    assert stored["asset_generations"][-1]["status"] == "succeeded"
    assert [event["stage"] for event in stored["asset_generations"][-1]["progress"]] == [
        "scene_succeeded", "scene_succeeded", "scene_succeeded"
    ]


def test_managed_generator_blocks_comfy_when_torch_has_no_usable_device(monkeypatch):
    generator = ManagedAssetGenerator()
    monkeypatch.setattr(
        generator,
        "_probe_comfy_torch",
        lambda: {"cuda_available": False, "mps_available": False, "error": None},
    )

    blockers = generator.blockers({"aspect": "16:9", "scenes": []})

    assert any("Comfy PyTorch에서 CUDA 또는 MPS 장치를 사용할 수 없습니다" in item for item in blockers)
