"""Core backend tests for Research Lab v2: methods, relations, validation, runner, measurements, analyzer, promote, immutability, and scope isolation."""

import uuid
import pytest
from sqlalchemy import text

from ttakkari.db import sessionmaker
from ttakkari.research import planner
from ttakkari.research.models import (
    BestResult,
    Configuration,
    Experiment,
    Measurement,
    Method,
    Run,
    Study,
)


async def create_test_study(client, auth, scope="personal"):
    r = await client.post(
        "/api/research/studies",
        headers=auth,
        json={
            "title": f"Study {uuid.uuid4().hex[:6]}",
            "goal": "Test goal",
            "scope": scope,
            "environment": {"device": "cpu"},
            "criteria": ["recall_at_k"],
        },
    )
    assert r.status_code == 201
    return r.json()["id"]


async def test_method_create_update_fork_and_relations(client, auth):
    # 1. Register a new custom hypothesis method
    m_slug = f"hypothesis-{uuid.uuid4().hex[:8]}"
    create_res = await client.post(
        "/api/research/methods",
        headers=auth,
        json={
            "slug": m_slug,
            "name": "Custom Hypothesis Method",
            "name_ko": "사용자 가설 기법",
            "summary": "Testing hypothesis creation",
            "targets": ["rag"],
            "layers": ["retrieval_index"],
            "goals": ["accuracy_quality"],
            "parameters": [{"key": "param_a", "type": "number", "default": 1.0}],
            "metrics": ["recall_at_k"],
            "lifecycle_status": "hypothesis",
        },
    )
    assert create_res.status_code == 201
    method_data = create_res.json()
    assert method_data["slug"] == m_slug
    assert method_data["version"] == 1
    assert method_data["origin"] == "user_registered"
    assert method_data["lifecycle_status"] == "hypothesis"

    m_id = method_data["id"]

    # 2. Update method in-place (since it's user_registered)
    patch_res = await client.patch(
        f"/api/research/methods/{m_id}",
        headers=auth,
        json={"summary": "Updated hypothesis summary"},
    )
    assert patch_res.status_code == 200
    assert patch_res.json()["summary"] == "Updated hypothesis summary"
    assert patch_res.json()["version"] == 1

    # 3. Create another method and link with relation
    m_slug2 = f"method2-{uuid.uuid4().hex[:8]}"
    create_res2 = await client.post(
        "/api/research/methods",
        headers=auth,
        json={
            "slug": m_slug2,
            "name": "Second Method",
            "name_ko": "두 번째 기법",
            "summary": "For relation test",
            "parameters": [],
            "metrics": [],
        },
    )
    m2_id = create_res2.json()["id"]

    rel_res = await client.post(
        f"/api/research/methods/{m_id}/relations",
        headers=auth,
        json={
            "to_method_id": m2_id,
            "relation_type": "conflicts_with",
            "note": "이 기법들은 함께 쓰면 충돌합니다.",
        },
    )
    assert rel_res.status_code == 201
    assert rel_res.json()["relation_type"] == "conflicts_with"

    # Verify method get includes relations
    get_res = await client.get(f"/api/research/methods/{m_id}", headers=auth)
    assert get_res.status_code == 200
    details = get_res.json()
    assert len(details["relations"]) == 1
    assert details["relations"][0]["to_method_slug"] == m_slug2


async def test_planner_validation_conflicts_budget_and_bayesian(client, auth):
    study_id = await create_test_study(client, auth)

    # 1. Create two conflicting methods
    m1 = (
        await client.post(
            "/api/research/methods",
            headers=auth,
            json={
                "slug": f"c1-{uuid.uuid4().hex[:6]}",
                "name": "Method 1",
                "name_ko": "기법 1",
                "summary": "...",
            },
        )
    ).json()
    m2 = (
        await client.post(
            "/api/research/methods",
            headers=auth,
            json={
                "slug": f"c2-{uuid.uuid4().hex[:6]}",
                "name": "Method 2",
                "name_ko": "기법 2",
                "summary": "...",
            },
        )
    ).json()

    await client.post(
        f"/api/research/methods/{m1['id']}/relations",
        headers=auth,
        json={"to_method_id": m2["id"], "relation_type": "conflicts_with", "note": "충돌 검증"},
    )

    # Validate combination with conflict
    val_conflict = await client.post(
        "/api/research/experiments/validate",
        headers=auth,
        json={
            "study_id": study_id,
            "method_id": m1["id"],
            "compared_method_ids": [m2["id"]],
            "strategy": "single",
            "search_space": {},
            "budget": {"max_runs": 20},
        },
    )
    assert val_conflict.status_code == 200
    res = val_conflict.json()
    assert not res["valid"]
    assert any("방법론 충돌" in err for err in res["errors"])

    # 2. Budget explosion in grid
    val_budget = await client.post(
        "/api/research/experiments/validate",
        headers=auth,
        json={
            "study_id": study_id,
            "method_id": m1["id"],
            "strategy": "grid",
            "search_space": {
                "a": [1, 2, 3, 4, 5],
                "b": [1, 2, 3, 4, 5],
            },  # 25 combinations > max_runs 10
            "budget": {"max_runs": 10},
        },
    )
    assert val_budget.status_code == 200
    assert not val_budget.json()["valid"]
    assert any("최대 허용 예산" in err for err in val_budget.json()["errors"])

    # 3. Bayesian strategy unsupported without optuna
    val_bayes = await client.post(
        "/api/research/experiments/validate",
        headers=auth,
        json={
            "study_id": study_id,
            "method_id": m1["id"],
            "strategy": "bayesian",
            "search_space": {"lr": [0.001, 0.01]},
            "budget": {"max_runs": 10},
        },
    )
    assert val_bayes.status_code == 200
    assert not val_bayes.json()["valid"]
    assert any("미지원" in err for err in val_bayes.json()["errors"])


async def test_external_report_integrity_and_measurement_immutability(client, auth):
    study_id = await create_test_study(client, auth)

    # Create experiment
    exp = (
        await client.post(
            f"/api/research/studies/{study_id}/experiments",
            headers=auth,
            json={
                "title": "External Report Evaluation",
                "hypothesis": "Testing external measurements",
                "rationale": "External test",
                "strategy": "single",
            },
        )
    ).json()

    # Record external report
    rep_res = await client.post(
        f"/api/research/experiments/{exp['id']}/external-report",
        headers=auth,
        json={
            "provenance": "external_benchmark",
            "source_title": "Synthetic core fixture",
            "source_url": "https://example.invalid/research-core-fixture",
            "environment_snapshot": {"cluster": "h100-cloud"},
            "metrics": [
                {
                    "metric_key": "decode_tps",
                    "metric_label": "디코드 처리량",
                    "value": 142.5,
                    "unit": "tok/s",
                    "direction": "higher_is_better",
                },
                {
                    "metric_key": "vram_peak_mb",
                    "metric_label": "VRAM 피크",
                    "value": 14200.0,
                    "unit": "MB",
                    "direction": "lower_is_better",
                },
            ],
            "raw_output": {"note": "Paper benchmark import"},
        },
    )
    assert rep_res.status_code == 201
    run_data = rep_res.json()
    assert run_data["execution_origin"] == "external_report"
    assert run_data["status"] == "succeeded"

    # Verify run details and measurements
    run_details = (
        await client.get(f"/api/research/runs/{run_data['id']}", headers=auth)
    ).json()
    assert len(run_details["measurements"]) == 2
    meas_id = run_details["measurements"][0]["id"]

    # Verify Measurement Immutability trigger in DB
    sm = sessionmaker()
    async with sm() as db:
        with pytest.raises(Exception) as exc_info:
            await db.execute(
                text(
                    "UPDATE research_measurements SET value = 999.0 WHERE id = :id"
                ),
                {"id": meas_id},
            )
            await db.commit()
        assert "research history is immutable" in str(exc_info.value).lower()


async def test_compare_series_structure_and_pareto(client, auth):
    study_id = await create_test_study(client, auth)

    # Create AB experiment with 2 configs
    exp = (
        await client.post(
            f"/api/research/studies/{study_id}/experiments",
            headers=auth,
            json={
                "title": "AB Speed Test",
                "strategy": "ab",
                "search_space": {"threads": [4, 8]},
                "primary_metric": "decode_tps",
                "secondary_metrics": ["latency_ms"],
            },
        )
    ).json()

    exp_id = exp["id"]
    configs = exp["configurations"]
    assert len(configs) >= 2
    cfg_base = configs[0]
    cfg_treat = configs[1]

    # Insert successful runs for each configuration
    await client.post(
        f"/api/research/experiments/{exp_id}/external-report",
        headers=auth,
        json={
            "provenance": "manual_evaluation",
            "configuration_id": cfg_base["id"],
            "metrics": [
                {"metric_key": "decode_tps", "value": 30.0, "direction": "higher_is_better"},
                {"metric_key": "latency_ms", "value": 50.0, "direction": "lower_is_better"},
            ],
        },
    )
    await client.post(
        f"/api/research/experiments/{exp_id}/external-report",
        headers=auth,
        json={
            "provenance": "manual_evaluation",
            "configuration_id": cfg_treat["id"],
            "metrics": [
                {"metric_key": "decode_tps", "value": 45.0, "direction": "higher_is_better"},
                {"metric_key": "latency_ms", "value": 40.0, "direction": "lower_is_better"},
            ],
        },
    )

    # 1. Trigger analyze
    analyze_res = await client.post(
        f"/api/research/experiments/{exp_id}/analyze",
        headers=auth,
        json={"baseline_configuration_id": cfg_base["id"]},
    )
    assert analyze_res.status_code == 200
    analysis = analyze_res.json()
    assert analysis["best_configuration_id"] == cfg_treat["id"]
    assert "Treatment" in analysis["recommendation_ko"]

    # 2. Get compare-series
    series_res = await client.get(
        f"/api/research/experiments/{exp_id}/compare-series?baseline_config_id={cfg_base['id']}",
        headers=auth,
    )
    assert series_res.status_code == 200
    series = series_res.json()

    # Check 7 required structures
    assert "delta_bars" in series
    assert "metric_bars" in series
    assert "repeat_distributions" in series
    assert "pareto_points" in series
    assert "pareto_frontier" in series
    assert "ablation_impacts" in series
    assert "config_diff_table" in series

    # Check delta calculation (+50% improvement)
    treat_delta = next(d for d in series["delta_bars"] if d["config_id"] == cfg_treat["id"])
    assert treat_delta["rel_delta_pct"] == pytest.approx(50.0, rel=1e-2)
    assert treat_delta["is_improvement"] is True


async def test_adopt_best_result_and_method_promote(client, auth):
    study_id = await create_test_study(client, auth)

    # Register hypothesis method
    method = (
        await client.post(
            "/api/research/methods",
            headers=auth,
            json={
                "slug": f"prom-method-{uuid.uuid4().hex[:6]}",
                "name": "Promotable Method",
                "lifecycle_status": "hypothesis",
                "metrics": ["recall_at_k"],
            },
        )
    ).json()

    # Create experiment referencing this method
    exp = (
        await client.post(
            f"/api/research/studies/{study_id}/experiments",
            headers=auth,
            json={
                "title": "Promotion Experiment",
                "method_id": method["id"],
                "strategy": "single",
                "primary_metric": "recall_at_k",
            },
        )
    ).json()

    # Record succeeded local run
    rep = (
        await client.post(
            f"/api/research/experiments/{exp['id']}/external-report",
            headers=auth,
            json={
                "provenance": "external_benchmark",
                "source_title": "Synthetic core fixture",
                "source_url": "https://example.invalid/research-core-fixture",
                "metrics": [{"metric_key": "recall_at_k", "value": 0.92}],
            },
        )
    ).json()

    # Adopt Best Result with promote_method=True
    adopt_res = await client.post(
        f"/api/research/studies/{study_id}/best-results",
        headers=auth,
        json={
            "experiment_id": exp["id"],
            "configuration_id": exp["configurations"][0]["id"],
            "run_id": rep["id"],
            "objective": "Max retrieval quality",
            "primary_metric": "recall_at_k",
            "constraints": {"max_latency_ms": 100},
            "rationale": "Achieved 0.92 recall without latency penalty",
            "promote_method": True,
        },
    )
    assert adopt_res.status_code == 201
    best_res = adopt_res.json()
    assert best_res["status"] == "adopted"
    assert best_res["objective"] == "Max retrieval quality"

    # Check method is now promoted to candidate (due to external_report)
    method_after = (
        await client.get(f"/api/research/methods/{method['id']}", headers=auth)
    ).json()
    assert method_after["lifecycle_status"] in ("candidate", "validated")


async def test_scope_isolation_and_system_catalog_sharing(client, auth):
    # Personal study
    s_personal = await create_test_study(client, auth, scope="personal")

    # Sync catalog to ensure system methods exist
    await client.post("/api/research/catalog/sync", headers=auth)

    # 1. System catalog methods visible in personal scope
    list_p = (
        await client.get("/api/research/methods?scope=personal&limit=10", headers=auth)
    ).json()
    assert list_p["total"] > 0
    assert any(m["origin"] == "system_seed" for m in list_p["items"])

    # 2. System catalog methods also visible in work scope
    list_w = (
        await client.get("/api/research/methods?scope=work&limit=10", headers=auth)
    ).json()
    assert list_w["total"] > 0
    assert any(m["origin"] == "system_seed" for m in list_w["items"])

    # 3. Personal custom method NOT visible in work scope
    custom_m = (
        await client.post(
            "/api/research/methods?scope=personal",
            headers=auth,
            json={
                "slug": f"secret-personal-{uuid.uuid4().hex[:6]}",
                "name": "Personal Only Method",
            },
        )
    ).json()

    work_search = (
        await client.get(
            f"/api/research/methods?q={custom_m['slug']}&scope=work", headers=auth
        )
    ).json()
    assert work_search["total"] == 0


async def test_regression_runner_gate_rejection(client, auth):
    """Defect 1: runner must not execute runs when generation accepted-work gate yields False."""
    from unittest.mock import patch
    from contextlib import asynccontextmanager
    from ttakkari.research import runner

    study_id = await create_test_study(client, auth)
    exp = (
        await client.post(
            f"/api/research/studies/{study_id}/experiments",
            headers=auth,
            json={
                "title": "Gate Rejection Test",
                "strategy": "single",
                "search_space": {"param": [1]},
            },
        )
    ).json()

    exp_id = uuid.UUID(exp["id"])
    cfg_id = uuid.UUID(exp["configurations"][0]["id"])

    # Create a queued run in DB
    sm = sessionmaker()
    run_id = uuid.uuid4()
    async with sm() as db:
        run_obj = Run(
            id=run_id,
            experiment_id=exp_id,
            configuration_id=cfg_id,
            study_id=uuid.UUID(study_id),
            adapter_id="retrieval_eval",
            status="queued",
        )
        db.add(run_obj)
        await db.commit()

    @asynccontextmanager
    async def mock_gate(kind: str, accepted_generation_id=None):
        yield False

    with patch("ttakkari.research.runner.generation.enter_accepted_work", mock_gate):
        await runner.run_experiment_batch(exp_id, [run_id])

    async with sm() as db:
        updated_exp = await db.get(Experiment, exp_id)
        updated_run = await db.get(Run, run_id)
        assert updated_exp.state == "cancelled"
        assert updated_run.status == "cancelled"
        assert updated_run.error_code == "GENERATION_REJECTED"
        assert updated_run.error_message is not None
        assert "rejected" in updated_run.error_message.lower() or "gate" in updated_run.error_message.lower()


async def test_regression_external_report_provenance_roundtrip(client, auth):
    """Defect 2: external report must validate and preserve provenance in raw_output and measurement meta."""
    study_id = await create_test_study(client, auth)
    exp = (
        await client.post(
            f"/api/research/studies/{study_id}/experiments",
            headers=auth,
            json={"title": "Provenance Test", "strategy": "single"},
        )
    ).json()

    # 1. Invalid provenance must be rejected with 422
    bad_res = await client.post(
        f"/api/research/experiments/{exp['id']}/external-report",
        headers=auth,
        json={
            "provenance": "unknown_source_forge",
            "metrics": [{"metric_key": "score", "value": 1.0}],
        },
    )
    assert bad_res.status_code == 422

    # 2. Valid provenance (e.g. manual_evaluation) must round-trip
    rep_res = await client.post(
        f"/api/research/experiments/{exp['id']}/external-report",
        headers=auth,
        json={
            "provenance": "manual_evaluation",
            "metrics": [{"metric_key": "user_rating", "value": 4.5}],
            "raw_output": {"note": "Human rating session"},
        },
    )
    assert rep_res.status_code == 201
    run_data = rep_res.json()
    assert run_data["raw_output"].get("provenance") == "manual_evaluation"

    # Query run details
    run_details = (
        await client.get(f"/api/research/runs/{run_data['id']}", headers=auth)
    ).json()
    assert run_details["raw_output"].get("provenance") == "manual_evaluation"
    assert run_details["measurements"][0]["meta"].get("provenance") == "manual_evaluation"


async def test_regression_best_result_status_and_consistency(client, auth):
    """Defect 3: best result must preserve rejected status and enforce membership consistency."""
    study_id = await create_test_study(client, auth)
    other_study_id = await create_test_study(client, auth)

    exp = (
        await client.post(
            f"/api/research/studies/{study_id}/experiments",
            headers=auth,
            json={"title": "Best Result Consistency Test", "strategy": "single"},
        )
    ).json()
    cfg_id = exp["configurations"][0]["id"]

    # External report run
    rep_res = await client.post(
        f"/api/research/experiments/{exp['id']}/external-report",
        headers=auth,
        json={
            "provenance": "manual_evaluation",
            "configuration_id": cfg_id,
            "metrics": [{"metric_key": "accuracy", "value": 0.5}],
        },
    )
    run_id = rep_res.json()["id"]

    # Other experiment in other study
    other_exp = (
        await client.post(
            f"/api/research/studies/{other_study_id}/experiments",
            headers=auth,
            json={"title": "Other Study Exp", "strategy": "single"},
        )
    ).json()

    # 1. Membership mismatch: experiment belongs to other study -> must fail with 4xx
    mismatch_res = await client.post(
        f"/api/research/studies/{study_id}/best-results",
        headers=auth,
        json={
            "experiment_id": other_exp["id"],
            "configuration_id": cfg_id,
            "run_id": run_id,
            "objective": "Max accuracy",
            "primary_metric": "accuracy",
            "rationale": "Mismatched components",
            "status": "adopted",
        },
    )
    assert 400 <= mismatch_res.status_code < 500

    # 2. Rejected status: must be recorded as rejected, decision node marked as rejected, and study.best_result_id not set
    reject_res = await client.post(
        f"/api/research/studies/{study_id}/best-results",
        headers=auth,
        json={
            "experiment_id": exp["id"],
            "configuration_id": cfg_id,
            "run_id": run_id,
            "objective": "Target accuracy >= 0.9",
            "primary_metric": "accuracy",
            "rationale": "Accuracy 0.5 did not meet threshold",
            "status": "rejected",
        },
    )
    assert reject_res.status_code == 201
    best_res_data = reject_res.json()
    assert best_res_data["status"] == "rejected"

    study_after = (
        await client.get(f"/api/research/studies/{study_id}", headers=auth)
    ).json()
    assert study_after.get("best_result_id") != best_res_data["id"]


async def test_regression_pareto_single_metric_and_x_metric_passthrough(client, auth):
    """Defect 4: single metric must not produce Pareto using same metric on both axes; pareto_x_metric must pass through."""
    study_id = await create_test_study(client, auth)

    # 1. Experiment with single metric only
    exp_single = (
        await client.post(
            f"/api/research/studies/{study_id}/experiments",
            headers=auth,
            json={
                "title": "Single Metric Exp",
                "strategy": "ab",
                "search_space": {"opt": [1, 2]},
                "primary_metric": "accuracy_only",
                "secondary_metrics": [],
            },
        )
    ).json()

    cfg1, cfg2 = exp_single["configurations"][0], exp_single["configurations"][1]
    await client.post(
        f"/api/research/experiments/{exp_single['id']}/external-report",
        headers=auth,
        json={"configuration_id": cfg1["id"], "metrics": [{"metric_key": "accuracy_only", "value": 0.8}]},
    )
    await client.post(
        f"/api/research/experiments/{exp_single['id']}/external-report",
        headers=auth,
        json={"configuration_id": cfg2["id"], "metrics": [{"metric_key": "accuracy_only", "value": 0.9}]},
    )

    series_single = (
        await client.get(
            f"/api/research/experiments/{exp_single['id']}/compare-series",
            headers=auth,
        )
    ).json()

    # Must NOT compute Pareto frontier using single metric as both axes
    assert series_single["pareto_frontier"] == []
    assert series_single.get("pareto_status") == "unavailable"
    assert "pareto_unavailable_reason" in series_single
    assert "2개" in series_single["pareto_unavailable_reason"] or "단일" in series_single["pareto_unavailable_reason"]

    # 2. Multi-metric experiment with pareto_x_metric passthrough to analyze
    exp_multi = (
        await client.post(
            f"/api/research/studies/{study_id}/experiments",
            headers=auth,
            json={
                "title": "Multi Metric Exp",
                "strategy": "ab",
                "search_space": {"opt": [1, 2]},
                "primary_metric": "latency",
                "secondary_metrics": ["vram", "cost"],
            },
        )
    ).json()
    m_cfg1, m_cfg2 = exp_multi["configurations"][0], exp_multi["configurations"][1]
    for cfg, lat, vram in [(m_cfg1, 50.0, 1000.0), (m_cfg2, 30.0, 1500.0)]:
        await client.post(
            f"/api/research/experiments/{exp_multi['id']}/external-report",
            headers=auth,
            json={
                "provenance": "manual_evaluation",
                "configuration_id": cfg["id"],
                "metrics": [
                    {"metric_key": "latency", "value": lat, "direction": "lower_is_better"},
                    {"metric_key": "vram", "value": vram, "direction": "lower_is_better"},
                ],
            },
        )

    # Analyze with explicit pareto_x_metric="vram" and pareto_y_metric="latency"
    analyze_res = await client.post(
        f"/api/research/experiments/{exp_multi['id']}/analyze",
        headers=auth,
        json={
            "pareto_x_metric": "vram",
            "pareto_y_metric": "latency",
        },
    )
    assert analyze_res.status_code == 200
    summary = analyze_res.json()["summary"]
    assert summary["secondary_metric"] == "vram"
    assert summary["primary_metric"] == "latency"
    assert len(summary["pareto_frontier"]) > 0

