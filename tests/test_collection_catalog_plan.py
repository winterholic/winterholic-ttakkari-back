"""A goal that names its targets must be planned from those names, and the trace must report what it stores."""
from pathlib import Path

import pytest
from test_collector_control import BASE

from ttakkari.collector import catalog, missions

pytestmark = pytest.mark.security
GOAL = (Path(__file__).parent / "fixtures" / "collect_design_goal.md").read_text()


def test_named_targets_become_verified_roots_and_clean_queries():
    plan = missions.plan(GOAL, max_pages=1000, max_depth=3)
    keys = {t["key"] for t in plan["targets"]}
    assert {"carbon", "material", "hig", "fluent", "govuk", "wcag", "nngroup", "pajamas"} <= keys
    urls = {s["url"] for s in plan["sources"]}
    assert "https://www.carbondesignsystem.com/" in urls and "https://m3.material.io/" in urls
    assert all(s["provenance"] == "catalog" for s in plan["sources"])
    joined = " ".join(plan["queries"] + plan.get("deferred_queries", []))
    # The goal sentence is never pasted into a search query: platform words and request wording stay out.
    assert "따까리" not in joined and "백과사전" not in joined and "해줘" not in joined
    assert any("typography" in q for q in plan["queries"]) and any("Carbon" in q for q in plan["queries"] + plan["deferred_queries"])
    assert plan["budget"]["searches"] == catalog.CATALOG_SEARCH_BUDGET
    labels = {c["topic"] for c in plan["coverage"]}
    assert "타이포그래피" in labels and "IBM Carbon" in labels


def test_goal_without_named_targets_keeps_the_generic_research_plan():
    plan = missions.plan("하네스 엔지니어링 이론과 논문을 조사해줘")
    assert plan["mode"] == "research" and "targets" not in plan and plan["budget"]["searches"] == 16


def test_short_aliases_need_word_boundaries_and_a_design_context():
    assert catalog.targets_in("highlight the garden and spectrum") == []
    assert [t.key for t in catalog.targets_in("Material Design 과 Apple HIG 비교")] == ["material", "hig"]


async def test_trace_reports_goal_requirements_and_failures(client, auth):
    made = await client.post(BASE.replace("/collect", "/collect") + "/missions", headers=auth, json={"goal": GOAL, "max_pages": 50, "max_depth": 1})
    assert made.status_code == 202, made.text
    trace = await client.get(f"{BASE}/missions/{made.json()['id']}/trace", headers=auth)
    assert trace.status_code == 200, trace.text
    body = trace.json()
    assert body["goal"].startswith("# 디자인 시스템") and "Carbon" in body["goal"]
    assert [s["key"] for s in body["stages"]] == ["discover", "acquire", "judge", "load", "ontology"]
    targets = [r for r in body["requirements"] if r["group"] == "target"]
    assert len(targets) >= 30 and all(r["status"] in {"pending", "active", "met", "partial", "blocked", "empty"} for r in targets)
    assert {r["key"] for r in body["rules"]} == {"library", "source", "judge", "failures", "budget", "ontology"}
    assert body["jobs"]["total"] >= 1
    assert set(body["grades"]) == {"공식 표준", "연구·이론", "기업·기관 권장", "경험·실무 글"}
