"""Bounded C/D and retrieval-payload probe for the a564c05 Research Lab checkout.

Requires the per-run TTAKKARI_TEST_DB and a local backend serving this checkout.
No GPU/TTS/Whisper calls are made. All external/manual values are explicitly synthetic.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
from typing import Any

import httpx
from sqlalchemy import func, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from ttakkari.research.models import Configuration, Measurement, Run
from ttakkari.security.auth import issue_access_token


def require_test_identity(raw_url: str) -> None:
    url = make_url(raw_url)
    identity = (url.database, url.username, url.host)
    print("preconnect_identity:", *identity)
    if identity not in {
        ("ttakkari_test", "ttk_test", "localhost"),
        ("ttakkari_test", "ttk_test", "127.0.0.1"),
    }:
        raise RuntimeError("test database identity mismatch; no connection/API call attempted")


async def request_json(client: httpx.AsyncClient, method: str, path: str, **kwargs: Any):
    response = await client.request(method, path, **kwargs)
    if response.status_code >= 400:
        return response, None
    return response, response.json()


async def run_probe(base_url: str, output: Path) -> dict[str, Any]:
    raw_url = os.environ.get("TTAKKARI_TEST_DB")
    if not raw_url:
        raise RuntimeError("TTAKKARI_TEST_DB is unset")
    require_test_identity(raw_url)

    engine = create_async_engine(raw_url, pool_pre_ping=True)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with sessions() as db:
        db_identity = (await db.execute(text("select current_database(), current_user"))).one()
        if (db_identity[0], db_identity[1]) != ("ttakkari_test", "ttk_test"):
            raise RuntimeError("connected test DB identity mismatch")
        print("connected_identity:", db_identity[0], db_identity[1])

    token, _ = issue_access_token()
    headers = {"Authorization": f"Bearer {token}"}
    result: dict[str, Any] = {
        "evidence_kind": "synthetic_and_local_measured_test_db_probe",
        "database_identity": {"database": "ttakkari_test", "user": "ttk_test", "host": make_url(raw_url).host},
        "backend_base": base_url,
        "source_revision": "a564c05c197367aa267b33b05be168d051e64716",
        "checks": {},
    }

    async with httpx.AsyncClient(base_url=base_url, headers=headers, timeout=30) as client:
        health = await client.get("/api/health")
        result["checks"]["backend_health"] = {"status": health.status_code}

        study_response, study = await request_json(
            client,
            "POST",
            "/api/research/studies",
            json={
                "title": "독립 인수 C·D 실행 증거",
                "goal": "실측 검색 baseline과 무개선 결과 보존, 새 가설의 수동 결과 등록 및 구조적 재사용 확인",
                "scope": "personal",
                "environment": {"fixture": "synthetic test DB only"},
                "criteria": ["recall_at_20", "mrr_at_20", "ndcg_at_20"],
            },
        )
        if study_response.status_code != 201:
            result["checks"]["study_create"] = {"status": study_response.status_code, "body": study_response.text[:500]}
            return result
        study_id = study["id"]
        result["study_id"] = study_id

        # Expand source disagreement probes beyond the original regression matrix:
        # every source field is varied in each nested persistence location, while
        # the top-level source remains canonical. Each rejection must be pre-write.
        source_cases = []
        conflict_values = {
            "source_title": "Conflicting synthetic title",
            "source_url": "https://example.invalid/conflict",
            "source_locator": "doi:10.0000/conflict",
            "source_year": 2022,
            "provenance": "manual_evaluation",
            "execution_origin": "local_execution",
        }
        for location in ("raw_output", "environment_snapshot", "meta"):
            for field, value in conflict_values.items():
                source_cases.append((location, field, value))
        source_results = []
        for index, (location, field, value) in enumerate(source_cases):
            exp_response, exp_body = await request_json(
                client,
                "POST",
                f"/api/research/studies/{study_id}/experiments",
                json={"title": f"source conflict {index}", "adapter_id": "external_report", "strategy": "single"},
            )
            exp_id = exp_body.get("experiment", {}).get("id") if exp_body else None
            if exp_response.status_code != 201 or not exp_id:
                source_results.append({"location": location, "field": field, "experiment_status": exp_response.status_code, "report_status": None, "run_count": None, "measurement_count": None})
                continue
            exp_cfg = exp_body["configurations"][0]["id"]
            report_body = {
                "configuration_id": exp_cfg,
                "provenance": "external_benchmark",
                "source_title": "Synthetic canonical source",
                "source_url": "https://example.invalid/canonical",
                "source_year": 2026,
                "metrics": [{"metric_key": "decode_tps", "value": 12.0}],
            }
            if location == "meta":
                report_body["metrics"][0]["meta"] = {field: value}
            else:
                report_body[location] = {field: value}
            response = await client.post(f"/api/research/experiments/{exp_id}/external-report", json=report_body)
            async with sessions() as db:
                run_count = await db.scalar(select(func.count()).select_from(Run).where(Run.experiment_id == exp_id))
                measurement_count = await db.scalar(select(func.count()).select_from(Measurement).where(Measurement.experiment_id == exp_id))
            source_results.append({
                "location": location,
                "field": field,
                "experiment_status": exp_response.status_code,
                "report_status": response.status_code,
                "rejected_field_named": field in response.text if response.status_code >= 400 else False,
                "run_count": run_count,
                "measurement_count": measurement_count,
            })

        # Source-free manual and imported reports are legitimate; they remain
        # explicitly labeled without inventing a citation.
        source_free_results = []
        for provenance in ("manual_evaluation", "imported_log"):
            exp_response, exp_body = await request_json(
                client,
                "POST",
                f"/api/research/studies/{study_id}/experiments",
                json={"title": f"source-free {provenance}", "adapter_id": "external_report", "strategy": "single"},
            )
            exp_id = exp_body.get("experiment", {}).get("id") if exp_body else None
            response = None
            if exp_id:
                response = await client.post(
                    f"/api/research/experiments/{exp_id}/external-report",
                    json={"configuration_id": exp_body["configurations"][0]["id"], "provenance": provenance, "metrics": [{"metric_key": "decode_tps", "value": 1.0, "meta": {"fixture": "synthetic"}}]},
                )
            source_free_results.append({
                "provenance": provenance,
                "experiment_status": exp_response.status_code,
                "report_status": response.status_code if response else None,
                "execution_origin": response.json().get("execution_origin") if response and response.status_code == 201 else None,
                "provenance_saved": response.json().get("provenance") if response and response.status_code == 201 else None,
            })
        result["checks"]["source_conflict_matrix"] = source_results
        result["checks"]["source_free_manual_import"] = source_free_results

        # C: actual local retrieval_eval A/B. The preset is a product fixture dataset,
        # so values are local measured adapter outputs, not production or general evidence.
        exp_response, experiment = await request_json(
            client,
            "POST",
            f"/api/research/studies/{study_id}/experiments",
            json={
                "title": "C baseline: lexical_overlap vs BM25 at k=20",
                "hypothesis": "BM25 may improve MRR/nDCG; recall can remain unchanged.",
                "rationale": "Preserve a no-improvement primary metric and compare secondary retrieval metrics.",
                "adapter_id": "retrieval_eval",
                "strategy": "ab",
                "search_space": {"retriever_mode": ["lexical_overlap", "bm25"]},
                "fixed_inputs": {"preset_id": "ko_tech_docs_v1", "k": 20},
                "primary_metric": "recall_at_20",
                "secondary_metrics": ["mrr_at_20", "ndcg_at_20"],
                "budget": {"max_runs": 2, "max_total_seconds": 60},
            },
        )
        if exp_response.status_code != 201:
            result["checks"]["rag_experiment_create"] = {"status": exp_response.status_code, "body": exp_response.text[:1000]}
            return result
        exp = experiment["experiment"]
        experiment_id = exp["id"]
        result["rag_experiment_id"] = experiment_id
        configs = experiment["configurations"]
        result["checks"]["rag_configurations"] = [
            {"id": c["id"], "label": c["label"], "role": c["role"], "parameters": c["parameters"]}
            for c in configs
        ]
        run_response, run_body = await request_json(
            client, "POST", f"/api/research/experiments/{experiment_id}/run", json={}
        )
        result["checks"]["rag_run_dispatch"] = {"status": run_response.status_code, "body": run_body or run_response.text[:500]}
        run_ids = [r["id"] for r in (run_body or {}).get("runs", [])]
        details: list[dict[str, Any]] = []
        for _ in range(60):
            details = []
            terminal = True
            for run_id in run_ids:
                response = await client.get(f"/api/research/runs/{run_id}")
                if response.status_code != 200:
                    terminal = False
                    continue
                data = response.json()
                details.append(data)
                if data.get("status") not in {"succeeded", "failed", "cancelled"}:
                    terminal = False
            if terminal and len(details) == len(run_ids):
                break
            await asyncio.sleep(0.25)

        result["checks"]["rag_runs"] = []
        async with sessions() as db:
            for detail in details:
                run_id = detail["id"]
                run = await db.get(Run, run_id)
                cfg = await db.get(Configuration, run.configuration_id) if run else None
                measurements = (
                    (await db.scalars(select(Measurement).where(Measurement.run_id == run_id).order_by(Measurement.metric_key))).all()
                    if run
                    else []
                )
                result["checks"]["rag_runs"].append(
                    {
                        "run_id": run_id,
                        "status": run.status if run else detail.get("status"),
                        "execution_origin": run.execution_origin if run else None,
                        "configuration_id": str(cfg.id) if cfg else None,
                        "configuration": cfg.parameters if cfg else None,
                        "raw_output": run.raw_output if run else None,
                        "metrics": [
                            {"metric_key": m.metric_key, "value": m.value, "unit": m.unit}
                            for m in measurements
                        ],
                    }
                )
        compare = await client.get(
            f"/api/research/experiments/{experiment_id}/compare-series",
            params={"baseline_config_id": experiment.get("baseline_configuration_id") or configs[0]["id"]},
        )
        if compare.status_code == 200:
            body = compare.json()
            result["checks"]["rag_comparison"] = {
                "status": compare.status_code,
                "primary_metric": body.get("primary_metric"),
                "delta_bars": body.get("delta_bars"),
                "metric_bars": body.get("metric_bars"),
            }
        else:
            result["checks"]["rag_comparison"] = {"status": compare.status_code, "body": compare.text[:500]}
        resume = await client.get(f"/api/research/studies/{study_id}/resume")
        result["checks"]["c_saved_reload"] = {
            "status": resume.status_code,
            "study_id": resume.json().get("study", {}).get("id") if resume.status_code == 200 else None,
            "experiment_count": len(resume.json().get("experiments", [])) if resume.status_code == 200 else None,
        }
        # The next experiment may be placed under the same study, but a typed
        # structural reference to the previous experiment/run/config is not part
        # of the current request contract. Probe such references explicitly.
        reuse_attempt = await client.post(
            f"/api/research/studies/{study_id}/experiments",
            json={
                "title": "C follow-up referencing prior result",
                "adapter_id": "retrieval_eval",
                "strategy": "single",
                "fixed_inputs": {"preset_id": "ko_tech_docs_v1", "k": 20},
                "source_study_id": study_id,
                "source_experiment_id": experiment_id,
                "source_run_id": run_ids[0] if run_ids else None,
                "source_configuration_id": configs[0]["id"],
            },
        )
        result["checks"]["c_structural_reuse_attempt"] = {
            "status": reuse_attempt.status_code,
            "rejection": reuse_attempt.json() if reuse_attempt.status_code >= 400 else None,
        }

        # D: register a genuinely unsupported idea as a hypothesis, keep it unverified,
        # and use only a clearly synthetic manual evaluation result.
        slug = f"independent-memory-layout-{study_id[:8]}"
        method_response, method = await request_json(
            client,
            "POST",
            "/api/research/methods?scope=personal",
            json={
                "slug": slug,
                "name": "GPU memory access layout hypothesis",
                "name_ko": "GPU 메모리 접근 레이아웃 가설",
                "summary": "Change memory access layout without changing weights.",
                "principle": "Hypothesis only; this Research Lab adapter does not implement this kernel intervention.",
                "targets": ["llm"],
                "layers": ["gpu_kernel"],
                "goals": ["latency_ttft"],
                "interventions": ["algorithm_swap"],
                "research_methods": ["baseline"],
                "parameters": [],
                "metrics": ["decode_tps"],
                "evidence": [],
                "evidence_status": "unverified",
                "adapter_hints": [],
                "lifecycle_status": "hypothesis",
                "tags": ["synthetic acceptance probe"],
            },
        )
        result["checks"]["d_hypothesis"] = {"status": method_response.status_code, "method": method}
        if method_response.status_code != 201:
            result["checks"]["d_followup"] = {"status": "not_run", "reason": "hypothesis registration failed"}
        else:
            method_id = method["id"]
            unsupported_response, unsupported = await request_json(
                client,
                "POST",
                f"/api/research/studies/{study_id}/experiments",
                json={
                    "title": "D unsupported local execution status",
                    "hypothesis": "This kernel idea is not executable by current adapters.",
                    "method_id": method_id,
                    "adapter_id": "llm_inference",
                    "strategy": "single",
                    "fixed_inputs": {"model": "qwen"},
                    "primary_metric": "decode_tps",
                    "budget": {"max_runs": 1, "max_total_seconds": 60},
                },
            )
            unsupported_id = unsupported.get("experiment", {}).get("id") if unsupported_response.status_code == 201 else None
            rejected = None
            if unsupported_id:
                rejected = await client.post(f"/api/research/experiments/{unsupported_id}/run", json={})
            result["checks"]["d_unsupported_execution"] = {
                "experiment_create": unsupported_response.status_code,
                "run_attempt": rejected.status_code if rejected else None,
                "run_rejection": rejected.json() if rejected and rejected.status_code >= 400 else None,
            }

            ext_response, external = await request_json(
                client,
                "POST",
                f"/api/research/studies/{study_id}/experiments",
                json={
                    "title": "D synthetic manual report",
                    "hypothesis": "Synthetic manual measurement only; no external source claim.",
                    "method_id": method_id,
                    "adapter_id": "external_report",
                    "strategy": "single",
                    "fixed_inputs": {},
                    "primary_metric": "decode_tps",
                    "budget": {"max_runs": 1, "max_total_seconds": 60},
                },
            )
            ext_experiment_id = external.get("experiment", {}).get("id") if ext_response.status_code == 201 else None
            external_run = None
            if ext_experiment_id:
                cfg_id = external["configurations"][0]["id"]
                report_response, external_run = await request_json(
                    client,
                    "POST",
                    f"/api/research/experiments/{ext_experiment_id}/external-report",
                    json={
                        "configuration_id": cfg_id,
                        "provenance": "manual_evaluation",
                        "environment_snapshot": {"fixture": "synthetic", "device": "not measured"},
                        "raw_output": {"note": "Synthetic acceptance payload, not a GPU or external benchmark."},
                        "metrics": [{"metric_key": "decode_tps", "metric_label": "decode_tps", "value": 1.0, "unit": "tok/s", "direction": "higher_is_better", "meta": {"fixture": "synthetic"}}],
                    },
                )
            else:
                report_response = None
            promoted = None
            if external_run and report_response and report_response.status_code == 201:
                promoted_response = await client.post(
                    f"/api/research/methods/{method_id}/promote",
                    json={"run_id": external_run["id"], "decision_note": "Synthetic manual fixture; retained as candidate, not validation.", "target_status": "candidate"},
                )
                promoted = {"status": promoted_response.status_code, "body": promoted_response.json()}
            reloaded_method_response = await client.get(f"/api/research/methods/{method_id}?scope=personal")
            catalog_response = await client.get("/api/research/methods", params={"q": slug, "scope": "personal", "limit": 20})
            follow_response, follow = await request_json(
                client,
                "POST",
                f"/api/research/studies/{study_id}/experiments",
                json={
                    "title": "D follow-up references candidate method",
                    "hypothesis": "Follow-up reuses the registered method by ID.",
                    "method_id": method_id,
                    "adapter_id": "external_report",
                    "strategy": "single",
                    "fixed_inputs": {},
                    "primary_metric": "decode_tps",
                    "budget": {"max_runs": 1, "max_total_seconds": 60},
                },
            )
            follow_id = follow.get("experiment", {}).get("id") if follow_response.status_code == 201 else None
            follow_reload = await client.get(f"/api/research/experiments/{follow_id}") if follow_id else None
            result["checks"]["d_manual_report_candidate_reload_catalog_followup"] = {
                "external_experiment_status": ext_response.status_code,
                "manual_report_status": report_response.status_code if report_response else None,
                "manual_run": external_run,
                "promotion": promoted,
                "method_reload_status": reloaded_method_response.status_code,
                "method_lifecycle_after_reload": reloaded_method_response.json().get("lifecycle_status") if reloaded_method_response.status_code == 200 else None,
                "catalog_search_status": catalog_response.status_code,
                "catalog_method_ids": [m.get("id") for m in catalog_response.json().get("items", [])] if catalog_response.status_code == 200 else [],
                "followup_create_status": follow_response.status_code,
                "followup_experiment_id": follow_id,
                "followup_method_id": follow_reload.json().get("experiment", {}).get("method_id") if follow_reload and follow_reload.status_code == 200 else None,
                "followup_reload_status": follow_reload.status_code if follow_reload else None,
            }

    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str) + "\n")
    await engine.dispose()
    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:18793")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = asyncio.run(run_probe(args.base_url, args.output))
    print(json.dumps({"output": str(args.output), "study_id": result.get("study_id"), "checks": result.get("checks", {})}, ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    main()
