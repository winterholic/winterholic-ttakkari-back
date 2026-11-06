"""Core contracts: reviewed direction, motion, explicit blockers and incremental reuse."""

import subprocess

import pytest
from test_creative_v2_asset_generation_e2e import pipeline as _pipeline_fixture
from test_creative_v2_dispatch import (
    approved,
    intent,
    run_renderer,
    upload_all,
)
from test_creative_v2_dispatch import (
    stores as _stores_fixture,
)

from ttakkari.creative.render import ffmpeg, inspect_media
from ttakkari.creative.v2 import dispatch, engines
from ttakkari.creative.v2.plans import build_plan


@pytest.fixture
def stores(tmp_path):
    return _stores_fixture.__wrapped__(tmp_path)


@pytest.fixture
def pipeline(tmp_path, monkeypatch):
    return _pipeline_fixture.__wrapped__(tmp_path, monkeypatch)


def direction(strategies=("frame_animation_2d", "scene_3d", "generative_video")):
    presets = {"frame_animation_2d": "walk_cycle", "scene_3d": "crystal_orbit"}
    return {
        "source": "llm",
        "model": "reviewed-fixture",
        "reviewed": True,
        "intent": "명시한 프리셋과 원본 모션을 섞어 조립한다",
        "domain_knowledge": [
            "걷기는 홀드 프레임, 공간 모션은 3D, 원본 영상은 재생한다"
        ],
        "scenes": [
            {
                "scene_id": f"i{i}",
                "strategy": strategy,
                "preset": presets.get(strategy),
                "rationale": "승인할 스토리보드의 제작 전략",
                "shots": [
                    {
                        "shot_id": f"shot-{i}",
                        "action": "프리셋 또는 제공된 영상 사용",
                        "layers": ["visual", "captions", "audio"],
                    }
                ],
            }
            for i, strategy in enumerate(strategies)
        ],
    }


def test_reviewed_direction_is_in_plan_and_changes_segment_keys():
    from ttakkari.creative.v2.planner import ProductionDirection
    from ttakkari.creative.v2.plans import render_plan

    plan = build_plan(
        "사진으로 영상",
        intent(),
        director=ProductionDirection.model_validate(direction()),
    )
    assert [s["strategy"] for s in plan["scenes"]] == [
        "frame_animation_2d",
        "scene_3d",
        "generative_video",
    ]
    assert len(render_plan(plan)["segments"]) == 3
    for scene in plan["scenes"]:
        assert scene["shots"][0]["selected_providers"]
        assert scene["shots"][0]["cost_latency_quality"]["measured"] is False
        assert scene["strategy_rationale"]["alternatives"]
    with pytest.raises(ValueError, match="reviewed"):
        build_plan(
            "写真",
            intent(),
            director=ProductionDirection.model_validate(
                {**direction(), "reviewed": False}
            ),
        )


def test_engines_are_discovered_but_unsupported_contracts_fail_closed():
    from ttakkari.creative.v2.planner import ProductionDirection

    assert engines.adapter_by_id("skill.frame_animation_2d")
    assert engines.adapter_by_id("skill.studio-3d-cinematic")
    hf = engines.adapter_by_id("skill.hyperframes_motion")
    assert hf and not hf.preflight().ok
    assert "render_segment" in " ".join(hf.preflight().missing)
    plan = build_plan(
        "사진", intent(), director=ProductionDirection.model_validate(direction())
    )
    plan["scenes"][0]["preset"] = "arbitrary_character"
    from ttakkari.creative.v2.plans import render_plan

    plan["render_plan"] = render_plan(plan)
    plan["render_gate"] = True
    assert any("preset" in r for r in dispatch.dispatch_blockers(plan))


def test_video_asset_is_forwarded_as_video_and_retains_motion(stores, tmp_path):
    plans, studio = stores
    plan = approved(plans)
    upload_all(plans, plan, tmp_path)
    video = tmp_path / "moving.mp4"
    subprocess.run(
        [
            ffmpeg(),
            "-v",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=s=1280x720:r=24",
            "-t",
            "1",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            str(video),
        ],
        check=True,
        capture_output=True,
    )
    asset = dispatch.put_asset(
        plans,
        plan["id"],
        kind="scene",
        scene_id=plan["scenes"][0]["scene_id"],
        source=video,
        name="moving.mp4",
        meta=inspect_media(video),
    )
    assert asset["kind"] == "video"
    dispatch.dispatch(plans, studio, plan["id"], 1)
    queued = dispatch.request_render(plans, studio, plan["id"], 1)
    project = studio.get(queued["segments"][0]["project_id"])
    source = next(a for a in project["assets"] if a["sha256"] == asset["sha256"])
    assert source["kind"] == "video"
    run_renderer(studio)
    result = dispatch.view(plans, studio, plan["id"])
    assert result["state"] == "succeeded", result
    path, _ = dispatch.final_media(plans, plan["id"], result["final"]["id"])
    frames = []
    for seconds in (0.3, 0.7):
        frame = subprocess.run(
            [
                ffmpeg(),
                "-v",
                "error",
                "-ss",
                str(seconds),
                "-i",
                str(path),
                "-frames:v",
                "1",
                "-vf",
                "crop=1280:400:0:0",
                "-f",
                "md5",
                "-",
            ],
            capture_output=True,
            check=True,
            text=True,
        ).stdout
        frames.append(frame)
    assert frames[0] != frames[1], "source motion was flattened to a still"
    assert result["quality_report"]["semantic_qa"]["verdict"] == "unknown"
    assert result["quality_report"]["viewing_qa"]["verdict"] == "unknown"


def test_preflight_false_without_missing_still_blocks(stores, monkeypatch):
    plans, _ = stores
    plan = approved(plans)
    monkeypatch.setattr(
        engines.V1SlideshowAdapter,
        "preflight",
        lambda _: engines.Preflight(False, None, []),
    )
    assert dispatch.dispatch_blockers(plan)


def test_directed_video_dispatch_reuses_unedited_scene_after_revision(stores, tmp_path):
    from ttakkari.creative.v2.duration import SceneEdit
    from ttakkari.creative.v2.planner import ProductionDirection

    plans, studio = stores
    plan = approved(
        plans,
        director=ProductionDirection.model_validate(
            direction(("generative_video",) * 3)
        ),
    )
    upload_all(plans, plan, tmp_path)
    video = tmp_path / "moving.mp4"
    subprocess.run(
        [
            ffmpeg(),
            "-v",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=s=1280x720:r=24",
            "-t",
            "1",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            str(video),
        ],
        check=True,
        capture_output=True,
    )
    for scene in plan["scenes"]:
        dispatch.put_asset(
            plans,
            plan["id"],
            kind="scene",
            scene_id=scene["scene_id"],
            source=video,
            name=video.name,
            meta=inspect_media(video),
        )
    dispatch.dispatch(plans, studio, plan["id"], 1)
    first = dispatch.request_render(plans, studio, plan["id"], 1)
    assert first["state"] == "succeeded", first
    before = dispatch.ProductionStore(plans).read(plan["id"])
    edited = plans.edit(
        plan["id"],
        1,
        scenes=[
            SceneEdit(
                id=s["scene_id"],
                title=s["title"] + (" 수정" if i == 2 else ""),
                narration=s["narration"],
                seconds=s["seconds"],
                info_weight=s["info_weight"],
                essential=s["essential"],
            )
            for i, s in enumerate(plan["scenes"])
        ],
    )
    assert edited["render_gate"] is False
    plans.approve(plan["id"], 2)
    dispatch.dispatch(plans, studio, plan["id"], 2)
    done = dispatch.request_render(plans, studio, plan["id"], 2)
    assert done["state"] == "succeeded", done
    after = dispatch.ProductionStore(plans).read(plan["id"])
    assert before["assets"] == after["assets"]
    for old, new in zip(
        before["dispatches"][0]["segments"][:2],
        after["dispatches"][1]["segments"][:2],
        strict=True,
    ):
        assert new["cached_from_previous_revision"] is True
        assert (
            new["run_key"] == old["run_key"]
            and new["output"]["sha256"] == old["output"]["sha256"]
        )


def test_managed_regeneration_reuses_successful_scenes_after_one_edit(pipeline):
    from ttakkari.creative.v2 import asset_generation
    from ttakkari.creative.v2.duration import SceneEdit

    _, plans, plan, generator, controller, _ = pipeline
    first = asset_generation.generate_assets(plans, plan["id"], 1, generator)
    assert len(controller.prompts) == 3
    edited = plans.edit(
        plan["id"],
        1,
        scenes=[
            SceneEdit(
                id=s["scene_id"],
                title=s["title"],
                narration=s["narration"] + (" 수정" if i == 2 else ""),
                seconds=s["seconds"],
                info_weight=s["info_weight"],
                essential=s["essential"],
            )
            for i, s in enumerate(plan["scenes"])
        ],
    )
    plans.approve(plan["id"], edited["revision"])
    second = asset_generation.generate_assets(plans, plan["id"], 2, generator)
    assert len(controller.prompts) == 4, (
        "only changed scene should invoke managed Comfy"
    )
    for sid in ("scene-0", "scene-1"):
        assert (
            first["assets"]["scenes"][sid]["sha256"]
            == second["assets"]["scenes"][sid]["sha256"]
        )
    assert second["assets"]["scenes"]["scene-0"]["kind"] == "video"
    assert second["assets"]["scenes"]["scene-0"]["preview_image_file"]


def test_directed_shot_capability_cannot_claim_lipsync(stores):
    from ttakkari.creative.v2.planner import ProductionDirection

    plans, _ = stores
    raw = direction(("generative_video",) * 3)
    raw["scenes"][0]["shots"][0]["required_capabilities"] = ["anim.lipsync"]
    plan = approved(plans, director=ProductionDirection.model_validate(raw))
    assert any("anim.lipsync" in reason for reason in dispatch.dispatch_blockers(plan))


def test_directed_native_segment_detects_lost_or_corrupt_output():
    record = {"log": []}
    segment = {
        "id": "seg-01",
        "project_id": None,
        "job_id": "native",
        "status": "succeeded",
        "output": {"file": "/no/such/core-output.mp4", "sha256": "a" * 64},
    }
    dispatch._sync_segment(None, record, segment)
    assert segment["status"] == "stale"


def test_real_frame_producer_is_called_through_studio_dispatch(
    stores, tmp_path, monkeypatch
):
    from ttakkari.creative.v2.duration import DurationRequest
    from ttakkari.creative.v2.planner import ProductionDirection

    plans, studio = stores
    short = intent().model_copy(
        update={
            "info_items": [
                i.model_copy(update={"narration": ""}) for i in intent().info_items
            ]
        }
    )
    plan = approved(
        plans,
        intent=short,
        duration=DurationRequest(mode="preferred", seconds=24),
        director=ProductionDirection.model_validate(
            direction(("frame_animation_2d", "slideshow", "slideshow"))
        ),
    )
    upload_all(plans, plan, tmp_path)
    # A producer scene needs no uploaded image: its actual render supplies video.
    with dispatch.ProductionStore(plans).tx(plan["id"]) as data:
        data["assets"]["scenes"].pop("i0")
    dispatch.dispatch(plans, studio, plan["id"], 1)
    result = dispatch.request_render(plans, studio, plan["id"], 1)
    assert result["state"] == "succeeded", result
    record = dispatch.ProductionStore(plans).read(plan["id"])["dispatches"][0]
    assert record["segments"][0]["producer_output"]["receipt"]["real_execution"] is True
    assert record["segments"][0]["engine"]["adapter"] == "skill.frame_animation_2d"
    assert result["quality_report"]["overall_verdict"] == "unknown"


def test_producer_runtime_failure_never_falls_back_to_slideshow(
    stores, tmp_path, monkeypatch
):
    from ttakkari.creative.v2.planner import ProductionDirection

    plans, studio = stores
    plan = approved(
        plans,
        director=ProductionDirection.model_validate(
            direction(("frame_animation_2d", "slideshow", "slideshow"))
        ),
    )
    upload_all(plans, plan, tmp_path)

    def fail(*args, **kwargs):
        raise RuntimeError("producer failed deliberately")

    monkeypatch.setattr(engines.ModuleAdapter, "render_segment", fail)
    dispatch.dispatch(plans, studio, plan["id"], 1)
    result = dispatch.request_render(plans, studio, plan["id"], 1)
    assert (
        result["state"] == "failed"
        and "producer failed deliberately" in result["reasons"][0]
    )
    assert result["segments"][0]["attempts"] == 1 and result["final"] is None
    assert all(s["status"] == "succeeded" for s in result["segments"][1:])
    assert not studio.list(), "a producer failure must not create fallback V1 projects"


def test_director_edit_requires_new_approval_and_preserves_scene_timing(stores):
    from ttakkari.creative.v2.planner import ProductionDirection

    plans, _ = stores
    plan = approved(
        plans,
        director=ProductionDirection.model_validate(
            direction(("generative_video",) * 3)
        ),
    )
    changed = direction(("slideshow", "generative_video", "generative_video"))
    edited = plans.edit(
        plan["id"], 1, director=ProductionDirection.model_validate(changed)
    )
    assert edited["revision"] == 2 and edited["render_gate"] is False
    assert [s["seconds"] for s in edited["scenes"]] == [
        s["seconds"] for s in plan["scenes"]
    ]
    assert edited["scenes"][0]["selected_engine"] == "skill.v1_ffmpeg_slideshow"
    assert plans.approve(plan["id"], 2)["render_gate"] is True


def test_repair_proposal_preserves_other_scene_assets(stores, tmp_path):
    plans, studio = stores
    plan = approved(plans)
    upload_all(plans, plan, tmp_path)
    dispatch.dispatch(plans, studio, plan["id"], 1)
    before = dispatch.ProductionStore(plans).read(plan["id"])["assets"]
    result = dispatch.plan_repair(
        plans,
        plan["id"],
        1,
        [{"scene_id": "i0", "code": "character_consistency", "layer": "visual"}],
    )
    assert result["invalidate"]["scene_ids"] == ["i0"]
    assert "scenes.i1" in result["preserve"]["asset_keys"]
    assert dispatch.ProductionStore(plans).read(plan["id"])["assets"] == before


def test_compositor_source_version_invalidates_segment_cache(
    stores, tmp_path, monkeypatch
):
    plans, _ = stores
    plan = approved(plans)
    upload_all(plans, plan, tmp_path)
    data = dispatch.ProductionStore(plans).read(plan["id"])
    segment = plan["render_plan"]["segments"][0]
    engine = {"adapter": "test", "version": "1"}
    first = dispatch._run_key(plan, segment, data, engine, {"applied": {}})
    monkeypatch.setattr(dispatch, "COMPOSITOR_VERSION", "changed-source")
    assert first != dispatch._run_key(plan, segment, data, engine, {"applied": {}})


def test_native_render_does_not_run_on_remote_api_process(stores, monkeypatch):
    from ttakkari import queued_work
    from ttakkari.creative.v2.planner import ProductionDirection

    plans, studio = stores
    plan = approved(
        plans,
        director=ProductionDirection.model_validate(
            direction(("generative_video",) * 3)
        ),
    )
    monkeypatch.setattr(queued_work, "delegated", lambda: True)
    with pytest.raises(dispatch.ProductionError, match="executor"):
        dispatch.request_render(plans, studio, plan["id"], 1)


def test_3d_boundary_projects_editorial_spec_to_strict_preset_contract(
    tmp_path, monkeypatch
):
    from types import SimpleNamespace

    from ttakkari.creative.v2.scene_3d import _normalize_render_input

    adapter = engines.adapter_by_id("skill.studio-3d-cinematic")
    monkeypatch.setattr(
        adapter.delegate, "audit_scene", lambda _: SimpleNamespace(ok=True)
    )

    def accept(spec, **kwargs):
        normalized = _normalize_render_input(spec)
        assert normalized["scene"]["scene_type"] == "crystal_orbit"
        raise RuntimeError("strict producer input accepted")

    monkeypatch.setattr(adapter.delegate, "render_segment", accept)
    with pytest.raises(RuntimeError, match="strict producer input accepted"):
        adapter.render_segment(
            {
                "plan_id": "p",
                "revision": 1,
                "segment_id": "s",
                "seconds": 8,
                "fps": 30,
                "width": 1280,
                "height": 720,
                "aspect": "16:9",
                "style_dna": {"lighting": "editorial only"},
                "editorial_scenes": [],
                "scenes": [
                    {
                        "scene_id": "s",
                        "seconds": 8,
                        "preset": "crystal_orbit",
                        "title": "editorial title",
                        "camera": [],
                        "motion": [],
                        "shots": [],
                    }
                ],
            },
            output_path=tmp_path / "unused.mp4",
            workdir=tmp_path,
        )


def test_legacy_adapter_selection_does_not_import_optional_producers():
    import sys

    code = """
import sys
from ttakkari.creative.v2 import engines
assert engines.adapter_for({'skill.v1_ffmpeg_slideshow', 'tool.ffmpeg'})
assert 'ttakkari.creative.v2.frame_animation' not in sys.modules
"""
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr


def test_hyperframes_limits_match_current_kafka_template():
    adapter = engines.adapter_by_id("skill.hyperframes_motion")
    assert adapter.limits.aspects == ("16:9",)
    assert adapter.limits.min_scenes == adapter.limits.max_scenes == 3


def test_native_compositor_keeps_v1_local_protocol_boundary(tmp_path, monkeypatch):
    from types import SimpleNamespace
    captured = []
    def deny(args, **kwargs):
        captured.extend(args)
        return SimpleNamespace(returncode=1, stderr="deliberate fixture failure")
    monkeypatch.setattr(engines.subprocess, "run", deny)
    with pytest.raises(RuntimeError, match="deliberate fixture failure"):
        engines.compose_segment(tmp_path / "source.mp4", tmp_path / "music.wav", tmp_path / "result.mp4",
                                aspect="16:9", seconds=3, captions=[])
    assert "-format_whitelist" in captured
    assert captured.count("-protocol_whitelist") >= 2
    assert captured[captured.index("-protocol_whitelist") + 1] == "file,pipe"
