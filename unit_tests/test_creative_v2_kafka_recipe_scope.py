"""Narration is an audio choice, not evidence for a documentary recipe."""

import pytest

from ttakkari.creative.v2.duration import DurationRequest
from ttakkari.creative.v2.intent import IntentSpec, Selection
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
