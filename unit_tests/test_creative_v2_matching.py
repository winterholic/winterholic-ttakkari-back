import pytest

from ttakkari.creative.v2.capabilities import GLOBAL_REQUIRES
from ttakkari.creative.v2.duration import DurationRequest
from ttakkari.creative.v2.intent import (
    Extraction,
    IntentConstraint,
    IntentSpec,
    Selection,
    Span,
    extraction_contract,
    hint_intent,
    validate_intent,
)
from ttakkari.creative.v2.matching import match_intent
from ttakkari.creative.v2.probe import PROBE_IDS
from ttakkari.creative.v2.registry import Registry

BRIEF = "10초짜리 사이버펑크 SF 시네마틱 영상. 귀여운 로우폴리 3D 캐릭터가 나와요."


def S(entry, role="secondary", strength="soft", quote=None, source=None):
    return Selection(
        entry_id=entry,
        role=role,
        strength=strength,
        confidence=0.8,
        source=source or ("user_explicit" if quote else "llm_inferred"),
        evidence=[Span(quote=quote)] if quote else [],
    )


def sf_intent(**kw):
    return IntentSpec(
        selections=[
            S("C01.story_film", "primary"),
            S("C02.cyberpunk", strength="hard", quote="사이버펑크"),
            S("C02.space_sf", quote="SF"),
            S("C03.cinematic_realism", "primary", quote="시네마틱"),
            S("C03.lowpoly_3d", strength="hard", quote="로우폴리 3D"),
        ],
        duration=DurationRequest(mode="preferred", seconds=10),
        **kw,
    )


def reg_all(**overrides):
    probes = {
        pid: {"installed": True, "version": None, "detail": ""} for pid in PROBE_IDS
    }
    for k, v in overrides.items():
        probes[k] = {"installed": v, "version": None, "detail": ""}
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


# --- 의도 검증 ---
def test_hallucinated_evidence_is_rejected():
    intent = IntentSpec(selections=[S("C02.joseon", strength="hard", quote="조선시대")])
    result = validate_intent(BRIEF, intent)
    assert not result.valid and result.errors[0].code == "evidence_not_in_brief"


def test_hard_selection_requires_quote_and_unknown_entry_is_rejected():
    intent = IntentSpec(selections=[S("C02.cyberpunk", strength="hard"), S("C99.nope")])
    codes = {e.code for e in validate_intent(BRIEF, intent).errors}
    assert {"hard_without_evidence", "unknown_entry"} <= codes


def test_multiple_primary_in_single_primary_catalog_and_duplicates_rejected():
    intent = IntentSpec(
        selections=[
            S("C03.pixel_art", "primary"),
            S("C03.photoreal", "primary"),
            S("C03.pixel_art"),
        ]
    )
    codes = {e.code for e in validate_intent(BRIEF, intent).errors}
    assert {"multiple_primary", "duplicate_selection"} <= codes


def test_lexical_hint_cannot_decide_and_is_flagged_for_confirmation():
    spec, hint = hint_intent("20초 사이버펑크 영상. 45초로 늘릴 수도 있어요.")
    assert spec.extraction.method == "lexical_hint"
    assert spec.selections and all(
        s.strength == "soft" and s.role == "secondary" and s.confidence <= 0.5
        for s in spec.selections
    )
    assert hint and hint["applied"] is False
    result = validate_intent("사이버펑크", spec)
    assert result.needs_confirmation
    forged = IntentSpec(
        selections=[S("C02.cyberpunk", "primary", source="lexical_hint")],
        extraction=Extraction(method="lexical_hint"),
    )
    assert any(
        e.code == "hint_cannot_decide" for e in validate_intent(BRIEF, forged).errors
    )


def test_extraction_contract_exposes_schema_and_all_catalog_ids():
    contract = extraction_contract()
    assert "selections" in contract["schema"]["properties"]
    assert (
        len(contract["catalogs"]) == 12
        and "C03.pixel_art" in contract["catalogs"]["C03"]["entries"]
    )


# --- 매칭 ---
def test_scifi_cinematic_selects_recipe_by_structure_not_keywords():
    result = match_intent(BRIEF, sf_intent(), reg_all())
    assert result.primary_recipe == "R02"
    assert result.status == "ready_unverified"  # 설치만 확인, 실행 기록 없음
    assert result.execution_scope == "lab_manual"
    top = result.candidates[0]
    assert top.recipe_id == "R02" and top.fit > 0 and "맞는 선택" in top.reasons[0]
    assert all(c.score <= top.score for c in result.candidates if not c.disqualified)


def test_required_capabilities_are_many_to_many_with_sources_and_global_needs():
    result = match_intent(BRIEF, sf_intent(), reg_all())
    by_options = {tuple(n.options): n for n in result.needs}
    model = by_options[("asset.model_3d",)]
    assert {s.entry_id for s in model.sources} >= {"C03.lowpoly_3d"}
    for cap in GLOBAL_REQUIRES:
        assert by_options[(cap,)].priority == "must"
    sources = {s.entry_id for n in result.needs for s in n.sources}
    assert {"C03.lowpoly_3d", "C02.cyberpunk"} <= sources


def test_llm_primary_selection_does_not_promote_required_capabilities_to_hard():
    intent = IntentSpec(
        selections=[S("C01.tech_explainer", "primary", quote="기술 설명", source="llm_inferred")],
        extraction=Extraction(method="llm", model="qwen"),
    )

    result = match_intent("기술 설명 영상", intent, reg_all())

    selected_needs = [
        need for need in result.needs
        if any(source.entry_id == "C01.tech_explainer" for source in need.sources)
    ]
    assert selected_needs
    assert all(need.priority != "must" for need in selected_needs), [
        (need.options, need.priority, [source.relation for source in need.sources])
        for need in selected_needs
    ]


def test_missing_required_capability_blocks_and_offers_alternatives():
    intent = IntentSpec(
        selections=[
            S("C01.gameplay", "primary", quote="게임플레이"),
            S("C03.pixel_art", strength="hard", quote="픽셀"),
        ]
    )
    brief = "픽셀 게임플레이 영상"
    result = match_intent(brief, intent, reg_all(godot=False))
    assert result.status == "blocked" and result.execution_scope == "none"
    missing = [g for g in result.gaps if g.priority == "must" and g.status == "missing"]
    assert missing
    assert any(
        "render.pixel_art" in g.options or "sim.game_state" in g.options
        for g in missing
    )


def test_same_intent_different_registry_changes_decision_with_reasons():
    intent = IntentSpec(
        selections=[
            S("C01.animation", "primary"),
            S("C03.lowpoly_3d", strength="hard", quote="로우폴리 3D"),
        ]
    )
    ok = match_intent(BRIEF, intent, reg_all())
    no_bpy_no_three = match_intent(BRIEF, intent, reg_all(bpy=False, three=False))
    assert ok.status != "blocked"
    assert no_bpy_no_three.status == "blocked"
    assert any("asset.model_3d" in g.options for g in no_bpy_no_three.gaps)


def test_hard_vs_soft_conflict_drops_soft_with_rationale():
    intent = IntentSpec(
        selections=[
            S("C03.pixel_art", strength="hard", quote="픽셀"),
            S("C03.photoreal"),
            S("C01.gameplay", "primary"),
        ]
    )
    result = match_intent("픽셀 게임", intent, reg_all())
    assert [d["entry_id"] for d in result.dropped] == ["C03.photoreal"]
    assert "필수" in result.dropped[0]["reason"]
    assert {s.entry_id for s in result.selections} == {"C03.pixel_art", "C01.gameplay"}


def test_both_hard_conflict_needs_user_decision_not_silent_choice():
    intent = IntentSpec(
        selections=[
            S("C03.pixel_art", strength="hard", quote="픽셀"),
            S("C03.photoreal", strength="hard", quote="실사"),
            S("C01.gameplay", "primary"),
        ]
    )
    result = match_intent("픽셀 실사 게임", intent, reg_all())
    assert result.status == "needs_user_decision"
    conflict = next(c for c in result.conflicts if c.resolution == "blocked_both_hard")
    assert {conflict.a, conflict.b} == {"C03.photoreal", "C03.pixel_art"}
    assert not result.dropped and result.execution_scope == "none"


def test_soft_conflict_is_kept_as_hybrid_and_routed_to_style_dna():
    intent = IntentSpec(
        selections=[
            S("C01.animation", "primary"),
            S("C03.photoreal"),
            S("C03.cel_shaded_3d"),
        ]
    )
    result = match_intent(BRIEF, intent, reg_all())
    assert any(c.resolution == "hybrid" for c in result.conflicts)
    assert (
        "C03.cel_shaded_3d × C03.photoreal" in result.style_resolution_needed
        or "C03.photoreal × C03.cel_shaded_3d" in result.style_resolution_needed
    )
    assert len(result.selections) == 3


def test_shorts_with_exact_length_over_platform_cap_cites_source_and_needs_user():
    brief = "쇼츠 5분 영상"
    intent = IntentSpec(
        selections=[S("C01.shorts", "primary", quote="쇼츠")],
        duration=DurationRequest(mode="exact", seconds=300),
        constraints=[
            IntentConstraint(
                id="len",
                kind="duration",
                hard=True,
                statement="정확히 5분",
                source="user_explicit",
                evidence=[Span(quote="5분")],
            )
        ],
    )
    result = match_intent(brief, intent, reg_all())
    conflict = next(c for c in result.conflicts if c.kind == "constraint")
    assert (
        "support.google.com" in conflict.rationale
        and "2026-10-22" in conflict.rationale
    )
    assert result.status == "needs_user_decision"
    soft = match_intent(
        brief,
        intent.model_copy(
            update={"duration": DurationRequest(mode="preferred", seconds=300)}
        ),
        reg_all(),
    )
    assert (
        next(c for c in soft.conflicts if c.kind == "constraint").resolution == "hybrid"
    )


def test_match_is_deterministic_and_fingerprint_tracks_registry_state():
    a = match_intent(BRIEF, sf_intent(), reg_all())
    b = match_intent(BRIEF, sf_intent(), reg_all())
    assert a.model_dump(mode="json") == b.model_dump(mode="json")
    c = match_intent(BRIEF, sf_intent(), reg_all(bpy=False))
    assert c.fingerprint != a.fingerprint


def test_invalid_intent_is_refused_before_matching():
    with pytest.raises(ValueError, match="의도 검증 실패"):
        match_intent(
            BRIEF,
            IntentSpec(selections=[S("C02.joseon", strength="hard", quote="조선시대")]),
            reg_all(),
        )


def test_no_viable_recipe_is_reported_not_forced():
    intent = IntentSpec(selections=[S("C01.vlog", "primary", quote="브이로그")])
    result = match_intent("브이로그", intent, reg_all())
    assert result.primary_recipe is None and result.status == "needs_user_decision"
    assert result.trace[-2].summary.startswith("맞는 레시피가 없다")


def test_one_capability_can_be_required_by_several_selections():
    intent = IntentSpec(
        selections=[
            S("C01.tech_explainer", "primary", quote="기술 설명"),
            S("C03.vector_motion_graphics", "primary", quote="모션그래픽"),
            S("C03.infographic"),
        ]
    )
    result = match_intent("기술 설명 모션그래픽", intent, reg_all())
    react = next(n for n in result.needs if n.options == ["render.react_motion"])
    assert {s.entry_id for s in react.sources} >= {
        "C01.tech_explainer",
        "C03.vector_motion_graphics",
    }
    assert react.priority == "should"
