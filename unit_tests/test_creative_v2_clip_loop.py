"""Comfy clips stay video assets, and clip repetition is recorded rather than hidden."""

import hashlib

import pytest
from test_creative_v2_asset_generation_e2e import pipeline as _pipeline_fixture
from test_creative_v2_dispatch import stores as _stores_fixture

from ttakkari.creative.v2 import asset_generation, dispatch


@pytest.fixture
def stores(tmp_path):
    return _stores_fixture.__wrapped__(tmp_path)


@pytest.fixture
def pipeline(tmp_path, monkeypatch):
    return _pipeline_fixture.__wrapped__(tmp_path, monkeypatch)


def test_managed_comfy_clip_is_preserved_as_video_not_a_single_png(pipeline):
    _, plans, plan, generator, _, _ = pipeline
    result = asset_generation.generate_assets(plans, plan["id"], plan["revision"], generator)
    assert result["status"] == "succeeded", result.get("error")

    store = dispatch.ProductionStore(plans)
    stored = store.read(plan["id"])
    for run in result["receipt"]["engine_runs"]:
        asset = stored["assets"]["scenes"][run["scene_id"]]
        original = next(iter(run["receipt"]["outputs"]))
        assert asset["kind"] == "video"
        assert asset["source_video_file"].endswith(".mp4")
        assert asset["file"] == asset["source_video_file"]
        assert asset["preview_image_file"].endswith(".png")
        assert asset["preview_image_file"] != asset["source_video_file"]
        # The stored asset is the byte-identical engine output, not a re-encoded or still-frame stand-in.
        assert hashlib.sha256(store.asset_path(plan["id"], asset).read_bytes()).hexdigest() == original["sha256"]
        assert asset["meta"]["has_motion"] is True


def test_clip_shorter_than_scene_records_loop_on_asset_meta_and_view(pipeline):
    _, plans, plan, generator, _, studio = pipeline
    asset_generation.generate_assets(plans, plan["id"], plan["revision"], generator)
    dispatch.dispatch(plans, studio, plan["id"], plan["revision"])
    view = dispatch.view(plans, studio, plan["id"])

    stored = dispatch.ProductionStore(plans).read(plan["id"])
    seen = False
    for scene in plan["scenes"]:
        asset = stored["assets"]["scenes"][scene["scene_id"]]
        loop = asset["meta"]["loop"]
        clip = asset["meta"]["duration"]
        assert loop["clip_seconds"] == pytest.approx(clip, abs=0.001)
        assert loop["scene_seconds"] == pytest.approx(scene["seconds"], abs=0.001)
        assert loop["applied"] is (clip + 0.05 < scene["seconds"])
        if loop["applied"]:
            seen = True
            assert loop["repeats"] >= 2
        assert view["assets"]["scenes"][scene["scene_id"]]["loop"] == loop
    assert seen, "fixture clips (3s) must be shorter than at least one planned scene"


def test_clip_loop_helper_marks_exact_fit_unknown_and_non_video():
    clip_loop = dispatch._clip_loop
    video = {"kind": "video", "meta": {"duration": 4.0}}
    assert clip_loop(video, 4.0)["applied"] is False
    assert clip_loop(video, 9.0) == {"applied": True, "clip_seconds": 4.0, "scene_seconds": 9.0, "repeats": 3}
    assert clip_loop({"kind": "video", "meta": {}}, 5.0) == {
        "applied": None, "clip_seconds": None, "scene_seconds": 5.0, "repeats": None,
    }
    assert clip_loop({"kind": "image", "meta": {"duration": 1.0}}, 5.0) is None


def test_directed_compositor_receipt_records_loop_when_clip_is_shorter_than_segment(
    stores, tmp_path, monkeypatch
):
    import subprocess
    from types import SimpleNamespace

    from test_creative_v2_core_integration import direction
    from test_creative_v2_dispatch import approved, make_wav

    from ttakkari.creative.render import ffmpeg, inspect_media
    from ttakkari.creative.v2 import engines
    from ttakkari.creative.v2.planner import ProductionDirection

    plans, studio = stores
    plan = approved(
        plans, director=ProductionDirection.model_validate(direction(("generative_video",) * 3))
    )
    video = tmp_path / "clip.mp4"
    subprocess.run(
        [ffmpeg(), "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=s=640x360:r=24", "-t", "1",
         "-pix_fmt", "yuv420p", str(video)], check=True, capture_output=True,
    )
    for scene in plan["scenes"]:
        dispatch.put_asset(plans, plan["id"], kind="scene", scene_id=scene["scene_id"], source=video,
                           name="clip.mp4", meta=inspect_media(video))
    wav = make_wav(tmp_path / "music.wav", plan["scenes"][-1]["end_time"] + 1.0)
    dispatch.put_asset(plans, plan["id"], kind="music", scene_id=None, source=wav, name="music.wav",
                       meta=inspect_media(wav))

    calls = []

    def fake_compose(source, music, output, **kw):
        calls.append(kw["seconds"])
        output.write_bytes(b"composite")
        return {"real_execution": True}

    monkeypatch.setattr(engines, "compose_segment", fake_compose)
    monkeypatch.setattr(dispatch, "_slice_music", lambda *a, **k: a[-1].write_bytes(b"wav"))
    monkeypatch.setattr(dispatch, "inspect_media", lambda path: {"duration": 1.0})
    monkeypatch.setattr(dispatch, "dimensions", lambda aspect: (1280, 720))

    dispatch.dispatch(plans, studio, plan["id"], 1)
    store = dispatch.ProductionStore(plans)
    with store.tx(plan["id"]) as data:
        record = data["dispatches"][-1]
        segment = record["segments"][0]
        dispatch._render_directed_segment(
            SimpleNamespace(id="x"), store, plan, segment, data, record["style"], "k", "title_only"
        )
        loop = segment["compositor_receipt"]["loop"]
    assert calls and loop["applied"] is True
    assert loop["clip_seconds"] == pytest.approx(1.0, abs=0.05)
    assert loop["scene_seconds"] == pytest.approx(segment["seconds"], abs=0.001)
    assert loop["repeats"] >= 2
