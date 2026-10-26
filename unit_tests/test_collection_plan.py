"""Product inputs, rather than source-registration settings, define a mission."""
import importlib

import pytest


def planner():
    try:
        return importlib.import_module("ttakkari.collector.missions").plan
    except ModuleNotFoundError:
        pytest.fail("mission planner is not implemented")


def test_mixed_input_retains_context_and_selects_adapters():
    context = "Physical AI 설계 가설: 로봇의 월드 모델이 중요하다. 아직 검증하지 않았다."
    result = planner()(
        "Physical AI 스킬을 위한 자료를 수집해줘", context=context,
        urls=["https://github.com/robotics/example", "https://arxiv.org/pdf/2401.00001"],
    )
    assert result["context"]["text"] == context
    assert result["context"]["provenance"] == "user_provided"
    assert result["context"]["fact_check_status"] == "unverified"
    assert {s["adapter"] for s in result["sources"]} == {"github", "paper_pdf"}
    assert result["budget"]["pages"] >= 100
    assert result["stop_conditions"]


def test_topic_only_creates_discovery_work_without_inventing_sources():
    result = planner()("하네스 엔지니어링 관련 이론과 논문을 폭넓게 조사해줘")
    assert result["mode"] == "research"
    assert result["sources"] == []
    assert len(result["queries"]) > 1
    assert result["coverage"]
    assert all(part["state"] == "unexplored" for part in result["coverage"])


def test_urls_in_context_are_extracted_deduplicated_and_preserved():
    result = planner()("공식 문서 수집 https://docs.python.org/3/", context="참고: https://docs.python.org/3/\nhttps://example.org/paper.pdf")
    assert len(result["sources"]) == 2
    assert {s["adapter"] for s in result["sources"]} == {"website", "paper_pdf"}


@pytest.mark.parametrize("goal", ["", "  ", "x\x00y"])
def test_invalid_goal_fails_before_persistence(goal):
    with pytest.raises(ValueError):
        planner()(goal)
