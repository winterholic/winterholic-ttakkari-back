"""승인된 v2 계획 → 스튜디오 렌더러(v1 FFmpeg) 디스패치·렌더·제작 기록.

입력 이미지·음악은 이 테스트가 만든 합성 파일이다. 이 테스트는 계획에서 렌더러까지의 연결을 검증할 뿐이며
컨셉 제작이나 생성 모델의 증거가 아니다.
"""

import asyncio
import math
import struct
import wave

import pytest
from PIL import Image

from ttakkari.creative.render import inspect_media, scan
from ttakkari.creative.service import Studio
from ttakkari.creative.v2 import dispatch
from ttakkari.creative.v2.duration import DurationRequest, SceneEdit
from ttakkari.creative.v2.intent import InfoItem, IntentSpec, Selection
from ttakkari.creative.v2.plans import PlanStore, build_plan
from ttakkari.creative.v2.registry import Registry

BRIEF = "내가 가진 사진으로 세 장면 슬라이드 영상을 만든다"
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
        build_plan(
            BRIEF,
            kw.pop("intent", intent()),
            registry=Registry(),
            **kw,
        )
    )
    return plans.approve(plan["id"], plan["revision"])


def attach_assets(studio, project_id, tmp_path):
    project = studio.get(project_id)
    scenes = []
    for i, scene in enumerate(project["scenes"]):
        image = tmp_path / f"scene-{i}.png"
        Image.new("RGB", (1280, 720), (40 + 60 * i, 90, 160 - 40 * i)).save(image)
        asset = studio.add_asset(
            project_id,
            image,
            name=f"장면 {i}",
            kind="image",
            provenance="test-synthetic",
        )
        scenes.append({**scene, "asset_id": asset["id"]})
    wav = tmp_path / "music.wav"
    seconds = math.ceil(sum(s["duration"] for s in scenes)) + 1
    with wave.open(str(wav), "wb") as f:
        f.setnchannels(1)
        f.setsampwidth(2)
        f.setframerate(22050)
        f.writeframes(
            b"".join(
                struct.pack("<h", int(9000 * math.sin(2 * math.pi * 220 * n / 22050)))
                for n in range(22050 * seconds)
            )
        )
    music = studio.add_asset(
        project_id, wav, name="합성음", kind="audio", provenance="test-synthetic"
    )
    current = studio.get(project_id)
    studio.edit(
        project_id, current["revision"], scenes=scenes, music_asset_id=music["id"]
    )


def test_baseline_slideshow_selection_opens_the_render_gate_only_when_approved(stores):
    plans, _ = stores
    plan = plans.create(build_plan(BRIEF, intent(), registry=Registry()))
    assert plan["match"]["execution_scope"] == "studio_runtime"
    assert plan["match"]["primary_recipe"] == "R21"
    assert plan["render_gate"] is False
    gated = plans.approve(plan["id"], plan["revision"])
    assert gated["render_gate"] is True


def test_rich_selection_keeps_its_own_recipe_and_gate_stays_closed(stores):
    plans, studio = stores
    rich = intent(("C01.tech_explainer", "C03.vector_motion_graphics"))
    plan = plans.create(build_plan(BRIEF, rich, registry=Registry()))
    assert plan["match"]["primary_recipe"] != "R21"
    assert plan["match"]["execution_scope"] != "studio_runtime"
    with pytest.raises(ValueError, match="렌더할 수 없습니다"):
        dispatch.dispatch(plans, studio, plan["id"], plan["revision"])
    assert studio.list() == []


def test_unapproved_plan_is_not_dispatched(stores):
    plans, studio = stores
    plan = plans.create(build_plan(BRIEF, intent(), registry=Registry()))
    with pytest.raises(ValueError, match="렌더할 수 없습니다"):
        dispatch.dispatch(plans, studio, plan["id"], plan["revision"])
    assert studio.list() == []


def test_dispatch_creates_project_with_plan_scenes_and_is_idempotent(stores):
    plans, studio = stores
    plan = approved(plans)
    first = dispatch.dispatch(plans, studio, plan["id"], plan["revision"])
    again = dispatch.dispatch(plans, studio, plan["id"], plan["revision"])
    assert first["project_id"] == again["project_id"] and len(studio.list()) == 1
    project = studio.get(first["project_id"])
    assert [s["id"] for s in project["scenes"]] == [s["scene_id"] for s in plan["scenes"]]
    assert [s["duration"] for s in project["scenes"]] == [s["seconds"] for s in plan["scenes"]]
    assert project["scenes"][0]["caption"] == "장면 1"
    assert first["state"] == "awaiting_assets"
    assert any("음악" in r for r in first["reasons"])
    assert first["project_url"] == f"/creative/{first['project_id']}"


def test_render_is_refused_until_assets_exist(stores):
    plans, studio = stores
    plan = approved(plans)
    dispatch.dispatch(plans, studio, plan["id"], plan["revision"])
    with pytest.raises(ValueError, match="자산이 없습니다"):
        dispatch.request_render(plans, studio, plan["id"], plan["revision"])


def test_dispatched_plan_renders_a_real_video_and_records_the_job(stores, tmp_path):
    plans, studio = stores
    plan = approved(plans)
    placed = dispatch.dispatch(plans, studio, plan["id"], plan["revision"])
    attach_assets(studio, placed["project_id"], tmp_path)
    assert dispatch.view(plans, studio, plan["id"])["state"] == "ready_to_render"

    queued = dispatch.request_render(plans, studio, plan["id"], plan["revision"])
    assert queued["state"] == "queued" and queued["job"]["status"] == "queued"
    asyncio.run(scan(studio))

    done = dispatch.view(plans, studio, plan["id"])
    assert done["state"] == "succeeded", done
    project = studio.get(placed["project_id"])
    job = next(j for j in project["render_jobs"] if j["id"] == done["job"]["id"])
    info = inspect_media(studio.path(placed["project_id"]) / job["output"])
    planned = plan["scenes"][-1]["end_time"]
    assert info["has_video"] and info["has_audio"]
    assert abs(info["duration"] - planned) < 0.6, (info, planned)
    # 같은 요청을 다시 보내도 새 작업이 생기지 않고 기록은 한 건이다.
    again = dispatch.request_render(plans, studio, plan["id"], plan["revision"])
    assert again["job"]["id"] == done["job"]["id"]
    record = dispatch.ProductionStore(plans).read(plan["id"])["dispatches"][0]
    assert len(record["render_requests"]) == 1
    assert record["plan_sha256"] == plan["approval"]["snapshot"]["plan_sha256"]


def test_project_edits_after_dispatch_block_the_render(stores, tmp_path):
    plans, studio = stores
    plan = approved(plans)
    placed = dispatch.dispatch(plans, studio, plan["id"], plan["revision"])
    attach_assets(studio, placed["project_id"], tmp_path)
    project = studio.get(placed["project_id"])
    scenes = [{**s} for s in project["scenes"]]
    scenes[0]["duration"] = scenes[0]["duration"] + 3
    studio.edit(placed["project_id"], project["revision"], scenes=scenes)
    assert dispatch.view(plans, studio, plan["id"])["state"] == "drifted"
    with pytest.raises(ValueError, match="승인된 계획과 달라졌습니다"):
        dispatch.request_render(plans, studio, plan["id"], plan["revision"])


def test_plan_revision_after_dispatch_closes_gate_and_marks_record_stale(stores):
    plans, studio = stores
    plan = approved(plans)
    dispatch.dispatch(plans, studio, plan["id"], plan["revision"])
    edited = plans.edit(
        plan["id"],
        plan["revision"],
        scenes=[
            SceneEdit(
                id=s["scene_id"],
                title=s["title"],
                narration=s["narration"],
                info_weight=s["info_weight"],
                essential=s["essential"],
                seconds=s["seconds"] + 1,
            )
            for s in plan["scenes"]
        ],
    )
    seen = dispatch.view(plans, studio, plan["id"])
    assert seen["state"] == "stale_revision" and seen["dispatchable"] is False
    assert edited["render_gate"] is False
    with pytest.raises(ValueError, match="렌더할 수 없습니다"):
        dispatch.request_render(plans, studio, plan["id"], edited["revision"])


def test_plan_beyond_renderer_limits_is_refused_with_split_reason(stores):
    plans, studio = stores
    plan = approved(
        plans,
        intent=intent(count=40),
        duration=DurationRequest(mode="preferred", seconds=900),
    )
    assert plan["render_gate"] is True
    with pytest.raises(ValueError, match="구간 디스패치와 이어 붙이기는 아직 구현되지 않았습니다"):
        dispatch.dispatch(plans, studio, plan["id"], plan["revision"])
    assert dispatch.view(plans, studio, plan["id"])["dispatchable"] is False
    assert studio.list() == []


def test_vertical_aspect_is_refused_because_the_v1_format_does_not_support_it(stores):
    plans, studio = stores
    plan = approved(plans, aspect="9:16")
    with pytest.raises(ValueError, match="화면비"):
        dispatch.dispatch(plans, studio, plan["id"], plan["revision"])
