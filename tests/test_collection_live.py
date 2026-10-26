"""Opt-in public-network acquisition into a disposable test database, never production.

COLLECTION_LIVE=1 PYTHONPATH=src ../back/.venv/bin/python -m pytest -q -s tests/test_collection_live.py
These are bounded transport/product checks, not proof of complete scenario coverage.
"""
import json
import os

import pytest

from ttakkari import kb_delivery
from ttakkari.collector import pipeline, runner

pytestmark = pytest.mark.skipif(os.environ.get("COLLECTION_LIVE") != "1", reason="explicit public-network smoke only")
BASE = "/api/assistant/collect"


@pytest.fixture(autouse=True)
def _no_live_paper_providers():
    # This opt-in module exercises the public providers too; the suite-wide
    # offline replacement would otherwise turn paper/search coverage into a fake.
    yield


async def settle(client, auth, ident, *, ticks=30):
    for _ in range(ticks):
        ran = await runner.consume_once(limit=5, managed_only=True)
        delivered = await kb_delivery.deliver_once()
        response = await client.get(BASE + "/missions/" + ident, headers=auth)
        assert response.status_code == 200, response.text
        detail = response.json()
        if not ran and not delivered:
            return detail
    raise AssertionError(f"mission did not settle: {detail}")


async def close_pipeline():
    if pipeline._default_fetcher is not None:
        await pipeline._default_fetcher.aclose()
    pipeline.configure(None)
    runner.register_handler(None)


@pytest.mark.parametrize("goal,url,query,pages", [
    ("공개 스킬의 지식 수집", "https://github.com/openai/skills/tree/main/skills/.curated/aspnet-core", "ASP.NET", 2),
    ("Python 공식 튜토리얼 체계적 수집", "https://docs.python.org/3/tutorial/", "Python", 3),
])
async def test_live_mission_reaches_real_kb_search(client, auth, goal, url, query, pages):
    pipeline.configure(None)
    pipeline.install()
    try:
        response = await client.post(BASE + "/missions", headers=auth,
                                     json={"goal": goal, "urls": [url], "max_pages": pages, "max_depth": 1})
        assert response.status_code == 202, response.text
        ident = response.json()["id"]
        for _ in range(4):
            await runner.consume_once(limit=1, managed_only=True)
            await kb_delivery.deliver_once()
            detail = (await client.get(BASE + "/missions/" + ident, headers=auth)).json()
            if all(job["status"] not in {"queued", "running"} for job in detail["jobs"]):
                break
        print(json.dumps({"input": url, "mission": ident, "status": detail["status"], "counts": detail["counts"],
                          "gaps": detail["gaps"], "jobs": detail["jobs"], "results": detail["results"]}, ensure_ascii=False))
        assert detail["counts"]["imported"] >= 1, detail
        hits = (await client.get("/api/assistant/kb/search", headers=auth, params={"q": query})).json()
        assert hits["items"], hits
    finally:
        await close_pipeline()


async def test_live_official_docs_extend_continues_the_preserved_frontier(client, auth):
    pipeline.configure(None)
    pipeline.install()
    try:
        made = await client.post(BASE + "/missions", headers=auth, json={
            "goal": "Python tutorial documentation", "urls": ["https://docs.python.org/3/tutorial/"],
            "max_pages": 1, "max_depth": 2,
        })
        assert made.status_code == 202, made.text
        ident = made.json()["id"]
        first = await settle(client, auth, ident)
        assert first["counts"]["imported"] == 1, first
        original_documents = {r["document_id"] for r in first["results"] if r.get("document_id")}
        extended = await client.post(BASE + f"/missions/{ident}/extend", headers=auth, json={"add_pages": 2})
        assert extended.status_code == 200, extended.text
        second = await settle(client, auth, ident)
        assert second["counts"]["imported"] > first["counts"]["imported"], second
        documents = {r["document_id"] for r in second["results"] if r.get("document_id")}
        assert original_documents < documents
        assert (await client.get("/api/assistant/kb/search", headers=auth, params={"q": "Python"})).json()["items"]
        print(json.dumps({"scenario": "D", "before": first["counts"], "after": second["counts"],
                          "gaps": second["gaps"], "runs": second["runs"]}, ensure_ascii=False))
    finally:
        await close_pipeline()


async def test_live_user_context_and_multiple_source_types_share_one_mission(client, auth):
    pipeline.configure(None)
    pipeline.install()
    try:
        context = ("가설: Python asyncio는 비동기 도구 실행의 대기를 줄일 수 있다. "
                   "참고 공식 문서 https://docs.python.org/3/library/asyncio.html "
                   "참고 스킬 https://github.com/openai/skills/tree/main/skills/.curated/aspnet-core "
                   "참고 논문 https://arxiv.org/abs/1706.03762")
        made = await client.post(BASE + "/missions", headers=auth,
                                 json={"goal": "Python asyncio와 에이전트 도구 실행 조사", "context": context,
                                       "max_pages": 4, "max_depth": 0})
        assert made.status_code == 202, made.text
        detail = await settle(client, auth, made.json()["id"])
        plan = detail["plan"]
        assert plan["mode"] == "mixed" and plan["context"]["provenance"] == "user_provided"
        assert {s["adapter"] for s in plan["sources"]} >= {"github", "paper", "website"}
        assert detail["counts"]["imported"] >= 2, detail
        relations = plan["context_links"]["relations"]
        assert any(r["target"].get("document_id") and r["verification"] == "unverified" for r in relations), plan
        hits = (await client.get("/api/assistant/kb/search", headers=auth, params={"q": "asyncio"})).json()
        assert hits["items"], hits
        print(json.dumps({"scenario": "C", "counts": detail["counts"], "relations": relations,
                          "papers": plan.get("papers"), "gaps": detail["gaps"]}, ensure_ascii=False))
    finally:
        await close_pipeline()


async def test_live_topic_without_urls_discovers_and_imports_public_sources(client, auth):
    pipeline.configure(None)
    pipeline.install()
    try:
        made = await client.post(BASE + "/missions", headers=auth,
                                 json={"goal": "Python asyncio", "max_pages": 3, "max_depth": 0})
        assert made.status_code == 202, made.text
        assert made.json()["plan"]["mode"] == "research"
        assert made.json()["plan"]["sources"] == []
        detail = await settle(client, auth, made.json()["id"])
        history = detail["plan"].get("search_history", [])
        assert any(h["status"] == "ok" for h in history), detail
        assert detail["plan"]["sources"], detail
        assert detail["counts"]["imported"] >= 1, detail
        assert (await client.get("/api/assistant/kb/search", headers=auth, params={"q": "asyncio"})).json()["items"]
        print(json.dumps({"scenario": "E", "counts": detail["counts"], "research": detail["research"],
                          "search_history": history, "gaps": detail["gaps"]}, ensure_ascii=False))
    finally:
        await close_pipeline()
