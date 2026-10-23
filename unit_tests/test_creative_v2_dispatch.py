"""승인된 v2 계획 → 엔진 어댑터 → 구간 실행 → 최종 미디어 QA → 제작 기록.

입력 이미지·음악은 이 테스트가 만든 합성 파일이다. 이 테스트는 계획에서 엔진(v1 FFmpeg 슬라이드쇼)까지의
수직 경로와 재개·재시도·QA 동작을 검증할 뿐이며 컨셉 제작이나 생성 모델의 증거가 아니다.
"""

import asyncio
import math
import struct
import subprocess
import wave
from itertools import pairwise
from pathlib import Path

import pytest
from PIL import Image

from ttakkari.creative.render import ffmpeg, inspect_media, scan
from ttakkari.creative.service import Studio
from ttakkari.creative.v2 import dispatch, engines
from ttakkari.creative.v2.duration import DurationRequest, SceneEdit
from ttakkari.creative.v2.intent import InfoItem, IntentSpec, Selection
from ttakkari.creative.v2.plans import PlanStore, build_plan
from ttakkari.creative.v2.registry import Registry

BRIEF = "내가 가진 사진으로 슬라이드 영상을 만든다"
NARRATION = "첫 번째 장면에서는 준비한 사진을 천천히 보여 주고 핵심만 짧게 설명합니다."


def intent(selection_ids=("C08.three_act", "C10.dissolve"), count=3):
    return IntentSpec(
        selections=[
            Selection(
                entry_id=entry_id,
                role="primary" if entry_id.startswith("C08") else "secondary",
                confidence=0.8,
            )
            for entry_id in selection_ids
        ],
        info_items=[
            InfoItem(id=f"i{i}", title=f"장면 {i + 1}", narration=NARRATION)
            for i in range(count)
        ],
    )


@pytest.fixture
def stores(tmp_path):
    return PlanStore(tmp_path / "plans", registry=Registry()), Studio(
        tmp_path / "studio"
    )


def approved(plans, **kw):
    plan = plans.create(
        build_plan(BRIEF, kw.pop("intent", intent()), registry=Registry(), **kw)
    )
    return plans.approve(plan["id"], plan["revision"])


def make_image(path: Path, shade: int) -> Path:
    Image.new("RGB", (1280, 720), (30 + shade % 200, 90, 160 - shade % 100)).save(path)
    return path


def make_wav(path: Path, seconds: float) -> Path:
    rate = 22050
    with wave.open(str(path), "wb") as f:
        f.setnchannels(1)
        f.setsampwidth(2)
        f.setframerate(rate)
        f.writeframes(
            b"".join(
                struct.pack("<h", int(9000 * math.sin(2 * math.pi * 220 * n / rate)))
                for n in range(int(rate * seconds))
            )
        )
    return path


def upload_scene(plans, plan, scene_id, tmp_path, shade):
    image = make_image(tmp_path / f"{scene_id}-{shade}.png", shade)
    return dispatch.put_asset(
        plans, plan["id"], kind="scene", scene_id=scene_id, source=image,
        name=f"{scene_id}.png", meta={"width": 1280, "height": 720},
    )


def upload_all(plans, plan, tmp_path, music_extra=1.0):
    for i, scene in enumerate(plan["scenes"]):
        upload_scene(plans, plan, scene["scene_id"], tmp_path, 10 * i)
    total = plan["scenes"][-1]["end_time"] + music_extra
    wav = make_wav(tmp_path / "music.wav", total)
    dispatch.put_asset(
        plans, plan["id"], kind="music", scene_id=None, source=wav,
        name="music.wav", meta=inspect_media(wav),
    )


def run_renderer(studio):
    """큐에 남은 렌더 작업이 없을 때까지 렌더러를 돌린다(렌더러 루프와 같은 scan)."""
    for _ in range(40):
        pending = [
            j
            for p in studio.list()
            for j in p["render_jobs"]
            if j["status"] in ("queued", "running")
        ]
        if not pending:
            return
        asyncio.run(scan(studio))
    raise AssertionError("렌더러가 큐를 비우지 못했습니다.")


# --- 게이트와 사전 조건 ------------------------------------------------------


def test_baseline_slideshow_selection_opens_the_render_gate_only_when_approved(stores):
    plans, _ = stores
    plan = plans.create(build_plan(BRIEF, intent(), registry=Registry()))
    assert plan["match"]["execution_scope"] == "studio_runtime"
    assert plan["match"]["primary_recipe"] == "R21"
    assert plan["render_gate"] is False
    assert plans.approve(plan["id"], plan["revision"])["render_gate"] is True


def test_rich_selection_keeps_its_own_recipe_and_gate_stays_closed(stores):
    plans, studio = stores
    rich = intent(("C01.tech_explainer", "C03.vector_motion_graphics"))
    plan = plans.create(build_plan(BRIEF, rich, registry=Registry()))
    assert plan["match"]["primary_recipe"] != "R21"
    with pytest.raises(ValueError, match="렌더할 수 없습니다"):
        dispatch.dispatch(plans, studio, plan["id"], plan["revision"])
    assert studio.list() == []


def test_unapproved_plan_is_not_dispatched_or_rendered(stores):
    plans, studio = stores
    plan = plans.create(build_plan(BRIEF, intent(), registry=Registry()))
    with pytest.raises(ValueError, match="렌더할 수 없습니다"):
        dispatch.dispatch(plans, studio, plan["id"], plan["revision"])
    with pytest.raises(ValueError, match="렌더할 수 없습니다"):
        dispatch.request_render(plans, studio, plan["id"], plan["revision"])
    assert studio.list() == []


def test_missing_required_font_blocks_dispatch(stores, monkeypatch, tmp_path):
    plans, studio = stores
    plan = approved(plans)
    monkeypatch.setattr(engines, "CAPTION_FONT", tmp_path / "no-such-font.ttc")
    with pytest.raises(ValueError, match="필수 의존성 없음.*글꼴"):
        dispatch.dispatch(plans, studio, plan["id"], plan["revision"])
    seen = dispatch.view(plans, studio, plan["id"])
    assert seen["state"] == "not_dispatched" and seen["dispatchable"] is False
    assert any("글꼴" in r for r in seen["reasons"])


def test_missing_ffmpeg_blocks_dispatch(stores, monkeypatch):
    plans, studio = stores
    plan = approved(plans)

    def broken():
        raise RuntimeError("no ffmpeg")

    monkeypatch.setattr(engines, "ffmpeg", broken)
    with pytest.raises(ValueError, match="필수 의존성 없음.*ffmpeg"):
        dispatch.dispatch(plans, studio, plan["id"], plan["revision"])


def test_provider_without_an_engine_adapter_is_not_dispatched(stores, monkeypatch):
    plans, studio = stores
    plan = approved(plans)
    monkeypatch.setattr(engines, "ADAPTERS", [])
    with pytest.raises(ValueError, match="엔진 어댑터가 없습니다"):
        dispatch.dispatch(plans, studio, plan["id"], plan["revision"])


def test_vertical_aspect_is_refused_by_the_engine_limits(stores):
    plans, studio = stores
    plan = approved(plans, aspect="9:16")
    with pytest.raises(ValueError, match="화면비"):
        dispatch.dispatch(plans, studio, plan["id"], plan["revision"])


def test_dispatch_records_engine_identity_and_plan_segments_without_creating_projects(stores):
    plans, studio = stores
    plan = approved(plans)
    first = dispatch.dispatch(plans, studio, plan["id"], plan["revision"])
    again = dispatch.dispatch(plans, studio, plan["id"], plan["revision"])
    assert first["state"] == "awaiting_assets" and again["log"] == first["log"]
    assert first["engine"]["adapter"] == "skill.v1_ffmpeg_slideshow"
    assert "ffmpeg version" in first["engine"]["version"]
    assert [s["id"] for s in first["segments"]] == [
        s["id"] for s in plan["render_plan"]["segments"]
    ]
    assert all(s["status"] == "pending" and s["project_id"] is None for s in first["segments"])
    assert studio.list() == []
    assert first["assets"]["missing_scene_ids"] == [s["scene_id"] for s in plan["scenes"]]


def test_render_is_refused_until_scene_images_and_long_enough_music_exist(stores, tmp_path):
    plans, studio = stores
    plan = approved(plans)
    dispatch.dispatch(plans, studio, plan["id"], plan["revision"])
    with pytest.raises(ValueError, match="이미지가 없습니다.*음악 자산이 없습니다"):
        dispatch.request_render(plans, studio, plan["id"], plan["revision"])
    upload_all(plans, plan, tmp_path, music_extra=-5.0)
    with pytest.raises(ValueError, match="음악.*계획 길이.*보다 짧습니다"):
        dispatch.request_render(plans, studio, plan["id"], plan["revision"])
    assert studio.list() == []


def test_assets_for_unknown_scenes_are_rejected(stores, tmp_path):
    plans, _ = stores
    plan = approved(plans)
    with pytest.raises(ValueError, match="장면 nope"):
        upload_scene(plans, plan, "nope", tmp_path, 1)


# --- 실제 엔진 실행 ------------------------------------------------------------


def test_approved_plan_renders_a_verified_media_asset_with_engine_and_input_ids(stores, tmp_path):
    plans, studio = stores
    plan = approved(plans)
    dispatch.dispatch(plans, studio, plan["id"], plan["revision"])
    upload_all(plans, plan, tmp_path)
    assert dispatch.view(plans, studio, plan["id"])["state"] == "ready_to_render"

    queued = dispatch.request_render(plans, studio, plan["id"], plan["revision"])
    assert queued["state"] == "rendering"
    assert queued["segments"][0]["job_id"] and queued["segments"][0]["project_id"]
    run_renderer(studio)

    done = dispatch.view(plans, studio, plan["id"])
    assert done["state"] == "succeeded", done
    final = done["final"]
    assert final["status"] == "qa_passed" and final["qa"]["passed"] is True
    assert {c["id"] for c in final["qa"]["checks"]} >= {
        "video_track", "audio_track", "resolution", "duration", "not_black", "audible",
    }
    path, name = dispatch.final_media(plans, plan["id"], final["id"])
    info = inspect_media(path)
    assert info["has_video"] and info["has_audio"]
    assert abs(info["duration"] - plan["scenes"][-1]["end_time"]) < 0.4
    assert name.endswith(".mp4") and path.stat().st_size == final["size"]

    record = dispatch.ProductionStore(plans).read(plan["id"])["dispatches"][0]
    seg = record["segments"][0]
    assert record["plan_sha256"] == plan["approval"]["snapshot"]["plan_sha256"]
    assert record["final"]["inputs"]["segments"][0]["job_id"] == seg["job_id"]
    assert record["final"]["inputs"]["segments"][0]["output_sha256"] == seg["output"]["sha256"]
    assert len(record["final"]["inputs"]["asset_sha256"]) >= 2
    assert record["final"]["engine"]["adapter"] == "skill.v1_ffmpeg_slideshow"
    events = [e["event"] for e in record["log"]]
    assert events[0] == "dispatched" and "segment_succeeded" in events and events[-1] == "assembled"
    assert final["duration"]["plan_status"] == "auto"
    assert abs(final["duration"]["measured_seconds"] - final["duration"]["planned_seconds"]) < 0.4
    # 같은 요청을 다시 보내도 새 작업이 생기지 않는다.
    again = dispatch.request_render(plans, studio, plan["id"], plan["revision"])
    assert again["segments"][0]["attempts"] == 1 and again["state"] == "succeeded"


def test_long_plan_over_the_single_project_limit_runs_split_segments_and_joins_them(stores, tmp_path):
    plans, studio = stores
    plan = approved(
        plans,
        intent=intent(count=30),
        duration=DurationRequest(mode="preferred", seconds=300),
    )
    rp = plan["render_plan"]
    assert rp["requires_split"] is True and len(rp["segments"]) >= 4
    assert all(len(s["scene_ids"]) >= 3 for s in rp["segments"])
    dispatch.dispatch(plans, studio, plan["id"], plan["revision"])
    upload_all(plans, plan, tmp_path)
    queued = dispatch.request_render(plans, studio, plan["id"], plan["revision"])
    assert len({s["project_id"] for s in queued["segments"]}) == len(rp["segments"])
    run_renderer(studio)

    done = dispatch.view(plans, studio, plan["id"])
    assert done["state"] == "succeeded", done
    path, _ = dispatch.final_media(plans, plan["id"], done["final"]["id"])
    info = inspect_media(path)
    planned = plan["scenes"][-1]["end_time"]
    assert planned > 240 and abs(info["duration"] - planned) < 1.0, (info, planned)
    assert [s["status"] for s in done["segments"]] == ["succeeded"] * len(rp["segments"])


def test_replacing_one_scene_image_reruns_only_its_segment(stores, tmp_path):
    plans, studio = stores
    plan = approved(plans, intent=intent(count=9))
    segments = plan["render_plan"]["segments"]
    assert len(segments) >= 2
    dispatch.dispatch(plans, studio, plan["id"], plan["revision"])
    upload_all(plans, plan, tmp_path)
    dispatch.request_render(plans, studio, plan["id"], plan["revision"])
    run_renderer(studio)
    first = dispatch.view(plans, studio, plan["id"])
    assert first["state"] == "succeeded"

    target = segments[-1]["scene_ids"][0]
    upload_scene(plans, plan, target, tmp_path, 199)
    pending = dispatch.view(plans, studio, plan["id"])
    assert pending["state"] == "ready_to_render"
    assert pending["outdated_segments"] == [segments[-1]["id"]]
    again = dispatch.request_render(plans, studio, plan["id"], plan["revision"])
    attempts = {s["id"]: s["attempts"] for s in again["segments"]}
    assert attempts == {s["id"]: (2 if s["id"] == segments[-1]["id"] else 1) for s in segments}
    keys_before = {s["id"]: s["run_key"] for s in first["segments"]}
    keys_after = {s["id"]: s["run_key"] for s in again["segments"]}
    assert [i for i in keys_before if keys_before[i] != keys_after[i]] == [segments[-1]["id"]]
    run_renderer(studio)
    final = dispatch.view(plans, studio, plan["id"])
    assert final["state"] == "succeeded"
    assert final["final"]["sha256"] != first["final"]["sha256"]


def test_failed_segment_is_retried_in_place_reusing_finished_scene_clips(stores, tmp_path):
    plans, studio = stores
    plan = approved(plans, intent=intent(count=9))
    segments = plan["render_plan"]["segments"]
    dispatch.dispatch(plans, studio, plan["id"], plan["revision"])
    upload_all(plans, plan, tmp_path)
    queued = dispatch.request_render(plans, studio, plan["id"], plan["revision"])
    victim = queued["segments"][0]
    project = studio.get(victim["project_id"])
    last_scene = project["scenes"][-1]
    asset = next(a for a in project["assets"] if a["id"] == last_scene["asset_id"])
    image = studio.path(victim["project_id"]) / asset["file"]
    good = image.read_bytes()
    image.write_bytes(b"not an image")
    run_renderer(studio)

    failed = dispatch.view(plans, studio, plan["id"])
    assert failed["state"] == "failed"
    bad = failed["segments"][0]
    assert bad["status"] == "failed" and bad["error"]
    assert all(s["status"] == "succeeded" for s in failed["segments"][1:])
    assert failed["final"] is None

    image.write_bytes(good)
    retry = dispatch.request_render(plans, studio, plan["id"], plan["revision"])
    assert retry["segments"][0]["attempts"] == 2
    assert retry["segments"][0]["project_id"] == victim["project_id"]
    assert [s["attempts"] for s in retry["segments"][1:]] == [1] * (len(segments) - 1)
    run_renderer(studio)
    done = dispatch.view(plans, studio, plan["id"])
    assert done["state"] == "succeeded"
    # 끝난 장면 클립은 다시 인코딩하지 않는다.
    assert done["segments"][0]["metrics"]["cached_scenes"] == len(project["scenes"]) - 1
    events = [e["event"] for e in dispatch.ProductionStore(plans).read(plan["id"])["dispatches"][0]["log"]]
    assert "segment_failed" in events


def test_lost_engine_project_is_rebuilt_on_the_next_request(stores, tmp_path):
    import shutil

    plans, studio = stores
    plan = approved(plans)
    dispatch.dispatch(plans, studio, plan["id"], plan["revision"])
    upload_all(plans, plan, tmp_path)
    queued = dispatch.request_render(plans, studio, plan["id"], plan["revision"])
    old = queued["segments"][0]["project_id"]
    shutil.rmtree(studio.path(old))
    stale = dispatch.view(plans, studio, plan["id"])
    assert stale["segments"][0]["status"] == "stale"
    rebuilt = dispatch.request_render(plans, studio, plan["id"], plan["revision"])
    assert rebuilt["segments"][0]["project_id"] != old
    run_renderer(studio)
    assert dispatch.view(plans, studio, plan["id"])["state"] == "succeeded"


def test_plan_revision_after_dispatch_closes_gate_and_marks_record_stale(stores):
    plans, studio = stores
    plan = approved(plans)
    dispatch.dispatch(plans, studio, plan["id"], plan["revision"])
    edited = plans.edit(
        plan["id"],
        plan["revision"],
        scenes=[
            SceneEdit(
                id=s["scene_id"], title=s["title"], narration=s["narration"],
                info_weight=s["info_weight"], essential=s["essential"], seconds=s["seconds"] + 1,
            )
            for s in plan["scenes"]
        ],
    )
    seen = dispatch.view(plans, studio, plan["id"])
    assert seen["state"] == "stale_revision" and seen["dispatchable"] is False
    assert edited["render_gate"] is False
    with pytest.raises(ValueError, match="렌더할 수 없습니다"):
        dispatch.request_render(plans, studio, plan["id"], edited["revision"])


def test_failed_qa_is_reported_and_the_media_is_not_served(stores, tmp_path, monkeypatch):
    plans, studio = stores
    plan = approved(plans)
    dispatch.dispatch(plans, studio, plan["id"], plan["revision"])
    upload_all(plans, plan, tmp_path)
    dispatch.request_render(plans, studio, plan["id"], plan["revision"])
    run_renderer(studio)
    real = engines.qa_media

    def failing(path, **kw):
        result = real(path, **kw)
        result["checks"].append({"id": "forced", "passed": False, "detail": "강제 실패", "measured": None})
        result["passed"] = False
        return result

    monkeypatch.setattr(engines, "qa_media", failing)
    # 키가 같아도 최종물이 없으면 다시 이어 붙이고 QA한다.
    with plans._folder(plan["id"]).joinpath("production.json").open() as f:
        assert f.read()
    store = dispatch.ProductionStore(plans)
    with store.tx(plan["id"]) as data:
        data["dispatches"][0]["final"] = None
    seen = dispatch.view(plans, studio, plan["id"])
    assert seen["state"] == "qa_failed" and "강제 실패" in seen["reasons"]
    with pytest.raises(FileNotFoundError):
        dispatch.final_media(plans, plan["id"], seen["final"]["id"])


# --- 스타일 적용 -----------------------------------------------------------------


def test_style_dna_applies_only_what_the_engine_consumes_and_reports_the_rest():
    adapter = engines.V1SlideshowAdapter()
    style = {
        "sha256": "s" * 64,
        "motion_language": {"text": "느린 패닝", "params": {"animation": "pan", "transition": "wipe"}},
        "visual_identity": {"text": "굵은 제목", "params": {"caption_style": "title_card"}},
        "lighting": {"text": "저조도", "params": {}},
        "palette": [{"hex": "#112233", "role": "bg"}],
    }
    result = dispatch._style_defaults({"style": style}, adapter)
    assert result["style_dna"] == "s" * 64
    assert result["applied"] == {"animation": "pan", "caption_style": "title_card"}
    assert any("transition" in i and "wipe" in i for i in result["ignored"])
    assert any(i.startswith("lighting") for i in result["ignored"])
    assert any(i.startswith("palette") for i in result["ignored"])
    assert dispatch._style_defaults({"style": None}, adapter) == {
        "style_dna": None, "applied": {}, "ignored": [],
    }


# --- 실제 QA 측정 --------------------------------------------------------------


def _lavfi(tmp_path, name, video, audio):
    out = tmp_path / name
    subprocess.run(
        [
            ffmpeg(), "-hide_banner", "-loglevel", "error", "-y",
            "-f", "lavfi", "-i", video, "-f", "lavfi", "-i", audio,
            "-t", "3", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", str(out),
        ],
        check=True,
        capture_output=True,
    )
    return out


def test_qa_detects_black_video_silence_resolution_and_duration(tmp_path):
    good = _lavfi(tmp_path, "good.mp4", "testsrc=s=1280x720:r=24", "sine=frequency=440")
    ok = engines.qa_media(good, aspect="16:9", planned_seconds=3.0)
    assert ok["passed"] is True

    black = _lavfi(tmp_path, "black.mp4", "color=c=black:s=1280x720:r=24", "sine=frequency=440")
    result = engines.qa_media(black, aspect="16:9", planned_seconds=3.0)
    assert result["passed"] is False
    assert {c["id"] for c in result["checks"] if not c["passed"]} == {"not_black"}

    silent = _lavfi(tmp_path, "silent.mp4", "testsrc=s=1280x720:r=24", "anullsrc=r=48000:cl=mono")
    assert {c["id"] for c in engines.qa_media(silent, aspect="16:9", planned_seconds=3.0)["checks"] if not c["passed"]} == {"audible"}

    small = _lavfi(tmp_path, "small.mp4", "testsrc=s=640x360:r=24", "sine=frequency=440")
    bad = engines.qa_media(small, aspect="16:9", planned_seconds=10.0)
    assert {c["id"] for c in bad["checks"] if not c["passed"]} == {"resolution", "duration"}


def test_scene_ids_cover_segments_exactly_once(stores):
    plans, _ = stores
    plan = approved(plans, intent=intent(count=9))
    segs = plan["render_plan"]["segments"]
    assert [i for s in segs for i in s["scene_ids"]] == [s["scene_id"] for s in plan["scenes"]]
    assert all(a["end_frame"] == b["start_frame"] for a, b in pairwise(segs))


def test_deviated_target_length_is_recorded_with_the_result(stores, tmp_path):
    plans, studio = stores
    plan = approved(plans, duration=DurationRequest(mode="preferred", seconds=600))
    assert plan["duration"]["status"] == "deviated" and plan["render_gate"] is True
    dispatch.dispatch(plans, studio, plan["id"], plan["revision"])
    upload_all(plans, plan, tmp_path)
    dispatch.request_render(plans, studio, plan["id"], plan["revision"])
    run_renderer(studio)
    done = dispatch.view(plans, studio, plan["id"])
    assert done["state"] == "succeeded"
    duration = done["final"]["duration"]
    assert duration["requested"]["seconds"] == 600 and duration["plan_status"] == "deviated"
    assert duration["measured_seconds"] < 100


def test_real_plan_style_params_reach_the_renderer_defaults(tmp_path):
    """계획에 저장되는 style 은 ComposeResult 라 facet 이 `dna` 아래에 있다. 평평한 가짜 style 만 시험하던 때는
    실제 계획의 animation/transition/caption_style 이 조용히 적용되지 않았다."""
    from ttakkari.creative.v2.intent import InfoItem, IntentSpec, Selection
    from ttakkari.creative.v2.plans import build_plan
    from ttakkari.creative.v2.style_dna import (
        Facet,
        Override,
        StyleDNADoc,
        StyleRef,
        StyleStore,
        seed_docs,
    )

    store = StyleStore(tmp_path, seeds=seed_docs())
    store.put(StyleDNADoc(
        style_id="probe_motion", version="1.0.0",
        parent=StyleRef(style_id="neutral_base", version="1.0.0"),
        overrides=[
            Override(field="motion_language", reason="편별 움직임 구분 시험"),
            Override(field="visual_identity", reason="편별 자막 스타일 구분 시험"),
        ],
        motion_language=Facet(text="패닝과 컷", params={"animation": "pan", "transition": "cut"}),
        visual_identity=Facet(text="중앙 자막", params={"caption_style": "center_highlight"}),
    ))
    intent = IntentSpec(
        selections=[Selection(entry_id="C08.three_act", role="primary", confidence=1.0)],
        info_items=[InfoItem(id=f"s{i}", title=f"t{i}", narration=f"n{i}") for i in range(3)],
    )
    plan = build_plan(
        "probe", intent, styles=store, style_refs=[StyleRef(style_id="probe_motion", version="1.0.0")]
    )
    assert "dna" in plan["style"]

    result = dispatch._style_defaults(plan, engines.V1SlideshowAdapter())

    assert result["style_dna"] == plan["style"]["sha256"]
    assert result["applied"] == {"animation": "pan", "transition": "cut", "caption_style": "center_highlight"}
    # 계획 style 이 없으면 그대로 기본값이다.
    assert dispatch._style_defaults({**plan, "style": None}, engines.V1SlideshowAdapter())["applied"] == {}
