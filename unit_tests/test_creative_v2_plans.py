from itertools import pairwise

import pytest

from ttakkari.creative.v2.duration import DurationRequest, SceneEdit
from ttakkari.creative.v2.intent import InfoItem, IntentSpec, Selection, Span
from ttakkari.creative.v2.matching import MatchResult
from ttakkari.creative.v2.plans import (
    PlanStore,
    _capability_snapshot,
    _plan_revision_hash,
    _render_authorized,
    build_plan,
)
from ttakkari.creative.v2.probe import PROBE_IDS
from ttakkari.creative.v2.registry import Registry

BRIEF = "기술 설명 영상. 요청이 처리되는 과정을 단계별로 설명해 주세요."
NARRATION = "이 단계에서는 시스템이 요청을 받아 검증하고 다음 처리로 넘기는 과정을 차례대로 설명합니다."


def registry():
    probes = {
        pid: {"installed": True, "version": None, "detail": ""} for pid in PROBE_IDS
    }
    return Registry(
        {
            "schema": 1,
            "generated_at": "t",
            "host": {},
            "command": "",
            "probes": probes,
            "records": {},
            "skills": {},
        }
    )


def intent(count=8, **kw):
    return IntentSpec(
        selections=[
            Selection(
                entry_id="C01.tech_explainer",
                role="primary",
                strength="hard",
                source="user_explicit",
                confidence=0.9,
                evidence=[Span(quote="기술 설명")],
            ),
            Selection(entry_id="C08.explainer", role="primary", confidence=0.7),
            Selection(
                entry_id="C03.vector_motion_graphics", role="primary", confidence=0.6
            ),
        ],
        info_items=[
            InfoItem(id=f"i{i}", title=f"단계 {i}", narration=NARRATION)
            for i in range(count)
        ],
        **kw,
    )


@pytest.fixture
def store(tmp_path):
    return PlanStore(tmp_path, registry=registry())


def make(store, **kw):
    plan = build_plan(BRIEF, kw.pop("intent", intent()), registry=registry(), **kw)
    return store.create(plan)


def test_auto_plan_has_expected_runtime_reasons_and_complete_scene_fields(store):
    plan = make(store)
    assert plan["basis"] == "info_items" and plan["intent_status"] == "confirmed"
    dur = plan["duration"]
    assert (
        dur["status"] == "auto"
        and dur["planned_seconds"] == plan["scenes"][-1]["end_time"]
    )
    assert (
        dur["reasons"]
        and dur["needed"]["min_seconds"]
        <= dur["planned_seconds"]
        <= dur["needed"]["max_seconds"]
    )
    scene = plan["scenes"][0]
    for key in (
        "scene_id",
        "start_time",
        "end_time",
        "narrative_goal",
        "style_dna",
        "characters",
        "environment",
        "camera",
        "motion",
        "audio",
        "required_skills",
        "selected_tools",
        "selected_models",
        "dependencies",
        "output_assets",
        "quality_checks",
    ):
        assert key in scene, key
    assert plan["scenes"][1]["dependencies"] == [plan["scenes"][0]["scene_id"]]
    assert plan["render_gate"] is False and plan["approval"] is None


def test_three_minute_preference_flows_into_scenes_and_total(store):
    plan = make(
        store,
        intent=intent(14),
        duration=DurationRequest(mode="preferred", seconds=180),
    )
    assert plan["duration"]["status"] == "target_met"
    assert (
        plan["scenes"][-1]["end_time"] == 180
        and plan["scenes"][-1]["end_frame"] == 5400
    )
    assert plan["intent"]["duration"]["seconds"] == 180
    assert sum(s["end_frame"] - s["start_frame"] for s in plan["scenes"]) == 5400


def test_user_duration_overrides_extractor_reading(store):
    plan = make(
        store,
        intent=intent(14, duration=DurationRequest(mode="preferred", seconds=60)),
        duration=DurationRequest(mode="preferred", seconds=240),
    )
    assert plan["intent"]["duration"]["seconds"] == 240


def test_too_short_request_keeps_content_and_exposes_alternatives(store):
    plan = make(
        store, intent=intent(20), duration=DurationRequest(mode="preferred", seconds=15)
    )
    assert (
        plan["duration"]["status"] == "deviated"
        and plan["duration"]["deviation_level"] == "excessive"
    )
    assert plan["duration"]["alternatives"]
    assert plan["scenes"][-1]["end_time"] > 15
    assert len(plan["scenes"]) == 20  # 내용을 깎아서 맞추지 않는다


def test_strict_conflict_keeps_editable_layout_but_blocks_approval(store):
    plan = make(
        store, intent=intent(20), duration=DurationRequest(mode="exact", seconds=30)
    )
    assert (
        plan["duration"]["status"] == "conflict"
        and plan["duration"]["planned_seconds"] is None
    )
    assert len(plan["scenes"]) == 20
    assert any("충돌" in b for b in plan["render_blockers"])
    with pytest.raises(ValueError, match="승인할 수 없습니다"):
        store.approve(plan["id"], plan["revision"])


def test_scene_edit_recomputes_total_and_invalidates_approval(store):
    plan = make(
        store,
        duration=DurationRequest(mode="preferred", seconds=120),
        intent=intent(10),
    )
    approved = store.approve(plan["id"], plan["revision"])
    assert (
        approved["render_gate"] is False
        and approved["approval"]["planned_frames"] == plan["scenes"][-1]["end_frame"]
    )
    edits = [
        SceneEdit(
            id=s["scene_id"],
            title=s["title"],
            narration=s["narration"],
            seconds=s["seconds"],
        )
        for s in plan["scenes"]
    ]
    edits[0] = edits[0].model_copy(update={"seconds": edits[0].seconds + 5})
    edited = store.edit(plan["id"], plan["revision"], scenes=edits, registry=registry())
    assert edited["revision"] == plan["revision"] + 1
    assert edited["approval"] is None and edited["render_gate"] is False
    assert edited["duration_check"]["total_seconds"] == pytest.approx(
        plan["scenes"][-1]["end_time"] + 5
    )
    assert edited["scenes"][-1]["end_time"] == pytest.approx(
        edited["duration_check"]["total_seconds"]
    )
    assert edited["basis"] == "user_edited"
    reapproved = store.approve(plan["id"], edited["revision"])
    assert reapproved["render_gate"] is False


def test_studio_runtime_approval_can_render_only_while_snapshot_matches(
    store, monkeypatch
):
    plan = make(store)
    plan["match"]["execution_scope"] = "studio_runtime"
    plan["match"]["status"] = "ready"
    for need in plan["match"]["needs"]:
        if need["priority"] == "must":
            need["status"] = "executed"
            if need["best"]:
                need["best"]["integration"] = "creative_runtime"
    store._save(plan)
    monkeypatch.setattr(
        "ttakkari.creative.v2.plans.match_intent",
        lambda _brief, _intent, _registry: MatchResult.model_validate(plan["match"]),
    )

    approved = store.approve(plan["id"], plan["revision"])
    assert approved["render_gate"] is True
    assert store.assert_renderable(plan["id"], plan["revision"])["render_gate"] is True

    renamed = store.get(plan["id"])
    renamed["name"] += " 수정"
    store._save(renamed)
    assert store.get(plan["id"])["render_gate"] is False
    renamed["name"] = plan["name"]
    store._save(renamed)
    assert store.get(plan["id"])["render_gate"] is True

    monkeypatch.setattr(
        "ttakkari.creative.v2.plans.match_intent",
        lambda _brief, _intent, _registry: MatchResult.model_validate(
            plan["match"]
            | {"execution_scope": "lab_manual", "status": "ready_unverified"}
        ),
    )
    assert store.get(plan["id"])["render_gate"] is False
    monkeypatch.setattr(
        "ttakkari.creative.v2.plans.match_intent",
        lambda _brief, _intent, _registry: MatchResult.model_validate(plan["match"]),
    )

    tampered = store.get(plan["id"])
    tampered["match"]["status"] = "ready_unverified"
    store._save(tampered)
    assert store.get(plan["id"])["render_gate"] is False
    with pytest.raises(ValueError, match="렌더할 수 없습니다"):
        store.assert_renderable(plan["id"], plan["revision"])


def test_lab_manual_plan_never_opens_render_gate_after_approval(store):
    plan = make(store)
    assert plan["match"]["execution_scope"] == "lab_manual"
    approved = store.approve(plan["id"], plan["revision"])
    assert approved["approval"] is not None
    assert approved["render_gate"] is False


def test_approval_persists_immutable_plan_and_capability_snapshot(store):
    plan = make(store)
    approved = store.approve(plan["id"], plan["revision"])
    snapshot = approved["approval"]["snapshot"]
    assert snapshot["revision"] == plan["revision"]
    assert len(snapshot["plan_sha256"]) == 64
    assert (
        snapshot["capabilities"]["execution_scope"] == plan["match"]["execution_scope"]
    )
    assert approved["render_gate"] is False


@pytest.mark.parametrize("status", ["ready_unverified", "blocked"])
def test_unverified_or_blocked_status_never_authorizes_render(store, status):
    plan = make(store)
    plan["match"]["execution_scope"] = "studio_runtime"
    plan["match"]["status"] = status
    plan["approval"] = {
        "revision": plan["revision"],
        "snapshot": {
            "revision": plan["revision"],
            "plan_sha256": _plan_revision_hash(plan),
            "capabilities": _capability_snapshot(plan["match"]),
        },
    }
    assert _render_authorized(plan) is False


def test_edit_that_cuts_speech_is_blocked_from_approval_with_fit_suggestion(store):
    plan = make(
        store, intent=intent(4), duration=DurationRequest(mode="exact", seconds=45)
    )
    assert plan["duration"]["status"] == "strict_met"
    edits = [
        SceneEdit(
            id=s["scene_id"],
            title=s["title"],
            narration=s["narration"],
            seconds=s["seconds"],
        )
        for s in plan["scenes"]
    ]
    edits[1] = edits[1].model_copy(update={"seconds": 2})
    edited = store.edit(plan["id"], plan["revision"], scenes=edits, registry=registry())
    check = edited["duration_check"]
    assert (
        check["status"] == "violation"
        and any("잘립니다" in e for e in check["errors"])
        and any("정확한 길이" in e for e in check["errors"])
    )
    assert edited["render_gate"] is False
    with pytest.raises(ValueError, match="승인할 수 없습니다"):
        store.approve(plan["id"], edited["revision"])


def test_changing_duration_replans_scenes_from_current_content(store):
    plan = make(
        store,
        intent=intent(14),
        duration=DurationRequest(mode="preferred", seconds=170),
    )
    replanned = store.edit(
        plan["id"],
        plan["revision"],
        duration=DurationRequest(mode="preferred", seconds=220),
        registry=registry(),
    )
    assert replanned["duration"]["request"]["seconds"] == 220
    assert replanned["scenes"][-1]["end_time"] == 220
    assert replanned["history"][-1]["kind"] == "duration_replanned"


def test_stale_revision_conflicts(store):
    plan = make(store)
    store.edit(
        plan["id"],
        plan["revision"],
        duration=DurationRequest(mode="preferred", seconds=90),
        registry=registry(),
    )
    with pytest.raises(ValueError, match="다른 변경"):
        store.edit(
            plan["id"],
            plan["revision"],
            duration=DurationRequest(mode="preferred", seconds=60),
            registry=registry(),
        )
    with pytest.raises(ValueError, match="다른 변경"):
        store.approve(plan["id"], plan["revision"])


def test_brief_only_plan_is_unconfirmed_hint_and_cannot_be_approved(store):
    plan = build_plan(
        "사이버펑크 SF 영상을 만들어 줘",
        None,
        registry=registry(),
        duration=DurationRequest(mode="preferred", seconds=45),
    )
    saved = store.create(plan)
    assert (
        saved["intent_status"] == "unconfirmed_hint"
        and saved["basis"] == "template_scaffold"
    )
    assert any("단어 힌트" in n for n in saved["notes"]) and any(
        "정보 항목이 없어" in n for n in saved["notes"]
    )
    assert all(s["strength"] == "soft" for s in saved["intent"]["selections"])
    with pytest.raises(ValueError, match="의도가 확인되지 않았습니다"):
        store.approve(saved["id"], saved["revision"])


def test_template_scaffold_does_not_pad_to_long_target(store):
    plan = build_plan(
        BRIEF,
        IntentSpec(
            selections=[
                Selection(entry_id="C01.tech_explainer", role="primary", confidence=0.6)
            ]
        ),
        registry=registry(),
        duration=DurationRequest(mode="preferred", seconds=600),
    )
    assert plan["duration"]["status"] == "deviated"
    assert plan["scenes"][-1]["end_time"] < 600
    assert any(a["kind"] == "extend_content" for a in plan["duration"]["alternatives"])


def test_three_short_info_items_deviate_from_six_hundred_seconds_without_padding(store):
    short_intent = IntentSpec(
        selections=[
            Selection(
                entry_id="C01.tech_explainer",
                role="primary",
                strength="hard",
                source="user_explicit",
                confidence=0.9,
                evidence=[Span(quote="기술 설명")],
            )
        ],
        info_items=[
            InfoItem(id=f"i{i}", title=f"정보 {i}", narration="") for i in range(3)
        ],
    )
    plan = make(
        store,
        intent=short_intent,
        duration=DurationRequest(mode="preferred", seconds=600),
    )
    assert plan["duration"]["status"] == "deviated"
    assert plan["duration"]["planned_seconds"] == 24
    assert any(
        "3개" in reason and "24.0초" in reason for reason in plan["duration"]["reasons"]
    )
    assert all(
        alt["requires_user_approval"] for alt in plan["duration"]["alternatives"]
    )


def test_unresolved_style_conflict_blocks_approval_and_records_choice(store, tmp_path):
    from ttakkari.creative.v2.style_dna import (
        Resolution,
        StyleRef,
        StyleStore,
        seed_docs,
    )

    styles = StyleStore(tmp_path / "styles", seeds=seed_docs())
    refs = [
        StyleRef(style_id="cute_3d", version="1.0.0"),
        StyleRef(style_id="horror_lighting", version="1.0.0"),
    ]
    plan = store.create(
        build_plan(BRIEF, intent(), registry=registry(), styles=styles, style_refs=refs)
    )
    assert plan["style"]["status"] == "unresolved"
    assert any("Style DNA" in b for b in plan["render_blockers"])
    res = [
        Resolution(
            field="lighting",
            decision="take",
            winner="horror_lighting@1.0.0",
            rationale="공포 조명을 우선한다",
            decided_by="user",
        ),
        Resolution(
            field="sound_identity",
            decision="take",
            winner="horror_lighting@1.0.0",
            rationale="공포 사운드를 우선한다",
            decided_by="user",
        ),
    ]
    resolved = store.create(
        build_plan(
            BRIEF,
            intent(),
            registry=registry(),
            styles=styles,
            style_refs=refs,
            resolutions=res,
        )
    )
    assert (
        resolved["style"]["status"] == "resolved"
        and resolved["scenes"][0]["style_dna"] == resolved["style"]["sha256"]
    )
    assert store.approve(resolved["id"], resolved["revision"])["render_gate"] is False


def test_invalid_ids_and_missing_plan(store):
    with pytest.raises(ValueError):
        store.get("not-a-uuid")
    with pytest.raises(FileNotFoundError):
        store.get("33333333-3333-4333-8333-333333333333")


def test_unreviewed_llm_scalars_cannot_change_plan_duration_or_content():
    proposed = IntentSpec.model_validate({
        "selections": [{
            "entry_id": "C01.tech_explainer", "role": "primary", "strength": "soft",
            "source": "llm_inferred", "evidence": [{"quote": "귀여운 강아지"}],
        }],
        "duration": {"mode": "preferred", "seconds": 900},
        "aspect": "1:1", "music_seconds": 890,
        "info_items": [{"id": "i1", "title": "주장", "narration": "임의 정보", "essential": False,
                        "evidence": [{"quote": "귀여운 강아지"}]}],
        "open_questions": ["의도를 검토해 주세요."],
        "extraction": {"method": "llm", "model": "mock"},
    })

    with pytest.raises(ValueError, match="검토 전 의도 초안"):
        build_plan(
            "비오는 날 귀여운 강아지가 걷는 10초 영상", proposed,
            duration=DurationRequest(mode="auto"), aspect="16:9", registry=registry(),
        )


def test_long_plan_gets_split_render_segments_with_resume_keys(store):
    plan = make(store, intent=intent(count=40))
    rp = plan["render_plan"]
    total = plan["scenes"][-1]["end_frame"]
    assert plan["scenes"][-1]["end_time"] > 240 and rp["requires_split"] is True
    segs = rp["segments"]
    assert len(segs) > 1 and rp["executed"] is False
    # 구간은 장면 경계에서 빈틈과 겹침 없이 전체를 덮는다.
    assert segs[0]["start_frame"] == 0 and segs[-1]["end_frame"] == total
    assert all(a["end_frame"] == b["start_frame"] for a, b in pairwise(segs))
    assert [sid for seg in segs for sid in seg["scene_ids"]] == [
        s["scene_id"] for s in plan["scenes"]
    ]
    assert all(seg["seconds"] <= 60 or len(seg["scene_ids"]) == 1 for seg in segs)
    assert rp["join"]["inputs"] == [seg["id"] for seg in segs]
    assert len({seg["input_sha256"] for seg in segs}) == len(segs)


def test_short_plan_is_a_single_segment_without_split(store):
    rp = make(store, intent=intent(count=3))["render_plan"]
    assert rp["requires_split"] is False and len(rp["segments"]) == 1


def test_segment_keys_ignore_position_so_unchanged_later_segments_can_resume(store):
    base = make(store, intent=intent(count=40))
    first_scene = base["scenes"][0]
    longer = store.edit(
        base["id"],
        base["revision"],
        scenes=[
            SceneEdit(
                id=s["scene_id"],
                title=s["title"],
                narration=s["narration"],
                info_weight=s["info_weight"],
                essential=s["essential"],
                seconds=s["seconds"] + (1.0 if s["scene_id"] == first_scene["scene_id"] else 0),
            )
            for s in base["scenes"]
        ],
    )
    old = {tuple(g["scene_ids"]): g["input_sha256"] for g in base["render_plan"]["segments"]}
    new = {tuple(g["scene_ids"]): g["input_sha256"] for g in longer["render_plan"]["segments"]}
    changed = [ids for ids in old if ids in new and old[ids] != new[ids]]
    assert changed == [tuple(base["render_plan"]["segments"][0]["scene_ids"])]


def test_render_plan_is_derived_not_stored_and_not_part_of_approval_hash(store, tmp_path):
    plan = make(store)
    saved = (tmp_path / plan["id"] / "plan.json").read_text()
    assert "render_plan" not in saved
    assert _plan_revision_hash(plan) == _plan_revision_hash(
        {k: v for k, v in plan.items() if k != "render_plan"}
    )


def test_gate_reasons_explain_every_closed_gate_and_are_empty_when_open(store, tmp_path):
    # 미승인
    plan = make(store)
    assert plan["render_gate"] is False
    assert any("승인되지 않았습니다" in r for r in plan["render_gate_reasons"])
    # 승인했지만 lab 수동 범위
    approved = store.approve(plan["id"], plan["revision"])
    assert approved["render_gate"] is False
    assert any("스튜디오 렌더러로 실행할 수 없습니다" in r for r in approved["render_gate_reasons"])
    assert any("연결되지 않았거나" in r for r in approved["render_gate_reasons"])
    # 기준선 계획은 승인 뒤 열리고 이유가 없다
    real = PlanStore(tmp_path / "real", registry=Registry())
    baseline = real.create(
        build_plan(
            BRIEF,
            IntentSpec(
                selections=[
                    Selection(entry_id="C08.three_act", role="primary", confidence=0.8),
                    Selection(entry_id="C10.dissolve", role="secondary", confidence=0.8),
                ],
                info_items=[
                    InfoItem(id=f"i{i}", title=f"장면 {i}", narration=NARRATION)
                    for i in range(3)
                ],
            ),
            registry=Registry(),
        )
    )
    assert baseline["render_gate"] is False and baseline["render_gate_reasons"]
    opened = real.approve(baseline["id"], baseline["revision"])
    assert opened["render_gate"] is True and opened["render_gate_reasons"] == []
    # 승인 뒤 장면을 바꾸면 다시 닫히고 이유가 나온다
    edited = real.edit(
        baseline["id"],
        baseline["revision"],
        scenes=[
            SceneEdit(
                id=s["scene_id"], title=s["title"], narration=s["narration"],
                info_weight=s["info_weight"], essential=s["essential"], seconds=s["seconds"] + 1,
            )
            for s in opened["scenes"]
        ],
    )
    assert edited["render_gate"] is False
    assert any("승인되지 않았습니다" in r for r in edited["render_gate_reasons"])


def test_gate_is_open_exactly_when_there_are_no_reasons(store):
    for plan in (make(store), store.approve(make(store)["id"], 1)):
        assert plan["render_gate"] == (plan["render_gate_reasons"] == [])
