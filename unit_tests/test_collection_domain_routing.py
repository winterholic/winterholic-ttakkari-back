"""Real owner directives must keep their subject instead of becoming UI research."""
from pathlib import Path

import pytest

from ttakkari.collector import catalog, missions

FIXTURES = Path(__file__).parents[1] / "tests" / "fixtures"


@pytest.mark.parametrize("fixture,subject", [
    ("collect_robotics_goal.md", "로보틱스"),
    ("collect_harness_goal.md", "AI Harness Engineering"),
])
def test_real_non_ui_directives_keep_their_subject(fixture, subject):
    goal = (FIXTURES / fixture).read_text()
    result = missions.plan(goal)
    assert "targets" not in result
    assert not result["sources"]
    queries = result["queries"] + result["deferred_queries"]
    assert all("design system guidelines" not in q for q in queries)
    assert all("디자인 시스템" not in q for q in queries)
    assert all(subject in q or "physical ai" in q.lower() or "harness engineering" in q.lower() for q in queries)
    assert all("프로젝트 목적" not in q and "백과사전" not in q for q in queries)
    assert all(t["subject"] for t in result["topics"])


@pytest.mark.parametrize("goal", [
    "Robotics motion planning, input representation and feedback control 연구",
    "Agent hierarchy adaptive policy tokens and information architecture 조사",
    "Transformer performance Hyperparameter Optimization 학습 방법 조사",
    "Bootstrap statistics and adaptive optimization 조사",
    "네이버 토스 금융 사업 조사",
    "로봇 디자인의 motion planning input representation feedback control 조사",
])
def test_overlapping_technical_words_do_not_select_a_ui_catalog(goal):
    result = missions.plan(goal)
    assert "targets" not in result
    assert all(t["subject"] for t in result["topics"])


def test_topic_words_do_not_match_inside_other_words():
    keys = {t["key"] for t in catalog.topics_in("Transformer performance Hyperparameter Optimization")}
    assert "components" not in keys and "enterprise" not in keys


@pytest.mark.parametrize("goal", [
    "디자인시스템의 타이포그래피, 간격, 컴포넌트 조사",
    "UI/UX typography spacing components research",
    "IBM Carbon과 Apple HIG 비교",
])
def test_explicit_ui_requests_still_use_the_catalog(goal):
    result = missions.plan(goal)
    assert "targets" in result
    assert any(not t["subject"] for t in result["topics"])


def test_real_design_directive_keeps_named_roots_and_topics():
    result = missions.plan((FIXTURES / "collect_design_goal.md").read_text())
    assert {"carbon", "hig", "material"} <= {t["key"] for t in result["targets"]}
    assert "typography" in {t["key"] for t in result["topics"]}
