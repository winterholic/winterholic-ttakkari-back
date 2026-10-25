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
        if pipeline._default_fetcher is not None:
            await pipeline._default_fetcher.aclose()
        pipeline.configure(None)
        runner.register_handler(None)
