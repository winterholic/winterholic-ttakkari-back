"""IntentSpec v2·동적 노출 규칙·resolve API. 계약: docs/design/creative-studio-v2-intent-v2.md"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError

from ttakkari.api import creative_v2_intent as api
from ttakkari.creative.v2.facets import RULES, resolve_layout
from ttakkari.creative.v2.intent import IntentSpec
from ttakkari.creative.v2.intent_resolve import ResolveRequest, resolve_intent
from ttakkari.creative.v2.intent_v2 import (
    Condition,
    Confirmation,
    load_intent,
    promote_v1,
)
from ttakkari.security.auth import require_user

A = "일본 애니메이션 스타일의 SF 단편을 만들고 싶다. 공포 분위기와 액션 장면이 있고, 약 2분 분량으로 구성해 줘."
B = "Kafka의 Producer, Broker, Consumer 간 메시지 흐름을 초보자가 이해할 수 있도록 16초 분량으로 설명해 줘."
C = "감성적인 음악에 어울리는 3분짜리 뮤직비디오를 만들어 줘. 영상의 움직임과 장면 전환이 음악의 흐름에 어울렸으면 좋겠어."


def run(brief="", **kw):
    return resolve_intent(ResolveRequest(brief=brief, **kw))


def groups(r):
    return {g.id for g in r.layout.groups}


def questions(r):
    return {q.field_id: q for q in r.layout.questions}


# --- 완료 기준 4: 시나리오 A/B/C ---------------------------------------------------


def test_scenarios_produce_different_groups_and_questions():
    a, b, c = run(A), run(B), run(C)
    ga, gb, gc = groups(a), groups(b), groups(c)
    assert {
        "character",
        "drawing",
        "cut_direction",
        "world",
        "space_camera",
        "lighting_vfx",
        "horror",
        "action",
    } <= ga
    assert {"explain", "diagram", "motion_graphics", "narration"} <= gb
    assert {"music", "lyrics", "av_sync"} <= gc
    # 공통 묶음(output)을 빼면 시나리오끼리 핵심 묶음이 겹치지 않는다.
    assert not (gb - {"output"}) & (ga | gc)
    assert not {"music", "lyrics", "av_sync"} & ga
    qa, qb, qc = set(questions(a)), set(questions(b)), set(questions(c))
    assert qa != qb != qc != qa
    assert {
        "horror.style",
        "action.kind",
        "world.tech_design",
        "cut.impact_frames",
    } <= qa
    assert {"diagram.type", "diagram.motion", "explain.audience"} <= qb
    assert {"sync.cut_on", "sync.motion_source", "music.source"} <= qc
    # B는 생성형 배경 대신 다이어그램 흐름을, C는 정지 이미지 대신 실제 움직임을 권한다.
    assert questions(b)["diagram.type"].recommended == "flow"
    assert questions(c)["sync.motion_source"].recommended == "generated_video"
    # 설명 콘텐츠에 필요 없는 캐릭터 연기 설정은 숨김으로 내려가고 이유가 남는다.
    hidden_b = {h.field_id: h for h in b.layout.hidden}
    assert "character.lipsync" in hidden_b and hidden_b["character.lipsync"].reason


@pytest.mark.parametrize(
    ("brief", "content_type", "seconds", "recipe"),
    [
        (A, "C01.animation", 120, "R04"),
        # 메시지 흐름 개념(Producer·Broker·Consumer)을 다루는 기술 설명은 템플릿 범위 안이라 R22로 자동 연결된다.
        (B, "C01.tech_explainer", 16, "R22"),
        (C, "C01.music_video", 180, "R19"),
    ],
)
def test_confirmed_v2_intent_survives_plan_storage(tmp_path, brief, content_type, seconds, recipe):
    from ttakkari.creative.v2.plans import PlanStore, build_plan

    resolved = run(brief, confirm=[content_type])
    store = PlanStore(tmp_path / "plans")
    created = store.create(build_plan(brief, None, intent_v2=resolved.intent))
    plan = store.get(created["id"])
    assert plan["intent_v2"] == resolved.intent.model_dump(mode="json")
    assert plan["match"]["primary_recipe"] == recipe
    assert plan["duration"]["request"]["seconds"] == seconds
    origin = next(item for item in plan["intent_projection"]["items"] if item["id"] == content_type)
    assert origin["origin"] == "user_confirmed"
    assert not plan["render_gate"]


def test_plan_duration_edit_creates_new_intent_revision(tmp_path):
    from ttakkari.creative.v2.duration import DurationRequest
    from ttakkari.creative.v2.plans import PlanStore, build_plan

    resolved = run(B, confirm=["C01.tech_explainer"])
    store = PlanStore(tmp_path / "plans")
    created = store.create(build_plan(B, None, intent_v2=resolved.intent))
    edited = store.edit(
        created["id"], created["revision"],
        duration=DurationRequest(mode="preferred", seconds=18),
    )
    assert edited["intent_v2"]["revision"] == resolved.intent.revision + 1
    assert edited["intent_v2"]["duration"]["seconds"] == 18
    assert edited["intent_projection"]["source"]["revision"] == edited["intent_v2"]["revision"]
    assert edited["duration"]["request"]["seconds"] == 18


def test_scenario_a_keeps_all_four_conditions_without_duplicate_questions():
    a = run(A)
    ids = {c.id for c in a.intent.conditions}
    assert {"C03.japanese_anime", "C02.space_sf", "tone.horror", "tone.action"} <= ids
    # 조건 넷 모두 어떤 묶음을 실제로 켰다(무시된 조건 없음).
    triggered = {t for g in a.layout.groups for t in g.triggered_by}
    assert {
        "C03.japanese_anime",
        "C02.space_sf",
        "tone.horror",
        "tone.action",
    } <= triggered
    fields = [q.field_id for q in a.layout.questions]
    assert len(fields) == len(set(fields))
    # 공포(느리게)와 액션(빠르게)이 같은 템포 설정을 묻지만 질문은 하나이고 조합 규칙이 추천을 정한다.
    tempo = questions(a)["cut.tempo"]
    assert tempo.recommended == "slow_build_burst"
    assert set(tempo.rules) >= {"horror_tone", "action_tone"}
    assert {x.value for x in tempo.alternatives} == {"slow", "fast"}
    # 일본 애니와 애니메이션 유형이 같은 캐릭터 묶음을 요구해도 묶음은 하나다.
    character = [g for g in a.layout.groups if g.id == "character"]
    assert len(character) == 1
    assert {"drawn_style", "animation_type"} <= set(character[0].rules)
    assert questions(a)["cut.impact_frames"].recommended == "yes"


def test_merge_rule_ask_beats_hide_and_combination_only_rules():
    # 설명 규칙은 sync.cut_on을 숨기지만 뮤직비디오 규칙이 묻는다. 질문이 이긴다.
    both = resolve_layout(
        [
            Condition(id="C01.tech_explainer", dimension="C01", origin="ai_inferred"),
            Condition(id="C01.music_video", dimension="C01", origin="ai_inferred"),
        ]
    )
    assert "sync.cut_on" in {q.field_id for q in both.questions}
    assert "sync.cut_on" not in {h.field_id for h in both.hidden}
    # 조합 규칙은 한쪽만 있을 때 적용되지 않는다.
    horror_only = resolve_layout(
        [Condition(id="tone.horror", dimension="tone", origin="ai_inferred")]
    )
    assert "horror_action_combo" not in horror_only.applied_rules
    assert {q.field_id: q for q in horror_only.questions}[
        "cut.tempo"
    ].recommended == "slow"
    assert all(r.id for r in RULES)


def test_answers_and_confirmed_conditions_remove_questions():
    first = run(A)
    second = run(
        A,
        intent=first.intent.model_dump(),
        answers=[{"field_id": "action.kind", "values": ["C07.battle", "C07.run"]}],
    )
    assert "action.kind" not in questions(second)
    assert {"C07.battle", "C07.run"} <= {
        c.id for c in second.intent.conditions if c.origin == "user_confirmed"
    }
    assert second.intent.revision == 2
    assert [h.action for h in second.intent.history].count("answered") == 1
    with pytest.raises(ValueError):
        run(A, answers=[{"field_id": "action.kind", "values": ["C05.night"]}])


# --- 완료 기준 5: 충돌과 출처 ----------------------------------------------------


def test_free_text_duration_vs_structured_duration_is_conflict_not_silent():
    r = run(C, structured={"duration": {"mode": "target", "seconds": 30}})
    conflict = next(c for c in r.intent.conflicts if c.id == "conflict.duration")
    assert conflict.status == "open" and conflict.blocking
    origins = {x.value: x.origin for x in conflict.candidates}
    assert origins == {"target:30": "user_confirmed", "target:180": "user_free_text"}
    assert r.intent.duration is None  # 해결 전에는 어느 쪽도 유효값이 아니다.
    assert not r.ready_for_plan and "conflict.duration" in r.blocking
    assert r.length.status == "blocked_by_conflict"
    assert "conflict.duration" in {c.id for c in r.layout.conflicts}

    resolved = run(
        C,
        intent=r.intent.model_dump(),
        structured={"duration": {"mode": "target", "seconds": 30}},
        resolutions=[{"conflict_id": "conflict.duration", "choice": "target:30"}],
    )
    assert (
        resolved.intent.duration.seconds == 30
        and resolved.intent.duration.origin == "user_confirmed"
    )
    assert (
        next(c for c in resolved.intent.conflicts if c.id == "conflict.duration").status
        == "resolved"
    )
    assert resolved.blocking == ["unconfirmed.content_type"]
    resolved = run(C, intent=resolved.intent.model_dump(), confirm=["C01.music_video"])
    assert resolved.ready_for_plan and resolved.intent.duration.seconds == 30
    # 다음 요청에서도 해결 기록이 유지된다.
    again = run(
        C,
        intent=resolved.intent.model_dump(),
        structured={"duration": {"mode": "target", "seconds": 30}},
    )
    assert again.intent.duration.seconds == 30 and again.ready_for_plan


def test_close_target_durations_do_not_conflict_and_strict_does():
    near = run(A, structured={"duration": {"mode": "target", "seconds": 125}})
    assert not near.intent.conflicts and near.intent.duration.seconds == 125
    strict = run(A, structured={"duration": {"mode": "strict", "seconds": 125}})
    assert strict.intent.duration is None


def test_ai_inferred_is_never_user_confirmed():
    b = run(B)
    explainer = b.intent.condition("C01.tech_explainer")
    assert (
        explainer is not None
        and explainer.origin == "ai_inferred"
        and explainer.confirmation is None
    )
    for r in (run(A), b, run(C)):
        assert all(c.origin != "user_confirmed" for c in r.intent.conditions)
    with pytest.raises(ValidationError):
        Condition(
            id="C01.tech_explainer",
            dimension="C01",
            origin="ai_inferred",
            strength="required",
        )
    with pytest.raises(ValidationError):
        Condition(
            id="C01.tech_explainer",
            dimension="C01",
            origin="ai_inferred",
            confirmation=Confirmation(at="2026-11-21T00:00:00+00:00", revision=1),
        )
    confirmed = run(B, intent=b.intent.model_dump(), confirm=["C01.tech_explainer"])
    c = confirmed.intent.condition("C01.tech_explainer")
    assert c.origin == "user_confirmed" and c.confirmation.revision == 2
    # 확인 질문은 추론값을 추천으로 보여 주되 확정하지 않는다.
    q = questions(run(A, structured={"selections": []}))["output.duration"]
    assert q.mode == "confirm" and q.recommended == "target"


def test_catalog_conflict_between_free_text_and_structured_choice():
    r = run(A, structured={"selections": [{"id": "C03.photoreal", "role": "primary"}]})
    conflict = next(c for c in r.intent.conflicts if c.kind == "catalog_conflict")
    assert {x.value for x in conflict.candidates} == {
        "C03.japanese_anime",
        "C03.photoreal",
    }
    assert conflict.status == "open" and conflict.blocking
    assert r.intent.condition("C03.japanese_anime") is not None  # 버리지 않았다
    fixed = run(
        A,
        intent=r.intent.model_dump(),
        resolutions=[{"conflict_id": conflict.id, "choice": "C03.japanese_anime"}],
    )
    assert fixed.intent.condition("C03.photoreal") is None
    assert "C03.photoreal" in fixed.intent.rejected
    assert fixed.intent.condition("C03.japanese_anime").origin == "user_confirmed"


def test_reject_survives_rereading_brief_and_unsupported_is_recorded():
    r = run(A, reject=["tone.horror"])
    assert r.intent.condition("tone.horror") is None and "horror" not in groups(r)
    again = run(A, intent=r.intent.model_dump())
    assert again.intent.condition("tone.horror") is None
    u = run(
        A,
        structured={"selections": [{"id": "tone.noir"}, {"id": "C99.unknown"}]},
        references=[
            {"id": "ref1", "kind": "video", "role": "motion"},
            {"id": "ref2", "kind": "image", "role": "style"},
        ],
    )
    reasons = {x.subject: x.reason for x in u.intent.unsupported}
    assert (
        reasons["tone.noir"] == "unknown_tone"
        and reasons["C99.unknown"] == "not_in_catalog"
    )
    assert any(x.reason == "reference_not_ingested" for x in u.intent.unsupported)
    usable = {x.id: x.usable for x in u.intent.references}
    assert usable == {"ref1": False, "ref2": True}


# --- v1 읽기 호환 ----------------------------------------------------------------


def test_v1_promotion_keeps_meaning_and_records_history():
    v1 = IntentSpec.model_validate(
        {
            "selections": [
                {
                    "entry_id": "C01.tech_explainer",
                    "role": "primary",
                    "strength": "hard",
                    "source": "user_explicit",
                    "evidence": [{"quote": "기술 설명"}],
                },
                {"entry_id": "C08.explainer", "source": "llm_inferred"},
            ],
            "duration": {"mode": "preferred", "seconds": 45},
            "open_questions": ["대상은 누구인가요?"],
        }
    )
    v2 = promote_v1(v1, "기술 설명 영상")
    assert v2.schema_version == 2 and v2.migrated_from == 1
    assert v2.condition("C01.tech_explainer").origin == "user_confirmed"
    assert v2.condition("C01.tech_explainer").strength == "required"
    assert v2.condition("C08.explainer").origin == "ai_inferred"
    assert v2.duration.mode == "target" and v2.duration.seconds == 45
    assert v2.duration.to_request().mode == "preferred"
    assert {h.action for h in v2.history} >= {
        "migrated_from_v1",
        "legacy_open_question",
    }

    llm = IntentSpec.model_validate(
        {
            "selections": [
                {
                    "entry_id": "C03.japanese_anime",
                    "strength": "hard",
                    "source": "llm_inferred",
                    "evidence": [{"quote": "애니"}],
                }
            ],
            "extraction": {"method": "llm"},
        }
    )
    promoted = promote_v1(llm, "애니")
    assert promoted.condition("C03.japanese_anime").strength == "preferred"
    assert "strength_downgraded" in {h.action for h in promoted.history}
    # schema_version 없는 저장 문서는 v1으로 읽는다.
    assert load_intent({"selections": []}).migrated_from == 1
    assert load_intent(promoted.model_dump()).schema_version == 2
    with pytest.raises(ValueError):
        load_intent({"schema_version": 3})
    # resolve는 v1 문서를 그대로 받아 승격한다.
    r = run("기술 설명 영상", intent=v1.model_dump())
    assert r.intent.migrated_from == 1 and r.intent.revision == 2


def test_length_assessment_uses_existing_duration_calculator():
    v1 = IntentSpec.model_validate(
        {
            "info_items": [
                {
                    "id": f"i{i}",
                    "title": f"단계 {i}",
                    "narration": "프로듀서가 메시지를 브로커의 토픽 파티션에 기록하고 컨슈머가 오프셋을 따라 읽습니다.",
                }
                for i in range(6)
            ],
            "duration": {"mode": "exact", "seconds": 16},
        }
    )
    r = run(intent=v1.model_dump(), read_free_text=False)
    assert r.length.status == "conflict" and r.length.alternatives
    assert r.length.needed_seconds["min"] > 16


# --- API --------------------------------------------------------------------------


def _client(authed=True):
    app = FastAPI()
    app.include_router(api.router)
    if authed:
        app.dependency_overrides[require_user] = lambda: "owner"
    return TestClient(app)


def test_resolve_api_round_trip_and_auth():
    assert (
        _client(False)
        .post("/api/creative/v2/intent/resolve", json={"brief": A})
        .status_code
        == 401
    )
    c = _client()
    r = c.post(
        "/api/creative/v2/intent/resolve",
        json={
            "brief": C,
            "structured": {"duration": {"mode": "target", "seconds": 30}},
        },
    )
    assert r.status_code == 200
    body = r.json()
    assert body["intent"]["schema_version"] == 2 and body["ready_for_plan"] is False
    assert any(x["id"] == "conflict.duration" for x in body["layout"]["conflicts"])
    bad = c.post(
        "/api/creative/v2/intent/resolve",
        json={
            "brief": C,
            "resolutions": [{"conflict_id": "conflict.nope", "choice": "x"}],
        },
    )
    assert bad.status_code == 422
    facets = c.get("/api/creative/v2/intent/facets").json()
    assert facets["rules_version"] and {g["id"] for g in facets["groups"]} >= {
        "horror",
        "diagram",
        "av_sync",
    }
