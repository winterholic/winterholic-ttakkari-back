"""장면 요구가 managed Comfy 프로필 사다리로 바뀌고, 생성형 영상이 불가할 때만 카메라 움직임으로 내려가는지 고정한다."""
from __future__ import annotations

import copy

import pytest
from test_creative_v2_asset_generation_e2e import pipeline as _pipeline_fixture

from ttakkari import managed_comfy
from ttakkari.creative.render import inspect_media
from ttakkari.creative.v2 import asset_routing as routing


@pytest.fixture
def pipeline(tmp_path, monkeypatch):
    return _pipeline_fixture.__wrapped__(tmp_path, monkeypatch)


def _scene(seconds=3.0, **extra):
    return {"scene_id": "s1", "seconds": seconds, **extra}


def test_scene_need_follows_the_plan_and_defaults_to_moving_clip():
    assert routing.scene_need(_scene()) == ("video", "정지 이미지 요구가 없어 움직이는 클립을 기본으로 함")
    assert routing.scene_need(_scene(strategy="generative_video"))[0] == "video"
    image_shot = {"output_contract": {"kind": "image"}, "required_capabilities": ["gen.image"]}
    assert routing.scene_need(_scene(shots=[image_shot]))[0] == "still"
    assert routing.scene_need(_scene(shots=[{"required_capabilities": ["gen.image"]}]))[0] == "still"
    mixed = [image_shot, {"output_contract": {"kind": "video"}}]
    assert routing.scene_need(_scene(shots=mixed))[0] == "video"
    assert routing.scene_need(_scene(shots=[{"required_capabilities": ["gen.video_i2v"]}]))[0] == "video"
    # 영상 전략은 shot 이 이미지를 말해도 영상이다.
    assert routing.scene_need(_scene(strategy="generative_video", shots=[image_shot]))[0] == "video"


def test_route_prefers_i2v_within_its_limit_and_t2v_long_beyond_it():
    short = routing.route_scene(_scene(3.0))
    assert short.ladder == ("keyframe_i2v", "t2v", "t2v_draft", "camera_motion")
    assert short.clip_seconds == 3.0
    long = routing.route_scene(_scene(8.0))
    assert long.ladder == ("t2v", "keyframe_i2v", "t2v_draft", "camera_motion")
    assert long.clip_seconds == pytest.approx(121 / 24)
    assert routing.route_scene(_scene(3.375)).ladder[0] == "keyframe_i2v"
    assert routing.route_scene(_scene(3.5)).ladder[0] == "t2v"
    still = routing.route_scene(_scene(shots=[{"output_contract": {"kind": "image"}}]))
    assert still.need == "still" and still.ladder == ("keyframe",)
    json_safe = still.record()
    assert json_safe["ladder"] == ["keyframe"]


def test_clip_options_clamp_to_profile_range_and_skip_unknown_length():
    assert routing.clip_options(routing.I2V_PROFILE, 3.0) == {"duration_seconds": 3.0}
    assert routing.clip_options(routing.I2V_PROFILE, 0.2) == {"duration_seconds": pytest.approx(25 / 24)}
    assert routing.clip_options(routing.I2V_PROFILE, 30.0) == {"duration_seconds": pytest.approx(81 / 24)}
    assert routing.clip_options(routing.T2V_LONG_PROFILE, None) == {}
    # 장면이 아주 길어도 프로필이 받는 최대치 그대로 넘겨 prepare 검증을 통과해야 한다.
    managed_comfy.PROFILES[routing.T2V_LONG_PROFILE].resolve(None, None, routing.clip_options(
        routing.T2V_LONG_PROFILE, 600.0)["duration_seconds"])


def test_profile_blockers_names_missing_models_without_loading_anything(tmp_path):
    reasons = routing.profile_blockers(routing.KEYFRAME_PROFILE, models_root=tmp_path)
    assert reasons and "flux-2-klein-4b.safetensors" in reasons[0]
    assert routing.profile_blockers("no-such-profile", models_root=tmp_path)


def _plan_with(plan, **scene_overrides):
    changed = copy.deepcopy(plan)
    for scene in changed["scenes"]:
        scene.update(scene_overrides)
    return changed


def _generate(generator, plan, tmp_path):
    events = []
    result = generator.generate(plan, workdir=tmp_path / "work", progress=events.append)
    return result, events


def test_short_scene_makes_keyframe_then_image_to_video_from_that_exact_keyframe(pipeline, tmp_path):
    _settings, _plans, plan, generator, controller, _studio = pipeline
    result, _events = _generate(generator, _plan_with(plan, seconds=3.0), tmp_path)

    profiles = [c["profile"] for c in controller.calls]
    assert profiles == [routing.KEYFRAME_PROFILE, routing.I2V_PROFILE] * 3 + [routing.MUSIC_PROFILE]
    keyframe, i2v = controller.calls[0], controller.calls[1]
    assert keyframe["options"] == {"width": 1280, "height": 720}
    first_keyframe_task = next(t for t, j in controller.jobs.items() if j["profile"] == routing.KEYFRAME_PROFILE)
    assert i2v["options"]["image_task_id"] == first_keyframe_task
    assert i2v["options"]["duration_seconds"] == 3.0
    scene = result["scenes"]["s1" if "s1" in result["scenes"] else next(iter(result["scenes"]))]
    assert scene["kind"] == "video" and scene["meta"]["motion_source"] == "generative_video"
    assert scene["meta"]["keyframe_task_id"] == first_keyframe_task
    route = scene["meta"]["generation_route"]
    assert route["selected"] == "keyframe_i2v" and route["generative_motion"] is True
    assert route["fallback_reason"] is None and "fallback_reason" not in scene["meta"]


def test_still_scene_stops_at_the_keyframe_image(pipeline, tmp_path):
    _settings, _plans, plan, generator, controller, _studio = pipeline
    shots = [{"shot_id": "k", "action": "frame", "output_contract": {"kind": "image"}}]
    result, _ = _generate(generator, _plan_with(plan, seconds=3.0, shots=shots), tmp_path)

    assert [c["profile"] for c in controller.calls] == [routing.KEYFRAME_PROFILE] * 3 + [routing.MUSIC_PROFILE]
    scene = next(iter(result["scenes"].values()))
    assert scene["kind"] == "image" and scene["meta"]["motion_source"] == "none"
    assert scene["preview_path"] is None
    assert scene["meta"]["generation_route"]["selected"] == "keyframe"


def test_i2v_failure_falls_to_t2v_and_stays_generative(pipeline, tmp_path):
    _settings, _plans, plan, generator, controller, _studio = pipeline
    controller.failing_profiles = {routing.I2V_PROFILE}
    result, events = _generate(generator, _plan_with(plan, seconds=3.0), tmp_path)

    scene = next(iter(result["scenes"].values()))
    route = scene["meta"]["generation_route"]
    assert route["selected"] == "t2v" and route["generative_motion"] is True
    assert route["fallback_reason"] is None
    assert [a["status"] for a in route["attempts"]] == ["failed", "succeeded"]
    assert "forced failure" in route["attempts"][0]["reason"]
    assert any(e.get("stage") == "scene_step_failed" and e["step"] == "keyframe_i2v" for e in events)


def test_camera_motion_is_used_only_when_every_generative_step_failed_and_says_why(pipeline, tmp_path):
    _settings, _plans, plan, generator, controller, _studio = pipeline
    controller.failing_profiles = {routing.I2V_PROFILE, routing.T2V_LONG_PROFILE, "wan22-5b-draft"}
    result, _ = _generate(generator, _plan_with(plan, seconds=3.0), tmp_path)

    scene = next(iter(result["scenes"].values()))
    meta = scene["meta"]
    assert scene["kind"] == "video" and meta["motion_source"] == "image_camera_motion"
    assert meta["generation_route"]["selected"] == "camera_motion"
    assert meta["generation_route"]["generative_motion"] is False
    for step in ("keyframe_i2v", "t2v", "t2v_draft"):
        assert f"{step} failed" in meta["fallback_reason"]
    assert meta["keyframe_sha256"] and meta["source_engine"].endswith("ffmpeg.camera_motion")
    info = inspect_media(scene["path"])
    assert info["has_video"] and (meta["width"], meta["height"], meta["fps"]) == (1280, 720, 24)
    assert info["duration"] == pytest.approx(3.0, abs=0.2)


def test_blocked_profile_is_recorded_and_skipped_without_submitting(pipeline, tmp_path):
    _settings, _plans, plan, generator, controller, _studio = pipeline
    generator._unavailable = {routing.I2V_PROFILE: "모델 파일이 없다: x"}
    result, _ = _generate(generator, _plan_with(plan, seconds=3.0), tmp_path)

    assert routing.I2V_PROFILE not in {c["profile"] for c in controller.calls}
    route = next(iter(result["scenes"].values()))["meta"]["generation_route"]
    assert route["attempts"][0] == {"step": "keyframe_i2v", "profile": routing.I2V_PROFILE,
                                    "status": "blocked", "reason": "모델 파일이 없다: x"}
    assert route["selected"] == "t2v"


def test_scene_fails_loudly_when_even_the_keyframe_fallback_is_gone(pipeline, tmp_path):
    _settings, _plans, plan, generator, controller, _studio = pipeline
    controller.failing_profiles = {routing.KEYFRAME_PROFILE, routing.I2V_PROFILE, routing.T2V_LONG_PROFILE,
                                   "wan22-5b-draft"}
    with pytest.raises(RuntimeError, match="모든 생성 단계가 실패"):
        _generate(generator, _plan_with(plan, seconds=3.0), tmp_path)


def test_unknown_profile_on_an_older_controller_is_a_rejection_not_a_crash(pipeline, tmp_path):
    _settings, _plans, plan, generator, controller, _studio = pipeline
    original = controller.prepare

    def reject_new_profiles(profile, **kwargs):
        if profile in {routing.KEYFRAME_PROFILE, routing.I2V_PROFILE, routing.T2V_LONG_PROFILE}:
            raise ValueError("unknown registered profile")
        return original(profile, **kwargs)

    controller.prepare = reject_new_profiles
    result, _ = _generate(generator, _plan_with(plan, seconds=3.0), tmp_path)

    route = next(iter(result["scenes"].values()))["meta"]["generation_route"]
    assert route["selected"] == "t2v_draft"
    assert {a["status"] for a in route["attempts"][:-1]} == {"rejected"}


def test_lease_busy_is_not_swallowed_as_a_step_rejection(pipeline, tmp_path):
    from ttakkari.host_resource import LeaseBusy

    _settings, _plans, plan, generator, controller, _studio = pipeline

    def busy(_profile, **_kwargs):
        raise LeaseBusy("host GPU resource is owned by another runtime")

    controller.prepare = busy
    with pytest.raises(LeaseBusy):
        _generate(generator, _plan_with(plan, seconds=3.0), tmp_path)
