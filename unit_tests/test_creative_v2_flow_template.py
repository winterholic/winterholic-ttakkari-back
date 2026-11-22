"""데이터 흐름 설명 요청 → R22 다이어그램 템플릿 자동 연결. 제품명이 아니라 템플릿이 선언한 개념 범위로 판정한다."""

import httpx
import pytest
from fastapi import FastAPI

from ttakkari.api import creative_v2 as api
from ttakkari.api import creative_v2_intent as intent_api
from ttakkari.creative.service import Studio
from ttakkari.creative.v2 import dispatch, flow_template
from ttakkari.creative.v2.intent import InfoItem
from ttakkari.creative.v2.intent_resolve import ResolveRequest, resolve_intent
from ttakkari.creative.v2.plans import PlanStore, build_plan
from ttakkari.creative.v2.registry import Registry
from ttakkari.creative.v2.style_dna import StyleStore
from ttakkari.security.auth import require_user

REQUEST = "Kafka Producer가 메시지를 발행하고 Broker의 Partition에 저장된 뒤 Consumer가 소비하는 과정을 설명하는 30초 16:9 교육용 모션그래픽"
GENERIC = "Producer가 메시지를 발행하고 Broker의 Partition에 저장된 뒤 Consumer가 소비하는 과정을 설명하는 30초 16:9 교육용 모션그래픽"
CONDITIONS = [
    {"id": "C01.tech_explainer", "role": "primary", "strength": "required"},
    {"id": "C03.vector_motion_graphics"},
]


@pytest.fixture
def store(tmp_path):
    return PlanStore(tmp_path / "plans", registry=Registry())


def intent_for(brief, *, seconds=30, aspect="16:9", info_items=None, selections=CONDITIONS):
    result = resolve_intent(
        ResolveRequest(
            brief=brief,
            structured={"selections": selections, "duration": {"mode": "target", "seconds": seconds}, "aspect": aspect},
            info_items=info_items,
        )
    )
    assert result.ready_for_plan, result.blocking
    return result.intent


def create(store, brief, **kw):
    # 브리프의 길이 문구는 구조화 길이와 일치시킨다(다르면 길이 충돌로 계획이 막힌다).
    brief = brief.replace("30초", f"{kw.get('seconds', 30)}초")
    return store.create(build_plan(brief, None, registry=Registry(), intent_v2=intent_for(brief, **kw)))


@pytest.mark.parametrize("brief", [REQUEST, GENERIC])
def test_data_flow_request_selects_the_runnable_template_and_opens_the_gate(store, brief):
    plan = create(store, brief)
    assert plan["match"]["primary_recipe"] == "R22"
    assert plan["match"]["execution_scope"] == "studio_runtime"
    assert plan["director"]["source"] == "recipe_template"
    assert [s["strategy"] for s in plan["scenes"]] == ["code_motion"] * len(plan["scenes"])
    assert 3 <= len(plan["scenes"]) <= 5
    assert plan["duration"]["status"] == "target_met"
    assert plan["template_scope"]["status"] == "applied"
    assert set(plan["template_scope"]["matched_concepts"]) >= {"producer", "broker", "consumer", "partition"}
    assert plan["render_blockers"] == []
    approved = store.approve(plan["id"], plan["revision"])
    assert approved["render_gate"], approved["render_gate_reasons"]
    assert not dispatch.dispatch_blockers(approved, runtime_preflight=False)
    # 자동 구성한 장면이 계획에 그대로 보이고 사용자가 승인 전에 확인할 수 있다.
    assert plan["scenes"][0]["title"] == "발행 · Publish"
    assert abs(plan["scenes"][-1]["end_time"] - 30) < 0.5


def test_scope_is_decided_by_concepts_not_by_the_product_name():
    assert not hasattr(flow_template, "KAFKA")
    brief = GENERIC.casefold()
    assert "kafka" not in brief
    items = []
    assert flow_template.in_scope(flow_template.MESSAGE_FLOW, brief, items)
    # 주체 개념이 둘뿐이면 범위 밖이다. 발행자와 소비자가 모두 있어도 세 개 미만이다.
    assert not flow_template.in_scope(flow_template.MESSAGE_FLOW, "producer와 consumer만 나옵니다", items)
    # 제품 이름만 직접 말해도 범위 안이다(템플릿이 선언한 주제어).
    assert flow_template.in_scope(flow_template.MESSAGE_FLOW, "Kafka를 설명한다", items)


def test_out_of_scope_data_flow_keeps_the_old_matching_and_records_why(store):
    brief = "Redis 캐시 적중과 미적중 흐름을 30초 16:9 모션그래픽으로 초보자에게 설명한다"
    plan = create(store, brief)
    scope = plan["template_scope"]
    assert scope["status"] == "out_of_scope"
    assert "producer" in scope["missing_concepts"] and "consumer" in scope["missing_concepts"]
    assert "다른 레시피" in scope["reason"]
    assert scope["supported"]["aspects"] == ["16:9"]
    assert plan["match"]["primary_recipe"] != "R22"
    assert "director" not in plan
    assert any("out_of_scope" not in n and scope["reason"] in n for n in plan["notes"])


def test_vertical_or_wrong_scene_count_is_reported_instead_of_forcing_the_template(store):
    vertical = create(store, GENERIC.replace("16:9", "9:16"), aspect="9:16")
    assert vertical["template_scope"]["status"] == "out_of_scope"
    assert "9:16" in vertical["template_scope"]["reason"]
    assert vertical["match"]["primary_recipe"] != "R22"
    two = [InfoItem(id=f"s{i}", title=f"단계 {i}", narration="설명") for i in range(2)]
    short = create(store, GENERIC, info_items=[i.model_dump() for i in two])
    assert short["template_scope"]["status"] == "out_of_scope"
    assert "2개" in short["template_scope"]["reason"]


def test_user_info_items_become_the_template_scenes(store):
    titles = ["메시지 발행", "파티션 저장", "컨슈머 소비"]
    items = [{"id": f"u{i}", "title": t, "narration": f"{t} 단계 설명입니다."} for i, t in enumerate(titles)]
    plan = create(store, GENERIC, info_items=items, seconds=24)
    assert plan["template_scope"]["scene_source"] == "request_items"
    assert [s["title"] for s in plan["scenes"]] == titles
    assert plan["match"]["primary_recipe"] == "R22"


def test_length_decides_scene_count_with_the_existing_duration_engine(store):
    brief = "Producer, Broker, Consumer의 메시지 흐름을 설명하는 30초 16:9 기술 설명 영상"
    plan = create(store, brief, selections=[{"id": "C01.tech_explainer", "role": "primary"}])
    # 개념이 세 개뿐이라 3장면이 기본이지만 30초는 3장면에 담기지 않아(장면당 상한) 템플릿 막 안에서 늘린다.
    assert len(plan["scenes"]) >= 4
    assert plan["duration"]["status"] == "target_met"
    short = create(store, brief, seconds=16, selections=[{"id": "C01.tech_explainer", "role": "primary"}])
    assert len(short["scenes"]) == 3


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


async def test_http_plan_create_from_the_natural_language_request_selects_r22(application):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=application), base_url="http://test") as client:
        resolved = await client.post("/api/creative/v2/intent/resolve", json={
            "brief": REQUEST,
            "structured": {"selections": CONDITIONS, "duration": {"mode": "target", "seconds": 30}, "aspect": "16:9"},
        })
        assert resolved.status_code == 200, resolved.text
        created = await client.post("/api/creative/v2/plans", json={"brief": REQUEST, "intent_v2": resolved.json()["intent"]})
        assert created.status_code == 201, created.text
        plan = created.json()
        assert plan["match"]["primary_recipe"] == "R22" and plan["template_scope"]["status"] == "applied"
        approved = await client.post(f"/api/creative/v2/plans/{plan['id']}/approve", json={"revision": plan["revision"]})
        assert approved.status_code == 200 and approved.json()["render_gate"] is True
