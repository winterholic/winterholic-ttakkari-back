"""Vertical end-to-end integration test for Research Lab v2 using real retrieval_eval adapter."""

import uuid

from ttakkari.db import sessionmaker
from ttakkari.research import runner


async def test_vertical_e2e_rag_research_flow(client, auth):
    # 1. 연구 생성 (Create Research Study)
    study_res = await client.post(
        "/api/research/studies",
        headers=auth,
        json={
            "title": "한국어 RAG 검색 정확도 최적화 연구",
            "goal": "BM25와 어휘 중첩(lexical overlap) 검색기의 Recall@k 및 지연시간 비교",
            "scope": "personal",
            "environment": {"device": "cpu", "python": "3.13"},
            "criteria": ["recall_at_k", "latency_ms"],
        },
    )
    assert study_res.status_code == 201
    study = study_res.json()
    study_id = study["id"]

    # 2. 카탈로그 동기화 및 방법론 선택 (Select RAG methods from catalog)
    await client.post("/api/research/catalog/sync", headers=auth)
    methods_res = await client.get("/api/research/methods?target=rag", headers=auth)
    assert methods_res.status_code == 200
    rag_methods = methods_res.json()["items"]
    assert len(rag_methods) > 0
    selected_method = rag_methods[0]

    # 3. 실험 설계 (Plan AB Experiment using retrieval_eval adapter)
    exp_res = await client.post(
        f"/api/research/studies/{study_id}/experiments",
        headers=auth,
        json={
            "title": "BM25 vs 어휘 중첩 A/B 검색 평가",
            "hypothesis": "BM25가 단순 어휘 중첩 대비 한국어 기술문서 검색 Recall@k를 20% 이상 향상시킬 것이다.",
            "rationale": "정보 검색 이론 및 TF-IDF 역문서 빈도 가중치 효과 검증",
            "method_id": selected_method["id"],
            "adapter_id": "retrieval_eval",
            "strategy": "ab",
            "search_space": {
                "retriever_mode": ["bm25", "lexical_overlap"],
            },
            "fixed_inputs": {
                "k": 3,
                "preset_id": "ko_tech_docs_v1",
            },
            "primary_metric": "recall_at_3",
            "secondary_metrics": ["query_latency_ms_mean"],
            "budget": {"max_runs": 10, "max_total_seconds": 60},
            "auto_start": False,
        },
    )
    assert exp_res.status_code == 201
    exp = exp_res.json()
    exp_id = exp["id"]
    configs = exp["configurations"]
    assert len(configs) == 2
    cfg_base = configs[0]

    # 4. 실제 어댑터 동기 실행 (Run experiment synchronously via runner)
    sm = sessionmaker()
    async with sm() as db:
        created_runs = await runner.start_experiment_runs(
            db, experiment_id=uuid.UUID(exp_id), background=False
        )
        assert len(created_runs) == 2
        await db.commit()

    # 5. 실행 결과 조회 (Verify Succeeded Runs and Measurements)
    runs_res = await client.get(f"/api/research/runs?experiment_id={exp_id}", headers=auth)
    assert runs_res.status_code == 200
    runs = runs_res.json()["runs"]
    assert len(runs) == 2
    for r in runs:
        assert r["status"] == "succeeded"
        assert r["wall_seconds"] is not None

        # Check measurements in run details
        detail = (await client.get(f"/api/research/runs/{r['id']}", headers=auth)).json()
        assert len(detail["measurements"]) > 0
        keys = [m["metric_key"] for m in detail["measurements"]]
        assert "recall_at_3" in keys
        assert "mrr_at_3" in keys

    # 6. 결과 비교 분석 (Compare-Series)
    series_res = await client.get(
        f"/api/research/experiments/{exp_id}/compare-series?baseline_config_id={cfg_base['id']}",
        headers=auth,
    )
    assert series_res.status_code == 200
    series = series_res.json()
    # 지표마다 설정 2개씩: 주요 지표 하나만 담던 이전 응답과 달리 측정된 모든 지표를 담는다
    metric_keys = {m["metric_key"] for m in series["metrics"]}
    assert {"recall_at_3", "mrr_at_3"} <= metric_keys
    assert len(series["delta_bars"]) == 2 * len(metric_keys)
    assert [g["metric_key"] for g in series["metric_bars"]] == [m["metric_key"] for m in series["metrics"]]
    assert all(len(g["values"]) == 2 for g in series["metric_bars"])
    assert len(series["repeat_distributions"]) == 2 * len(metric_keys)
    assert len(series["pareto_points"]) == 2
    assert series["config_diff_table"]["rows"]

    # 7. 최적 결과물 채택 (Adopt Best Result with Decision node)
    best_cfg_id = series["best_configuration_id"] or cfg_base["id"]
    matching_run = next(r for r in runs if r["configuration_id"] == best_cfg_id)

    adopt_res = await client.post(
        f"/api/research/studies/{study_id}/best-results",
        headers=auth,
        json={
            "experiment_id": exp_id,
            "configuration_id": best_cfg_id,
            "run_id": matching_run["id"],
            "objective": "최고의 한국어 기술문서 Recall@k 달성",
            "primary_metric": "recall_at_3",
            "constraints": {"max_latency_ms": 100},
            "rationale": "BM25 설정이 검색 품질과 응답 속도 면에서 최적의 균형을 보임",
            "promote_method": True,
        },
    )
    assert adopt_res.status_code == 201
    best_res = adopt_res.json()
    assert best_res["status"] == "adopted"
    assert best_res["objective"] == "최고의 한국어 기술문서 Recall@k 달성"

    # 8. 연구 재조회 (Resume / Snapshot)
    resume_res = await client.get(f"/api/research/studies/{study_id}/resume", headers=auth)
    assert resume_res.status_code == 200
    snapshot = resume_res.json()
    nodes = snapshot["nodes"]
    decision_nodes = [n for n in nodes if n["kind"] == "decision"]
    assert len(decision_nodes) >= 1
    assert any("최적 결과물 채택" in d["title"] for d in decision_nodes)

    # 9. 저장된 채택 결과를 바탕으로 후속 연구를 설계하고 lineage를 복원한다.
    source_detail_before = (await client.get(f"/api/research/experiments/{exp_id}", headers=auth)).json()
    source_config_before = next(c for c in source_detail_before["configurations"] if c["id"] == best_cfg_id)
    followup_res = await client.post(
        f"/api/research/studies/{study_id}/experiments",
        headers=auth,
        json={
            "title": "채택 설정 기반 후속 검증",
            "adapter_id": "retrieval_eval",
            "strategy": "single",
            "primary_metric": "recall_at_3",
            "fixed_inputs": {"k": 5},
            "source_study_id": study_id,
            "source_experiment_id": exp_id,
            "source_configuration_id": best_cfg_id,
            "source_run_id": matching_run["id"],
            "auto_start": False,
        },
    )
    assert followup_res.status_code == 201, followup_res.text
    followup = followup_res.json()
    followup_id = followup["id"]
    assert followup["source_study_id"] == study_id
    assert followup["source_experiment_id"] == exp_id
    assert followup["source_configuration_id"] == best_cfg_id
    assert followup["source_run_id"] == matching_run["id"]
    assert followup["configurations"][0]["parameters"]["k"] == 5
    assert followup["configurations"][0]["parameters"]["preset_id"] == "ko_tech_docs_v1"

    restored = (await client.get(f"/api/research/experiments/{followup_id}", headers=auth)).json()
    for key, expected in {
        "source_study_id": study_id,
        "source_experiment_id": exp_id,
        "source_configuration_id": best_cfg_id,
        "source_run_id": matching_run["id"],
    }.items():
        assert restored[key] == expected
        assert restored["experiment"][key] == expected
    followup_snapshot = (await client.get(f"/api/research/studies/{study_id}/resume", headers=auth)).json()
    planned_event = next(
        t for t in followup_snapshot["timeline"]
        if t["kind"] == "experiment_planned" and t["payload"].get("id") == followup_id
    )
    assert planned_event["payload"]["source_configuration_id"] == best_cfg_id
    assert planned_event["payload"]["source_run_id"] == matching_run["id"]

    source_detail_after = (await client.get(f"/api/research/experiments/{exp_id}", headers=auth)).json()
    source_config_after = next(c for c in source_detail_after["configurations"] if c["id"] == best_cfg_id)
    assert source_config_after["parameters"] == source_config_before["parameters"]
    assert next(r for r in source_detail_after["runs"] if r["id"] == matching_run["id"])["status"] == "succeeded"
