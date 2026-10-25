"""Bounded CPU acceptance for composing two real retrieval interventions."""

import hashlib
import json
import os
import uuid
from pathlib import Path

from sqlalchemy import text

from ttakkari.db import sessionmaker
from ttakkari.research import runner


async def test_chunking_hybrid_grid_and_combined_repeat_roundtrip(client, auth):
    async with sessionmaker()() as db:
        identity = (await db.execute(text("select current_database(), current_user"))).one()
        assert tuple(identity) == ("ttakkari_test", "ttk_test")

    response = await client.post("/api/research/catalog/sync", headers=auth)
    assert response.status_code == 200, response.text
    response = await client.get("/api/research/methods?target=rag&limit=100", headers=auth)
    assert response.status_code == 200, response.text
    methods = {m["slug"]: m for m in response.json()["items"]}
    method_id = methods["document_chunking_strategy"]["id"]
    compared_id = methods["hybrid_retrieval"]["id"]
    response = await client.post("/api/research/studies", headers=auth, json={
        "title": "CPU 청크 분할 × 하이브리드 검색 조합 인수",
        "goal": "두 개입의 단독/조합 실행 및 반복 결과 보존 검증; 일반 성능 향상 가설 아님",
        "scope": "personal", "criteria": ["recall_at_5", "ndcg_at_5"],
    })
    assert response.status_code == 201, response.text
    study_id = response.json()["id"]
    common = {
        "method_id": method_id, "compared_method_ids": [compared_id],
        "adapter_id": "retrieval_eval", "primary_metric": "recall_at_5",
        "secondary_metrics": ["ndcg_at_5", "query_latency_ms_mean"],
        "fixed_inputs": {"preset_id": "ko_tech_docs_v1", "k": 5,
                         "chunk_size": 128, "chunk_overlap": 16},
        "budget": {"max_runs": 4, "max_total_seconds": 60},
    }
    grid_body = {
        **common, "title": "2×2: BM25/Hybrid × 原文/128字chunk", "strategy": "grid",
        "search_space": {"chunking_strategy": ["none", "fixed_window"],
                         "retriever_mode": ["bm25", "hybrid_rrf"]},
    }
    preview_body = {k: v for k, v in grid_body.items() if k not in {"title", "secondary_metrics"}}
    preview = await client.post("/api/research/experiments/validate", headers=auth,
                                json={**preview_body, "study_id": study_id})
    assert preview.status_code == 200, preview.text
    plan = preview.json()
    assert plan["valid"], plan
    assert any("조합" in warning for warning in plan["warnings"]), plan
    response = await client.post(f"/api/research/studies/{study_id}/experiments",
                                 headers=auth, json=grid_body)
    assert response.status_code == 201, response.text
    grid = response.json()
    assert len(grid["configurations"]) == 4

    async def execute_and_reload(experiment_id, expected_count):
        async with sessionmaker()() as db:
            rows = await runner.start_experiment_runs(db, uuid.UUID(experiment_id), background=False)
            assert len(rows) == expected_count
        response = await client.get(f"/api/research/experiments/{experiment_id}", headers=auth)
        assert response.status_code == 200, response.text
        fresh = response.json()
        details = []
        for run in fresh["runs"]:
            response = await client.get(f"/api/research/runs/{run['id']}", headers=auth)
            assert response.status_code == 200, response.text
            detail = response.json()
            assert detail["status"] == "succeeded", detail
            assert detail["execution_origin"] == "local_measured"
            assert any(m["metric_key"] == "recall_at_5" for m in detail["measurements"])
            details.append(detail)
        assert len(details) == expected_count
        return fresh, details

    grid_fresh, grid_runs = await execute_and_reload(grid["id"], 4)
    configs = {c["id"]: c for c in grid_fresh["configurations"]}
    observed = set()
    chunks = {}
    for run in grid_runs:
        params = configs[run["configuration_id"]]["parameters"]
        raw = run["raw_output"]
        assert raw["chunking_strategy"] == params["chunking_strategy"]
        assert raw["retriever_mode"] == params["retriever_mode"]
        assert raw["k"] == 5
        observed.add((raw["chunking_strategy"], raw["retriever_mode"]))
        chunks[raw["chunking_strategy"]] = raw["chunk_count"]
    assert observed == {(c, r) for c in ("none", "fixed_window") for r in ("bm25", "hybrid_rrf")}
    assert chunks["fixed_window"] > chunks["none"], chunks
    assert grid_fresh["method_id"] == method_id
    assert grid_fresh["compared_method_ids"] == [compared_id]
    response = await client.get(f"/api/research/experiments/{grid['id']}/compare-series",
                                headers=auth)
    assert response.status_code == 200, response.text
    comparison = response.json()
    assert len(comparison["pareto_points"]) == 4

    repeat_body = {**common, "title": "128字chunk + Hybrid 두 번 실행", "strategy": "repeat",
                   "fixed_inputs": {**common["fixed_inputs"], "chunking_strategy": "fixed_window",
                                    "retriever_mode": "hybrid_rrf"},
                   "budget": {"max_runs": 2, "repetitions": 2, "max_total_seconds": 60}}
    response = await client.post(f"/api/research/studies/{study_id}/experiments",
                                 headers=auth, json=repeat_body)
    assert response.status_code == 201, response.text
    repeat, repeat_runs = await execute_and_reload(response.json()["id"], 2)
    assert {r["repeat_index"] for r in repeat_runs} == {1, 2}
    response = await client.get(f"/api/research/experiments/{repeat['id']}/compare-series", headers=auth)
    assert response.status_code == 200, response.text
    repeat_comparison = response.json()
    assert any(len(row.get("values", [])) == 2 for row in repeat_comparison["repeat_distributions"]), repeat_comparison

    output = os.environ.get("RESEARCH_COMBINATION_RECEIPT")
    if output:
        root = Path(__file__).resolve().parents[1]
        sources = ["src/ttakkari/research/adapters/retrieval_eval.py",
                   "src/ttakkari/research/adapters/presets/ko_tech_docs_v1.json",
                   "src/ttakkari/research/planner.py", "src/ttakkari/research/runner.py"]
        receipt = {"environment": "Codex command sandbox, real ASGI/Run PostgreSQL, CPU hash embedder",
                   "identity": list(identity), "study_id": study_id, "plan": plan,
                   "grid": grid_fresh, "grid_runs": grid_runs, "comparison": comparison,
                   "repeat": repeat, "repeat_runs": repeat_runs, "repeat_comparison": repeat_comparison,
                   "source_sha256": {p: hashlib.sha256((root / p).read_bytes()).hexdigest() for p in sources},
                   "limitations": ["No browser, operating MCP, neural embedding, GPU, or general quality improvement claim.",
                                   "Bounded ko_tech_docs_v1 fixture; two repeats verify persistence, not statistical confidence."]}
        Path(output).write_text(json.dumps(receipt, ensure_ascii=False, indent=2), encoding="utf-8")
