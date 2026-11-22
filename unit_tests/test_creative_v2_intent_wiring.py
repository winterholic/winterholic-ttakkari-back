"""의도 v2 → 계획 → 레시피 매칭 → 제작 결정 → 렌더 제어 연결. 특정 시나리오가 아니라 연결 규칙을 확인한다.

각 시험은 연결을 끊었을 때 실패해야 한다(stale 제거, 답변 소비, 필수 조건 연결, 길이·엔진 한도 표시, 톤 매칭, 렌더 제어).
"""

import httpx
import pytest
from fastapi import FastAPI
from test_creative_v2_core_integration import direction

from ttakkari.api import creative_v2 as api
from ttakkari.api import creative_v2_intent as intent_api
from ttakkari.creative.service import Studio
from ttakkari.creative.v2 import dispatch, engines
from ttakkari.creative.v2.duration import DurationRequest
from ttakkari.creative.v2.intent import IntentSpec, Selection, Span
from ttakkari.creative.v2.intent_plan import PlanIntentError
from ttakkari.creative.v2.intent_resolve import ResolveRequest, resolve_intent
from ttakkari.creative.v2.matching import TONE_FITS, TONE_WEIGHT, match_intent
from ttakkari.creative.v2.planner import ProductionDirection
from ttakkari.creative.v2.plans import PlanStore, build_plan
from ttakkari.creative.v2.registry import Registry
from ttakkari.creative.v2.style_dna import StyleStore
from ttakkari.security.auth import require_user

NARRATION = "첫 번째 장면에서는 준비한 사진을 천천히 보여 주고 핵심만 짧게 설명합니다."


def items(count=3):
    return [{"id": f"i{i}", "title": f"장면 {i + 1}", "narration": NARRATION} for i in range(count)]


def resolve(brief, selections=(), *, duration=None, aspect=None, **kw):
    structured = {"selections": [dict(s) for s in selections]}
    if duration:
        structured["duration"] = duration
    if aspect:
        structured["aspect"] = aspect
    return resolve_intent(ResolveRequest(brief=brief, structured=structured, **kw))


def plan_of(result, **kw):
    assert result.ready_for_plan, result.blocking
    return build_plan(
        result.intent.brief, None, registry=Registry(), intent_v2=result.intent, **kw
    )


def blockers(plan):
    from ttakkari.creative.v2.plans import _blockers

    return _blockers(plan)


def slideshow_result(**kw):
    """정지 이미지 슬라이드쇼(R21) 기준선으로 가는 조합. 전달 항목이 있고 사용자가 전환·카메라를 정했다."""
    return resolve(
        "내가 가진 사진으로 영상을 만든다",
        [
            {"id": "C01.vlog", "role": "primary"},
            {"id": "C08.three_act"},
            {"id": "C10.dissolve"},
            {"id": "C06.dolly"},
        ],
        duration={"mode": "auto"},
        aspect=kw.pop("aspect", "16:9"),
        info_items=items(),
        **kw,
    )


# --- 자연어 수정 직후 이전 의도 ---------------------------------------------------


def test_editing_the_brief_drops_free_text_conditions_whose_quote_is_gone():
    first = resolve(
        "공포 액션 애니메이션을 2분 길이로 만들어 줘",
        [{"id": "C01.animation", "role": "primary"}],
    )
    assert {c.id for c in first.intent.conditions} >= {"tone.horror", "tone.action"}
    assert first.intent.duration.seconds == 120
    second = resolve_intent(
        ResolveRequest(
            brief="감성적인 음악에 어울리는 영상을 만들어 줘",
            intent=first.intent.model_dump(mode="json"),
        )
    )
    ids = {c.id for c in second.intent.conditions}
    assert not ids & {"tone.horror", "tone.action"}
    assert "tone.emotional" in ids
    assert "C01.animation" in ids  # 사용자가 확정한 조건은 브리프가 바뀌어도 남는다
    assert second.intent.duration.seconds is None  # 이전 문장의 2분이 새 계획으로 새지 않는다
    assert any(h.action == "stale_text_removed" for h in second.intent.history)
    # 확정 조건은 남지만 사라진 문장의 인용은 지워, 이후 계획 검증이 낡은 인용 때문에 막히지 않는다.
    kept = next(c for c in second.intent.conditions if c.id == "C01.animation")
    assert kept.evidence == [] and kept.origin == "user_confirmed"
    assert any(h.action == "evidence_pruned" for h in second.intent.history)
    assert plan_of(second)["intent_v2"]["revision"] == second.intent.revision


def test_plan_refuses_an_intent_whose_text_evidence_is_not_in_the_brief():
    first = resolve(
        "공포 액션 애니메이션을 만들어 줘", [{"id": "C01.animation", "role": "primary"}]
    )
    edited = first.intent.model_copy(update={"brief": "감성 영상을 만들어 줘"})
    with pytest.raises(PlanIntentError) as caught:
        build_plan("감성 영상을 만들어 줘", None, registry=Registry(), intent_v2=edited)
    assert caught.value.code == "intent_stale"
    assert {r["id"] for r in caught.value.reasons} >= {"stale.tone.horror", "stale.tone.action", "stale.C01.animation"}


# --- 정보 구조 ---------------------------------------------------------------------


def test_info_items_sent_to_resolve_become_the_plan_scenes():
    result = slideshow_result()
    assert len(result.intent.info_items) == 3
    plan = plan_of(result)
    assert plan["basis"] == "info_items"
    assert [s["scene_id"] for s in plan["scenes"]] == ["i0", "i1", "i2"]
    assert any(h.action == "info_items_set" for h in result.intent.history)
    # 비우기와 이어받기를 구분한다.
    kept = resolve_intent(ResolveRequest(brief=result.intent.brief, intent=result.intent.model_dump(mode="json")))
    assert len(kept.intent.info_items) == 3
    cleared = resolve_intent(
        ResolveRequest(brief=result.intent.brief, intent=result.intent.model_dump(mode="json"), info_items=[])
    )
    assert cleared.intent.info_items == []


# --- 답변 → 제작 결정 --------------------------------------------------------------


def anime_result(**kw):
    return resolve(
        "일본 애니메이션 스타일의 SF 단편을 만든다",
        [
            {"id": "C01.animation", "role": "primary", "strength": "required"},
            {"id": "C03.japanese_anime", "strength": "required"},
        ],
        duration={"mode": "target", "seconds": 30},
        aspect="16:9",
        answers=[
            *kw.pop("extra", []),
            {"field_id": "drawing.shading", "values": ["cel"]},
            {"field_id": "cut.tempo", "values": ["slow_build_burst"]},
            {"field_id": "light.key", "values": ["C05.neon"]},
            {"field_id": "character.expression", "values": ["exaggerated"]},
        ],
        confirm=kw.pop("confirm", []),
        **kw,
    )


def test_answers_become_categorized_decisions_instead_of_not_applied():
    plan = plan_of(anime_result())
    decisions = {d["id"]: d for d in plan["production_decisions"]["decisions"]}
    assert decisions["d.drawing.shading"]["category"] == "visual"
    assert "셀 셰이딩" in decisions["d.drawing.shading"]["directive"]
    assert decisions["d.light.key"]["category"] == "lighting"
    assert decisions["d.cut.tempo"]["category"] == "editing"
    assert decisions["d.drawing.shading"]["origin"] == "user_confirmed"
    not_applied = {n["id"] for n in plan["intent_projection"]["not_applied"]}
    assert not {"answer.drawing.shading", "answer.cut.tempo", "answer.light.key"} & not_applied
    answers = {i["id"]: i for i in plan["intent_projection"]["items"] if i["kind"] == "answer"}
    assert answers["answer.drawing.shading"]["applied_to"] == ["production_decision:d.drawing.shading"]
    assert answers["answer.drawing.shading"]["execution"] == "plan_only"


def test_scenes_carry_concrete_decisions_and_pacing_by_position():
    plan = plan_of(anime_result())
    first, last = plan["scenes"][0], plan["scenes"][-1]
    assert any("셀 셰이딩" in t for t in first["production"]["by_category"]["visual"])
    assert any("빌드업" in t for t in first["production"]["by_category"]["editing"])
    assert any("폭발" in t for t in last["production"]["by_category"]["editing"])
    assert set(first["information"]) >= {"seconds", "ideal_seconds", "ratio", "treatment", "drop_candidate"}


def test_capability_that_does_not_exist_is_unsupported_with_reason_and_stays_out_of_scenes():
    plan = plan_of(anime_result())
    expression = next(d for d in plan["production_decisions"]["decisions"] if d["id"] == "d.character.expression")
    assert expression["status"] == "unsupported"
    assert "anim.expression" in expression["status_reason"]
    assert any(
        n["id"] == "d.character.expression" and "지원 불가" in n["reason"]
        for n in plan["intent_projection"]["not_applied"]
    )
    assert not any(
        "표정을 과장" in text
        for scene in plan["scenes"]
        for texts in scene["production"]["by_category"].values()
        for text in texts
    )
    assert plan["production_decisions"]["counts"]["unsupported"] >= 1


def test_answers_for_hidden_fields_are_reported_not_silently_dropped():
    # 공포 톤이 없으면 공포 설정 묶음이 꺼진다. 그래도 사용자가 한 답변은 의도에 남고 계획에서 "숨겨져 옮기지 않음"으로 보인다.
    result = anime_result(extra=[{"field_id": "horror.style", "values": ["dread"]}])
    plan = plan_of(result)
    ids = {d["id"] for d in plan["production_decisions"]["decisions"]}
    assert "d.horror.style" not in ids
    assert any(a.field_id == "horror.style" for a in result.intent.answers)
    hidden = [n for n in plan["production_decisions"]["not_applied"] if n["id"] == "answer.horror.style"]
    assert hidden and "숨겨" in hidden[0]["reason"]
    assert any(n["id"] == "answer.horror.style" for n in plan["intent_projection"]["not_applied"])


# --- 렌더 제어로 이어지는 조건 ----------------------------------------------------------


def test_user_conditions_reach_the_engine_as_render_controls():
    plan = plan_of(slideshow_result())
    assert plan["match"]["primary_recipe"] == "R21"
    controls = plan["production_decisions"]["render_controls"]
    assert controls["transition"]["value"] == "fade"
    assert controls["animation"]["value"] == "zoom_in"
    by_id = {d["id"]: d for d in plan["production_decisions"]["decisions"]}
    assert by_id["d.C10.dissolve"]["status"] == "executable"
    assert by_id["d.C10.dissolve"]["consumer"] == ["skill.v1_ffmpeg_slideshow"]
    style = dispatch._style_defaults(plan, engines.V1SlideshowAdapter())
    assert style["applied"] == {"animation": "zoom_in", "transition": "fade"}
    assert style["user_controls"] == {"animation": "d.C06.dolly", "transition": "d.C10.dissolve"}
    executable = {i["id"]: i.get("execution") for i in plan["intent_projection"]["items"]}
    assert executable["C10.dissolve"] == "executable"


def test_user_controls_win_over_style_dna_but_unconfirmed_values_do_not():
    plan = plan_of(slideshow_result())
    plan["style"] = {
        "sha256": "s" * 64,
        "dna": {"motion_language": {"text": "x", "params": {"animation": "pan", "transition": "cut"}}},
    }
    assert dispatch._style_defaults(plan, engines.V1SlideshowAdapter())["applied"]["transition"] == "fade"
    # 같은 조건을 AI 해석(미확정)으로 낮추면 렌더 제어가 되지 않는다.
    spec = slideshow_result().intent
    lowered = spec.model_copy(update={"conditions": [
        c.model_copy(update={"origin": "ai_inferred", "confirmation": None, "strength": "preferred",
                             "evidence": c.evidence or [Span(quote="사진")]})
        if c.id == "C10.dissolve" else c
        for c in spec.conditions
    ]})
    soft = build_plan(lowered.brief, None, registry=Registry(), intent_v2=lowered)
    assert "transition" not in soft["production_decisions"]["render_controls"]
    dissolve = next(d for d in soft["production_decisions"]["decisions"] if d["id"] == "d.C10.dissolve")
    assert dissolve["status"] == "plan_only"
    assert "확정" in dissolve["status_reason"]


def test_engine_that_does_not_accept_the_value_keeps_it_plan_only_and_records_ignored():
    plan = plan_of(slideshow_result())
    plan["production_decisions"]["render_controls"]["transition"]["value"] = "wipe"
    style = dispatch._style_defaults(plan, engines.V1SlideshowAdapter())
    assert "transition" not in style["applied"]
    assert any("wipe" in i for i in style["ignored"])


def test_decisions_survive_a_scene_edit_and_change_the_segment_key(tmp_path):
    from ttakkari.creative.v2.duration import SceneEdit
    from ttakkari.creative.v2.plans import render_plan

    store = PlanStore(tmp_path / "plans", registry=Registry())
    created = store.create(plan_of(slideshow_result()))
    edited = store.edit(
        created["id"], created["revision"],
        scenes=[SceneEdit(id=f"i{i}", title=f"장면 {i + 1}", narration=NARRATION, seconds=6) for i in range(3)],
    )
    assert edited["production_decisions"]["render_controls"]["transition"]["value"] == "fade"
    assert all(s["production"]["decision_ids"] for s in edited["scenes"])
    assert render_plan(created)["segments"][0]["input_sha256"] != render_plan(edited)["segments"][0]["input_sha256"]


# --- 필수 조건 연결 ----------------------------------------------------------------


def test_required_condition_that_no_path_realizes_blocks_approval(tmp_path):
    result = resolve(
        "제품 소개 광고를 만든다",
        [
            {"id": "C01.advertisement", "role": "primary", "strength": "required"},
            {"id": "C03.comic_2d", "strength": "required"},
        ],
        duration={"mode": "auto"},
        aspect="1:1",
    )
    plan = plan_of(result)
    rows = {r["id"]: r for r in plan["production_decisions"]["required_conditions"]}
    assert rows["C01.advertisement"]["status"] == "realized"
    assert rows["C03.comic_2d"]["status"] == "unrealized"
    assert any("C03.comic_2d" in b for b in blockers(plan))
    store = PlanStore(tmp_path / "plans", registry=Registry())
    created = store.create(plan)
    with pytest.raises(ValueError, match="C03.comic_2d"):
        store.approve(created["id"], created["revision"])


def test_required_condition_realized_by_the_chosen_recipe_is_not_blocked():
    plan = plan_of(anime_result())
    rows = {r["id"]: r for r in plan["production_decisions"]["required_conditions"]}
    assert rows["C03.japanese_anime"]["realized_by"] == ["recipe"]
    assert not any("필수 조건" in b for b in blockers(plan))


# --- 레시피 매칭은 톤을 고려한다 -------------------------------------------------------


def test_tone_moves_recipe_choice_without_making_unfit_recipes_eligible():
    base = IntentSpec(
        selections=[Selection(entry_id="C01.story_film", role="primary")],
        extraction={"method": "user_structured"},
    )
    plain = match_intent("x", base)
    horror = match_intent("x", base, tone=["tone.horror"])
    registry = Registry()
    fits = lambda r: set(registry.recipes[r].fits) & set(TONE_FITS["tone.horror"])
    scores = lambda m: {c.recipe_id: c.score for c in m.candidates}
    assert not fits(plain.primary_recipe)
    assert fits(horror.primary_recipe)  # 톤을 드러내는 항목을 fits로 가진 레시피가 주 레시피가 된다
    assert plain.primary_recipe != horror.primary_recipe
    assert scores(horror)["R20"] == pytest.approx(scores(plain)["R20"] + TONE_WEIGHT)
    assert scores(horror)[plain.primary_recipe] == scores(plain)[plain.primary_recipe]
    assert plain.fingerprint != horror.fingerprint
    assert match_intent("x", base, tone=[]).fingerprint == plain.fingerprint
    # 어떤 레시피도 맞지 않는 선택은 톤이 있어도 레시피를 얻지 못한다.
    odd = IntentSpec(selections=[Selection(entry_id="C01.vlog", role="primary")], extraction={"method": "user_structured"})
    assert match_intent("x", odd, tone=["tone.horror"]).primary_recipe is None


def test_plan_records_the_tone_used_for_matching_and_recomputes_the_same_match(tmp_path):
    result = resolve(
        "무서운 이야기를 영화로 만든다",
        [{"id": "C01.story_film", "role": "primary"}],
        duration={"mode": "auto"},
        aspect="16:9",
    )
    assert "tone.horror" in {c.id for c in result.intent.conditions}
    plan = plan_of(result)
    assert plan["match_tone"] == ["tone.horror"]
    assert "match.recipe_score" in next(
        i for i in plan["intent_projection"]["items"] if i["id"] == "tone.horror"
    )["applied_to"]
    store = PlanStore(tmp_path / "plans", registry=Registry())
    created = store.create(plan)
    # get()은 저장된 톤으로 능력 판정을 다시 계산한다. 지문이 어긋나면 승인 게이트가 닫힌다.
    assert store.get(created["id"])["match"]["fingerprint"] == plan["match"]["fingerprint"]


# --- 길이·정보량, 엔진 한도 --------------------------------------------------------------


def test_user_length_far_beyond_the_information_is_a_blocker_until_the_user_decides(tmp_path):
    result = resolve(
        "감성적인 음악에 어울리는 3분짜리 뮤직비디오",
        [{"id": "C01.music_video", "role": "primary", "strength": "required"}],
        duration={"mode": "target", "seconds": 180},
        aspect="16:9",
    )
    plan = plan_of(result)
    length = plan["production_decisions"]["length"]
    assert length["treatment"] == "capacity_short"
    assert length["requested"]["seconds"] == 180 and length["planned_seconds"] < 60
    assert any("180" in b and "모자라" in b for b in blockers(plan))
    store = PlanStore(tmp_path / "plans", registry=Registry())
    created = store.create(plan)
    relaxed = store.edit(
        created["id"], created["revision"],
        duration=DurationRequest(mode="preferred", seconds=length["capacity"]["max_seconds"]),
    )
    assert relaxed["production_decisions"]["length"]["treatment"] == "fit"
    assert not any("모자라" in b for b in relaxed["render_blockers"])


def test_engine_limit_conflict_is_visible_at_plan_time_not_only_at_dispatch():
    plan = plan_of(slideshow_result(aspect="9:16"))
    fit = plan["production_decisions"]["engine_fit"]
    assert fit["status"] == "blocked"
    assert any("화면비 9:16" in r for r in fit["reasons"])
    ok = plan_of(slideshow_result())["production_decisions"]["engine_fit"]
    assert ok["status"] == "ok" and ok["engines"] == ["skill.v1_ffmpeg_slideshow"]


def test_engines_can_differ_per_scene_and_the_plan_reports_each_segment_engine():
    result = slideshow_result()
    plan = plan_of(
        result,
        director=ProductionDirection.model_validate(
            direction(("generative_video", "frame_animation_2d", "scene_3d"))
        ),
    )
    fit = plan["production_decisions"]["engine_fit"]
    assert fit["mixed_engines"] is True
    assert [s["engine"] for s in fit["segments"]] == [
        "skill.comfy_video_renderer", "skill.frame_animation_2d", "skill.studio-3d-cinematic",
    ]
    # 사용자가 고른 카메라(돌리)를 프리셋 엔진은 소비하지 못한다. 조건을 버리지 않고 렌더 전에 차단 이유로 보인다.
    assert any("skill.frame_animation_2d" in r and "camera/motion" in r for r in fit["reasons"])
    assert not any("skill.comfy_video_renderer" in r for r in fit["reasons"])
    assert fit["status"] == "blocked"


def test_reviewed_storyboard_keeps_the_decisions_and_reports_the_director_engines(stores):
    import json
    import uuid

    from test_creative_v2_stage3 import output
    from test_creative_v2_stage3 import request as storyboard_request

    from ttakkari.creative.v2 import storyboard as sb

    plans, _ = stores
    base = storyboard_request()
    spec = resolve(
        base.brief,
        [{"id": "C01.vlog", "role": "primary"}, {"id": "C08.three_act"}, {"id": "C10.dissolve"}],
        duration={"mode": "target", "seconds": 9},
        aspect="16:9",
    ).intent
    body = sb.StoryboardRequest.model_validate({**base.model_dump(), "intent_v2": spec.model_dump()})
    raw = json.dumps(output(), ensure_ascii=False)
    plan = sb.materialize(
        plans, body, raw, run_id=str(uuid.uuid4()), model="fixture", reviewed_output_sha256=sb.output_hash(raw),
    )
    decisions = plan["production_decisions"]
    assert decisions["render_controls"]["transition"]["value"] == "fade"
    assert all(s["production"]["decision_ids"] and s["information"] for s in plan["scenes"])
    assert decisions["engine_fit"]["engines"] == ["skill.v1_ffmpeg_slideshow"]
    assert decisions["intent_revision"] == spec.revision


# --- 실제 제작까지 -----------------------------------------------------------------------


@pytest.fixture
def stores(tmp_path):
    from test_creative_v2_dispatch import stores as _stores

    return _stores.__wrapped__(tmp_path)


def test_user_decisions_reach_the_rendered_project_and_the_production_record(stores, tmp_path):
    from test_creative_v2_dispatch import run_renderer, upload_all

    plans, studio = stores
    created = plans.create(plan_of(slideshow_result()))
    plan = plans.approve(created["id"], created["revision"])
    assert plan["render_gate"], plan["render_gate_reasons"]
    dispatch.dispatch(plans, studio, plan["id"], plan["revision"])
    upload_all(plans, plan, tmp_path)
    dispatch.request_render(plans, studio, plan["id"], plan["revision"])
    run_renderer(studio)
    done = dispatch.view(plans, studio, plan["id"])
    assert done["state"] == "succeeded", done
    record = dispatch.ProductionStore(plans).read(plan["id"])["dispatches"][0]
    assert record["style"]["applied"] == {"animation": "zoom_in", "transition": "fade"}
    assert record["style"]["user_controls"] == {"animation": "d.C06.dolly", "transition": "d.C10.dissolve"}
    # 렌더러가 받은 장면이 실제로 사용자가 정한 값을 갖는다.
    projects = [p for p in studio.list() if p["render_jobs"]]
    scenes = [sc for p in projects for sc in p["scenes"]]
    assert scenes and all(sc.get("animation") == "zoom_in" for sc in scenes)
    assert rubric_decisions(record) == ["d.C06.dolly", "d.C10.dissolve"]


def rubric_decisions(record):
    return sorted(d["id"] for d in record["rubric"]["intent_decisions"] if d["status"] == "executable")


# --- API 경계 --------------------------------------------------------------------------


@pytest.fixture
def application(tmp_path, monkeypatch):
    plans = PlanStore(tmp_path / "plans")
    monkeypatch.setattr(api, "default_plan_store", lambda: plans)
    monkeypatch.setattr(api, "default_style_store", lambda: StyleStore(tmp_path / "styles"))
    monkeypatch.setattr(api, "default_studio", lambda: Studio(tmp_path / "studio"))
    app = FastAPI()
    app.include_router(api.router)
    app.include_router(intent_api.router)
    app.dependency_overrides[require_user] = lambda: "owner"
    return app


async def test_http_resolve_then_plan_shows_decisions_and_rejects_stale_text(application):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=application), base_url="http://test") as client:
        resolved = await client.post("/api/creative/v2/intent/resolve", json={
            "brief": "일본 애니메이션 SF 공포 액션을 2분으로",
            "structured": {"selections": [{"id": "C01.animation", "role": "primary"}], "aspect": "16:9"},
            "answers": [{"field_id": "drawing.shading", "values": ["cel"]}],
            "info_items": items(),
        })
        assert resolved.status_code == 200, resolved.text
        intent = resolved.json()["intent"]
        assert len(intent["info_items"]) == 3
        created = await client.post("/api/creative/v2/plans", json={"brief": intent["brief"], "intent_v2": intent})
        assert created.status_code == 201, created.text
        body = created.json()
        assert any(d["id"] == "d.drawing.shading" for d in body["production_decisions"]["decisions"])
        edited = dict(intent, brief="감성 단편 애니메이션")
        stale = await client.post("/api/creative/v2/plans", json={"brief": "감성 단편 애니메이션", "intent_v2": edited})
        assert stale.status_code == 422
        assert stale.json()["code"] == "intent_stale"
