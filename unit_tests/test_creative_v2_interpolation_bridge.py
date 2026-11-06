"""Core selection contracts; renderer captures below are not video evidence."""

import json
from dataclasses import asdict
from pathlib import Path

import pytest
from test_creative_v2_core_integration import direction
from test_creative_v2_dispatch import approved
from test_creative_v2_dispatch import stores as _stores

from ttakkari.creative.v2 import dispatch, engines, frame_animation
from ttakkari.creative.v2.planner import ProductionDirection


@pytest.fixture
def stores(tmp_path):
    return _stores.__wrapped__(tmp_path)


def selected(mode=None):
    raw = direction(("frame_animation_2d", "slideshow", "slideshow"))
    if mode is not None:
        raw["scenes"][0]["interpolation_mode"] = mode
    return ProductionDirection.model_validate(raw)


@pytest.mark.parametrize("mode", [None, "hold", "linear"])
def test_approved_plan_to_real_adapter_project_spec(
    stores, tmp_path, monkeypatch, mode
):
    plans, _ = stores
    plan = approved(plans, director=selected(mode))
    assert plan["approval"]["revision"] == plan["revision"]
    if mode is not None:
        assert plan["scenes"][0]["interpolation_mode"] == mode
        assert plan["director"]["scenes"][0]["interpolation_mode"] == mode
    captured = []

    def capture(self, *, spec, output_mp4, **kwargs):
        captured.append(spec)
        Path(output_mp4).write_bytes(b"contract capture, not video")
        return {
            "qa": {"probe": {"duration": spec.target_duration}},
            "total_frames_rendered": 0,
            "sampled_keyframes": [],
        }

    monkeypatch.setattr(
        frame_animation.FrameAnimationRenderer, "render_project", capture
    )
    monkeypatch.setattr(
        dispatch,
        "inspect_media",
        lambda path: {
            "has_video": True,
            "duration": plan["render_plan"]["segments"][0]["seconds"],
        },
    )
    store = dispatch.ProductionStore(plans)
    dispatch._render_producer(
        engines.adapter_by_id("skill.frame_animation_2d"),
        store,
        plan,
        plan["render_plan"]["segments"][0],
        "capture",
    )
    assert captured[0].interpolation_mode == (mode or "hold")
    if mode == "linear":
        assert "hold" not in captured[0].interpolation_reason
    execution = json.loads(
        (store.folder(plan["id"]) / "engine-work/capture/input.json").read_text()
    )
    assert execution.get("interpolation_mode", "hold") == (mode or "hold")


def test_mode_edit_changes_segment_key_and_requires_reapproval(stores):
    plans, _ = stores
    before = approved(plans, director=selected("hold"))
    changed = plans.edit(before["id"], 1, director=selected("linear"))
    assert changed["revision"] == 2 and changed["approval"] is None
    assert not changed["render_gate"]
    assert (
        before["render_plan"]["segments"][0]["input_sha256"]
        != changed["render_plan"]["segments"][0]["input_sha256"]
    )
    assert (
        before["render_plan"]["segments"][1:] == changed["render_plan"]["segments"][1:]
    )
    with pytest.raises(ValueError):
        plans.assert_renderable(before["id"], 1)
    approved_plan = plans.approve(before["id"], 2)
    assert approved_plan["scenes"][0]["interpolation_mode"] == "linear"
    reset = plans.edit(before["id"], 2, director=selected())
    assert reset["scenes"][0].get("interpolation_mode", "hold") == "hold"


@pytest.mark.parametrize("mode", ["optical_flow", "", False, 5])
def test_unsupported_selection_is_rejected_at_plan_and_adapter(
    tmp_path, monkeypatch, mode
):
    with pytest.raises(ValueError, match="interpolation_mode"):
        selected(mode)
    called = []
    monkeypatch.setattr(
        frame_animation.FrameAnimationRenderer,
        "render_project",
        lambda *a, **k: called.append(True),
    )
    with pytest.raises(ValueError, match="interpolation_mode"):
        frame_animation.ADAPTER.render_segment(
            {"seconds": 8, "interpolation_mode": mode}, output_path=tmp_path / "bad.mp4"
        )
    assert called == []


def test_linear_on_non_b_strategy_is_rejected():
    raw = direction(("slideshow",) * 3)
    raw["scenes"][0]["interpolation_mode"] = "linear"
    with pytest.raises(ValueError, match="frame_animation_2d"):
        ProductionDirection.model_validate(raw)


def test_omission_and_explicit_hold_build_identical_project_specs(
    tmp_path, monkeypatch
):
    captured = []

    def capture(self, *, spec, output_mp4, **kwargs):
        captured.append(asdict(spec))
        Path(output_mp4).write_bytes(b"contract capture, not video")
        return {
            "qa": {"probe": {"duration": 8}},
            "total_frames_rendered": 0,
            "sampled_keyframes": [],
        }

    monkeypatch.setattr(
        frame_animation.FrameAnimationRenderer, "render_project", capture
    )
    for extra in ({}, {"interpolation_mode": "hold"}):
        frame_animation.ADAPTER.render_segment(
            {"seconds": 8, **extra}, output_path=tmp_path / "capture.mp4"
        )
    assert captured[0] == captured[1]


def test_storyboard_linear_survives_reviewed_plan_creation(stores):
    import uuid

    from test_creative_v2_stage3 import output, request

    from ttakkari.creative.v2.storyboard import materialize, output_hash

    plans, _ = stores
    draft = output()
    draft["scenes"][0].update(
        strategy="frame_animation_2d", preset="walk_cycle", interpolation_mode="linear"
    )
    draft["scenes"][0]["alternatives"] = [
        {"strategy": "slideshow", "rationale": "업로드 소스 재생 대안"}
    ]
    raw = json.dumps(draft, ensure_ascii=False)
    plan = materialize(
        plans,
        request(),
        raw,
        run_id=str(uuid.uuid4()),
        model="schema-fixture",
        reviewed_output_sha256=output_hash(raw),
    )
    assert plan["scenes"][0]["interpolation_mode"] == "linear"
    assert plan["director"]["scenes"][0]["interpolation_mode"] == "linear"
    assert plan["approval"] is None
