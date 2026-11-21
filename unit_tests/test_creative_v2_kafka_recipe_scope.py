"""Narration is an audio choice, not evidence for a documentary recipe."""

import pytest

from ttakkari.creative.v2 import dispatch
from ttakkari.creative.v2.dispatch import dispatch_blockers
from ttakkari.creative.v2.duration import DurationRequest
from ttakkari.creative.v2.intent import InfoItem, IntentSpec, Selection
from ttakkari.creative.v2.planner import ProductionDirection
from ttakkari.creative.v2.plans import PlanStore, build_plan
from ttakkari.creative.v2.registry import Registry

BRIEF = (
    "Kafka 메시지 흐름을 16초, 16:9 기술 설명 영상으로 만든다. "
    "프로듀서가 토픽 파티션에 기록하고 컨슈머가 오프셋을 따라 읽는다. "
    "기술 용어를 사실에 맞게 쓰고 내레이션 자막으로 구성한다."
)


@pytest.mark.parametrize("structured", [False, True])
def test_technical_narration_cannot_select_documentary_or_inject_map_data(structured):
    intent = (
        IntentSpec(
            selections=[
                Selection(entry_id=c)
                for c in ["C01.tech_explainer", "C09.narration", "C10.subtitles"]
            ]
        )
        if structured
        else None
    )
    plan = build_plan(
        BRIEF, intent, duration=DurationRequest(mode="preferred", seconds=16)
    )
    assert plan["match"]["primary_recipe"] not in {"R16", "R21"}
    documentary = next(
        c for c in plan["match"]["candidates"] if c["recipe_id"] == "R16"
    )
    assert documentary["fit"] == 0
    assert not any("research.map_data" in n["options"] for n in plan["match"]["needs"])
    if not structured:
        assert plan["intent_status"] == "unconfirmed_hint"
        assert plan["validation"]["needs_confirmation"]


@pytest.mark.parametrize("entry", ["C01.documentary", "C08.documentary_form"])
def test_explicit_documentary_keeps_required_research_and_closed_gate(tmp_path, entry):
    intent = IntentSpec(
        selections=[Selection(entry_id=entry), Selection(entry_id="C09.narration")]
    )
    plans = PlanStore(tmp_path / "plans")
    plan = plans.create(build_plan("출처와 지도 자료를 사용하는 다큐멘터리", intent))
    assert plan["match"]["primary_recipe"] == "R16"
    needs = {tuple(n["options"]) for n in plan["match"]["needs"]}
    assert {("research.fact_check",), ("research.map_data",)} <= needs
    assert ["research.fact_check"] in Registry().recipes["R16"].requires
    assert ["research.map_data"] in Registry().recipes["R16"].requires
    assert not plan["render_gate"]
    assert plan["approval"] is None


def test_reviewed_kafka_direction_opens_distinct_route(tmp_path):
    info = [
        InfoItem(id=slug, title=title)
        for slug, title in (
            ("send", "Producer가 메시지 전송"),
            ("store", "Broker가 토픽에 메시지 저장"),
            ("read", "Consumer가 오프셋부터 읽기"),
        )
    ]
    intent = IntentSpec(
        selections=[Selection(entry_id="C01.tech_explainer")],
        duration=DurationRequest(mode="exact", seconds=16),
        info_items=info,
        aspect="16:9",
    )
    direction = ProductionDirection.model_validate({
        "source": "user_structured", "reviewed": True, "intent": "Kafka 메시지 흐름",
        "scenes": [
            {"scene_id": item.id, "strategy": "code_motion", "preset": "kafka_flow",
             "rationale": "데이터 흐름을 움직이는 도형으로 전달",
             "shots": [{"shot_id": item.id, "action": item.title}]}
            for item in info
        ],
    })
    plans = PlanStore(tmp_path / "plans")
    preview = plans.create(build_plan(BRIEF, intent))
    assert preview["match"]["primary_recipe"] != "R22"
    directed = plans.edit(preview["id"], 1, director=direction)
    assert directed["match"]["primary_recipe"] == "R22"
    assert not directed["render_gate"]
    approved = plans.approve(directed["id"], directed["revision"])
    assert approved["render_gate"]
    assert approved["render_plan"]["segments"][0]["seconds"] == 16
    assert not dispatch_blockers(approved, runtime_preflight=False)
    store = dispatch.ProductionStore(plans)
    state = dispatch._assets_state(approved, store.read(approved["id"]), store)
    assert state["complete"] and state["music"] is None
    revised = plans.edit(approved["id"], approved["revision"], duration=DurationRequest(mode="exact", seconds=17))
    assert not revised["render_gate"]
    assert plans.approve(revised["id"], revised["revision"])["render_gate"]
    unrelated = build_plan("Redis Pub/Sub 흐름을 설명한다.", intent, director=direction)
    assert unrelated["match"]["primary_recipe"] != "R22"


def test_musicless_kafka_dispatch_computes_run_key_and_reports_outdated_segments(tmp_path, monkeypatch):
    """Kafka R22 has no music asset; dispatch must not assume one (seen in the real render run)."""
    from ttakkari.creative.service import Studio
    from ttakkari.creative.v2 import engines

    info = [InfoItem(id=slug, title=slug.title()) for slug in ("producer", "broker", "consumer")]
    intent = IntentSpec(
        selections=[Selection(entry_id="C01.tech_explainer")],
        duration=DurationRequest(mode="exact", seconds=16), info_items=info, aspect="16:9",
    )
    direction = ProductionDirection.model_validate({
        "source": "user_structured", "reviewed": True, "intent": "Kafka 메시지 흐름",
        "scenes": [
            {"scene_id": item.id, "strategy": "code_motion", "preset": "kafka_flow",
             "rationale": "흐름 도식", "shots": [{"shot_id": item.id, "action": item.title}]}
            for item in info
        ],
    })
    ready = engines.Preflight(True, "test", [])
    monkeypatch.setattr(engines.ModuleAdapter, "preflight", lambda *_: ready)
    plans = PlanStore(tmp_path / "plans")
    plan = plans.create(build_plan(BRIEF, intent))
    plan = plans.edit(plan["id"], 1, director=direction)
    plan = plans.approve(plan["id"], plan["revision"])
    view = dispatch.dispatch(plans, Studio(tmp_path / "studio"), plan["id"], plan["revision"])
    assert view["state"] == "ready_to_render"
    assert view["engine"]["adapter"] == "skill.hyperframes"
    assert view["outdated_segments"] == ["seg-01"]


def test_reviewed_kafka_dispatches_and_queues_with_core_shot_requirements(
    tmp_path, monkeypatch,
):
    from ttakkari import queued_work
    from ttakkari.creative.service import Studio
    from ttakkari.creative.v2 import engines, render_worker

    info = [InfoItem(id=slug, title=slug.title()) for slug in ("producer", "broker", "consumer")]
    intent = IntentSpec(
        selections=[Selection(
            entry_id="C01.tech_explainer", role="primary", strength="hard",
            source="user_explicit", confirmed=True,
        )],
        duration=DurationRequest(mode="exact", seconds=16), info_items=info, aspect="16:9",
    )
    direction = ProductionDirection.model_validate({
        "source": "user_structured", "reviewed": True, "intent": "Kafka 메시지 흐름",
        "scenes": [
            {"scene_id": item.id, "strategy": "code_motion", "preset": "kafka_flow",
             "rationale": "흐름 도식", "shots": [{"shot_id": item.id, "action": item.title,
                "required_capabilities": ["edit.timeline", "edit.captions", "audio.mix", "qa.machine_video"]}]}
            for item in info
        ],
    })
    candidate = build_plan(BRIEF, intent, director=direction)
    timeline = next(need for need in candidate["match"]["needs"] if need["options"] == ["edit.timeline"])
    assert timeline["best"]["entry_id"] == "skill.v1_ffmpeg_slideshow"
    plans = PlanStore(tmp_path / "plans")
    saved = plans.create(candidate)
    plan = plans.approve(saved["id"], saved["revision"])
    assert plan["match"]["primary_recipe"] == "R22"
    monkeypatch.setattr(queued_work, "delegated", lambda: True)
    monkeypatch.setattr(engines.ModuleAdapter, "preflight", lambda *_: pytest.fail("API ran host preflight"))
    view = dispatch.dispatch(plans, Studio(tmp_path / "studio"), plan["id"], plan["revision"])
    assert view["state"] == "ready_to_render"
    assert view["engine"]["adapter"] == "skill.hyperframes"
    assert view["engine"]["producers"] == ["skill.hyperframes_motion"]
    request = render_worker.enqueue(plans, plan["id"], plan["revision"])
    assert request["status"] == "queued"
    assert render_worker.enqueue(plans, plan["id"], plan["revision"])["id"] == request["id"]
