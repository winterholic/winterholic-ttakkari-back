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


async def test_rerun_rejects_a_saved_misrouted_plan_without_changing_history(client, auth):
    from sqlalchemy import select

    from ttakkari.collector_models import CollectionMission
    from ttakkari.db import sessionmaker

    goal = (Path(__file__).parent / "fixtures" / "collect_robotics_goal.md").read_text()
    made = await client.post(f"{BASE}/missions", headers=auth, json={"goal": goal})
    mid = made.json()["id"]
    async with sessionmaker()() as db:
        row = await db.scalar(select(CollectionMission).where(CollectionMission.id == mid))
        # Recreate the stored pre-fix plan; do not ask the corrected planner to reproduce its old bug.
        wrong = catalog.plan_parts(goal, [], [t for t in catalog.TOPICS if t["key"] in {"interaction", "components", "enterprise"}])
        row.plan = {**row.plan, **wrong}
        await db.commit()
    response = await client.post(f"{BASE}/missions/{mid}/rerun", headers=auth)
    assert response.status_code == 422 and "조사 범위" in response.text
    detail = (await client.get(f"{BASE}/missions/{mid}", headers=auth)).json()
    assert detail["run_seq"] == 1 and len(detail["runs"]) == 1
    assert {t["key"] for t in detail["plan"]["topics"]} == {"interaction", "components", "enterprise"}


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


def test_supplement_scope_limits_a_run_to_the_chosen_entries():
    entry = {"url": "https://x", "target": "carbon", "topics": ["t:carbon"]}
    other = {"url": "https://y", "topics": ["typography"]}
    plan = {"supplement": {"run_seq": 2, "keys": ["t:carbon"]}}
    assert missions.in_supplement(plan, 2, entry) and not missions.in_supplement(plan, 2, other)
    assert missions.in_supplement(plan, 3, other) and missions.in_supplement({}, 2, other)  # other runs are untouched


def test_supplement_queries_take_a_new_angle_on_the_same_target_or_topic():
    target = catalog.supplement_queries("t:toss", "토스 디자인 시스템")
    assert any("toss.tech" in q for q in target) and all("documentation guidelines" not in q for q in target)
    assert any("typography" in q for q in catalog.supplement_queries("typography", "타이포그래피"))


async def test_supplement_starts_a_run_for_weak_entries_only(client, auth):
    from sqlalchemy import select, update

    from ttakkari.collector_models import CollectionMission
    from ttakkari.db import sessionmaker

    made = await client.post(f"{BASE}/missions", headers=auth, json={"goal": GOAL, "max_pages": 50, "max_depth": 1})
    mid = made.json()["id"]
    busy = await client.post(f"{BASE}/missions/{mid}/supplement", headers=auth, json={"keys": ["t:carbon"]})
    assert busy.status_code == 422 and "진행 중" in busy.text  # a running mission is never supplemented underneath itself
    async with sessionmaker()() as db:
        await db.execute(update(CollectionMission).where(CollectionMission.id == mid).values(status="partial"))
        await db.commit()
    unknown = await client.post(f"{BASE}/missions/{mid}/supplement", headers=auth, json={"keys": ["t:nope"]})
    assert unknown.status_code == 422
    done = await client.post(f"{BASE}/missions/{mid}/supplement", headers=auth, json={"keys": ["t:carbon", "typography"]})
    assert done.status_code == 200, done.text
    body = done.json()
    assert body["supplement_keys"] == ["t:carbon", "typography"] and body["run_seq"] == 2
    async with sessionmaker()() as db:
        plan = (await db.scalar(select(CollectionMission).where(CollectionMission.id == mid))).plan
    assert plan["supplement"] == {"run_seq": 2, "keys": ["t:carbon", "typography"]}
    assert any("case study" in q for q in plan["queries"])
    assert plan["research"]["run_seq"] == 2 and plan["research"]["search_cursor"] >= 30  # earlier queries are not repeated
