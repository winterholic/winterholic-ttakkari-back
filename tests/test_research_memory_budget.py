"""Real Run DB probes with model-free children; no ML or host GPU calls."""
from __future__ import annotations

import pytest
from test_research_runner_lifecycle import ProbeAdapter, seed_runs

from ttakkari import model_process
from ttakkari.db import sessionmaker
from ttakkari.research import runner
from ttakkari.research.adapters.base import AdapterRunResult
from ttakkari.research.models import Configuration, Experiment, Run


async def budget_fixture(*, memory=1, adapter_id="tts_synthesis", mode="synthesize", count=2):
    exp_id, ids = await seed_runs(count)
    async with sessionmaker()() as db:
        exp = await db.get(Experiment, exp_id)
        exp.budget = {"max_memory_mb": memory}
        exp.adapter_id = adapter_id
        for rid in ids:
            row = await db.get(Run, rid)
            row.adapter_id = adapter_id
            (await db.get(Configuration, row.configuration_id)).parameters = {"mode": mode, "texts": ["시험"]}
        await db.commit()
    return exp_id, ids


@pytest.mark.parametrize("adapter_id,mode,memory", [
    ("retrieval_eval", "synthesize", 128), ("llm_inference", "synthesize", 128),
    ("tts_synthesis", "normalize_only", 128), ("tts_synthesis", "synthesize", True),
    ("tts_synthesis", "synthesize", 0), ("tts_synthesis", "synthesize", "128"),
])
async def test_persisted_unsupported_or_invalid_memory_budget_never_dispatches(client, adapter_id, mode, memory):
    exp_id, ids = await budget_fixture(memory=memory, adapter_id=adapter_id, mode=mode)
    adapter = ProbeAdapter()
    adapter.release.set()
    await runner.run_experiment_batch(exp_id, ids, adapter)
    async with sessionmaker()() as db:
        rows = [await db.get(Run, rid) for rid in ids]
        assert all(r.status == "cancelled" for r in rows)
        assert rows[0].error_code == "budget_invalid"
        assert all(r.raw_output == {} for r in rows)
    assert adapter.calls == 0


@pytest.mark.parametrize("swallow", [False, True])
async def test_memory_guard_survives_swallowed_error_and_cancels_remaining_batch(client, auth, tmp_path, swallow):
    exp_id, ids = await budget_fixture()
    executable = tmp_path / "child"
    executable.write_text("#!/usr/bin/env python3\nimport sys,time\nsys.stdin.read()\ntime.sleep(5)\n")
    executable.chmod(0o755)
    adapter = ProbeAdapter()

    async def execute(**kwargs):
        adapter.calls += 1
        try:
            await model_process.run_json_worker(module="ttakkari.tts.worker_supertonic", request={}, timeout=5, python=executable)
        except model_process.WorkerMemoryLimitError:
            if not swallow:
                raise
        return AdapterRunResult(status="succeeded", raw_output={"partial": "preserved"},
                                environment_snapshot={"research_memory_guard": {"fake": True}})

    adapter.execute_run = execute
    await runner.run_experiment_batch(exp_id, ids, adapter)
    assert adapter.calls == 1
    async with sessionmaker()() as db:
        first, second = [await db.get(Run, rid) for rid in ids]
        assert first.status == second.status == "cancelled"
        assert first.error_code == "memory_budget_exceeded"
        assert first.environment_snapshot["research_memory_guard"]["sampled_peak_rss_bytes"] > 1048576
        assert "fake" not in first.environment_snapshot["research_memory_guard"]
        assert first.environment_snapshot["research_model_workers"]["state"] == "released"
        assert second.started_at is None
        if swallow:
            assert first.raw_output == {"partial": "preserved"}
    response = await client.get(f"/api/research/runs/{ids[0]}", headers=auth)
    assert response.status_code == 200
    assert response.json()["environment_snapshot"]["research_memory_guard"]["error_code"] == "memory_budget_exceeded"


@pytest.mark.parametrize("adapter_id,mode", [("llm_inference", "synthesize"), ("tts_synthesis", "normalize_only")])
async def test_validation_reports_unsupported_memory_scope(client, auth, adapter_id, mode):
    exp_id, _ = await budget_fixture()
    async with sessionmaker()() as db:
        study_id = (await db.get(Experiment, exp_id)).study_id
    response = await client.post("/api/research/experiments/validate", headers=auth, json={
        "study_id": str(study_id), "adapter_id": adapter_id, "strategy": "single",
        "fixed_inputs": {"mode": mode, "texts": ["시험"]}, "budget": {"max_runs": 1, "max_memory_mb": 128},
    })
    assert response.status_code == 200
    assert response.json()["valid"] is False
    assert any("max_memory_mb" in message for message in response.json()["errors"])


async def test_success_without_any_model_child_cannot_claim_memory_monitoring(client):
    exp_id, ids = await budget_fixture(memory=128)
    adapter = ProbeAdapter()
    adapter.release.set()
    await runner.run_experiment_batch(exp_id, ids, adapter)
    async with sessionmaker()() as db:
        row = await db.get(Run, ids[0])
        assert row.status == "cancelled"
        assert row.error_code == "memory_probe_unavailable"
        assert row.environment_snapshot["research_memory_guard"]["samples"] == 0
    assert adapter.calls == 1


async def test_validation_advertises_budget_contract_for_deployment_skew(client, auth):
    exp_id, _ = await budget_fixture()
    async with sessionmaker()() as db:
        study_id = (await db.get(Experiment, exp_id)).study_id
    response = await client.post('/api/research/experiments/validate', headers=auth, json={
        'study_id': str(study_id), 'adapter_id': 'tts_synthesis', 'strategy': 'single',
        'fixed_inputs': {'mode': 'synthesize', 'texts': ['시험']},
        'budget': {'max_runs': 1, 'max_memory_mb': 128},
    })
    assert response.status_code == 200
    assert response.json()['valid'] is True
    assert response.json()['resource_estimate']['memory_budget_contract'] == 1
