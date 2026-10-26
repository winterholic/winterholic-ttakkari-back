"""Real PostgreSQL probes for cancellation and duplicate Research Run claims."""
from __future__ import annotations

import asyncio
import os
import time
import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url

from ttakkari.db import sessionmaker
from ttakkari.research import runner
from ttakkari.research.adapters.base import AdapterRunResult
from ttakkari.research.models import Configuration, Experiment, Run, Study


async def seed_runs(count=1):
    async with sessionmaker()() as db:
        identity = (await db.execute(text("select current_database(), current_user"))).one()
        expected = make_url(os.environ["TTAKKARI_TEST_DB"])
        assert identity[0] == expected.database != "ttakkari"
        if expected.username:
            assert identity[1] == expected.username
        study = Study(title="Runner lifecycle", goal="Cancellation and duplicate dispatch", scope="personal")
        db.add(study)
        await db.flush()
        exp = Experiment(study_id=study.id, title="Lifecycle", hypothesis="No replay", rationale="Probe",
                         adapter="probe", adapter_id="probe", state="queued")
        db.add(exp)
        await db.flush()
        cfg = Configuration(experiment_id=exp.id, label="one", config_hash=uuid.uuid4().hex, parameters={})
        db.add(cfg)
        await db.flush()
        runs = [Run(experiment_id=exp.id, study_id=study.id, configuration_id=cfg.id,
                    adapter_id="probe", status="queued", repeat_index=i + 1) for i in range(count)]
        db.add_all(runs)
        await db.commit()
        return exp.id, [run.id for run in runs]


class ProbeAdapter:
    def __init__(self):
        self.calls = 0
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    def get_adapter(self, adapter_id):
        return self

    async def execute_run(self, **kwargs):
        self.calls += 1
        self.entered.set()
        await self.release.wait()
        return AdapterRunResult(status="succeeded", raw_output={"probe": "completed"},
                                environment_snapshot={"execution": "synthetic_adapter"})

    def to_measurements(self, result, config):
        return []


async def test_batch_time_budget_stops_adapter_and_unstarted_runs(client):
    exp_id, ids = await seed_runs(2)
    async with sessionmaker()() as db:
        (await db.get(Experiment, exp_id)).budget = {"max_total_seconds": 0.05}
        await db.commit()
    adapter = ProbeAdapter()
    # Deliberately never release it: the execution budget must cancel the wait.
    await asyncio.wait_for(runner.run_experiment_batch(exp_id, ids, adapter), 0.8)
    async with sessionmaker()() as db:
        rows = [await db.get(Run, rid) for rid in ids]
        assert [row.status for row in rows] == ["cancelled", "cancelled"]
        assert all(row.error_code == "budget_exceeded" for row in rows)
        assert all(row.finished_at is not None for row in rows)
        assert (await db.get(Experiment, exp_id)).state == "cancelled"
    assert adapter.calls == 1
    assert not any(rid in runner._CANCEL_EVENTS for rid in ids)


async def test_batch_budget_is_shared_and_preserves_finished_run(client, monkeypatch):
    exp_id, ids = await seed_runs(2)
    async with sessionmaker()() as db:
        (await db.get(Experiment, exp_id)).budget = {"max_total_seconds": 0.2}
        await db.commit()
    adapter = ProbeAdapter()
    adapter.release.set()
    original = runner.execute_run_instance

    async def finish_then_delay(*args, **kwargs):
        result = await original(*args, **kwargs)
        # Simulate time spent between runs, after the first evidence was saved.
        await asyncio.sleep(0.25)
        return result

    monkeypatch.setattr(runner, "execute_run_instance", finish_then_delay)
    await runner.run_experiment_batch(exp_id, ids, adapter)
    async with sessionmaker()() as db:
        first, second = [await db.get(Run, rid) for rid in ids]
        assert first.status == "succeeded"
        assert first.raw_output == {"probe": "completed"}
        assert second.status == "cancelled"
        assert second.error_code == "budget_exceeded"
        assert second.started_at is None
        assert (await db.get(Experiment, exp_id)).state == "cancelled"
    assert adapter.calls == 1


async def test_budget_marks_blocking_adapter_late_result_without_claiming_preemption(client):
    exp_id, ids = await seed_runs(2)
    async with sessionmaker()() as db:
        (await db.get(Experiment, exp_id)).budget = {"max_total_seconds": 0.05}
        await db.commit()
    adapter = ProbeAdapter()

    async def blocks_event_loop(**kwargs):
        adapter.calls += 1
        time.sleep(0.08)  # noqa: ASYNC251 - deliberate non-preemptible native-call probe
        return AdapterRunResult(status="succeeded", raw_output={"late": True}, environment_snapshot={})

    adapter.execute_run = blocks_event_loop
    await runner.run_experiment_batch(exp_id, ids, adapter)
    async with sessionmaker()() as db:
        first, second = [await db.get(Run, rid) for rid in ids]
        assert first.status == second.status == "cancelled"
        assert first.error_code == second.error_code == "budget_exceeded"
        assert first.raw_output == {"late": True}
    assert adapter.calls == 1


@pytest.mark.parametrize("seconds", [0, -1, True, "30", None, 3601, float("nan"), float("inf")])
async def test_invalid_persisted_time_budget_fails_closed(client, seconds):
    exp_id, ids = await seed_runs()
    async with sessionmaker()() as db:
        (await db.get(Experiment, exp_id)).budget = {"max_total_seconds": seconds}
        # PostgreSQL JSON rejects NaN and infinity: test those at the shared parser boundary.
        if isinstance(seconds, float):
            from ttakkari.research.planner import execution_budget_seconds
            with pytest.raises(ValueError, match="max_total_seconds"):
                execution_budget_seconds({"max_total_seconds": seconds})
            return
        await db.commit()
    adapter = ProbeAdapter()
    adapter.release.set()
    await runner.run_experiment_batch(exp_id, ids, adapter)
    async with sessionmaker()() as db:
        row = await db.get(Run, ids[0])
        assert row.status == "cancelled"
        assert row.error_code == "budget_invalid"
        assert (await db.get(Experiment, exp_id)).state == "cancelled"
    assert adapter.calls == 0


@pytest.mark.parametrize("seconds", [0, -1, True, "30", None, 3601, 10**400])
async def test_plan_validation_rejects_invalid_time_budget(client, auth, seconds):
    exp_id, _ = await seed_runs()
    async with sessionmaker()() as db:
        study_id = (await db.get(Experiment, exp_id)).study_id
    response = await client.post("/api/research/experiments/validate", headers=auth, json={
        "study_id": str(study_id), "budget": {"max_total_seconds": seconds},
    })
    assert response.status_code == 200
    assert response.json()["valid"] is False
    assert any("max_total_seconds" in message for message in response.json()["errors"])


async def test_budget_expiry_preserves_adapter_cleanup_output(client):
    exp_id, ids = await seed_runs(2)
    async with sessionmaker()() as db:
        (await db.get(Experiment, exp_id)).budget = {"max_total_seconds": 0.05}
        await db.commit()
    adapter = ProbeAdapter()

    async def cleans_up_after_cancel(**kwargs):
        adapter.calls += 1
        if adapter.calls > 1:
            return AdapterRunResult(status="succeeded", raw_output={}, environment_snapshot={})
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            return AdapterRunResult(status="succeeded", raw_output={"cleanup": "done"},
                                    environment_snapshot={"execution": "synthetic_adapter"})

    adapter.execute_run = cleans_up_after_cancel
    await asyncio.wait_for(runner.run_experiment_batch(exp_id, ids, adapter), 0.8)
    async with sessionmaker()() as db:
        rows = [await db.get(Run, rid) for rid in ids]
        assert [row.status for row in rows] == ["cancelled", "cancelled"]
        assert all(row.error_code == "budget_exceeded" for row in rows)
        assert rows[0].raw_output == {"cleanup": "done"}
        assert rows[0].environment_snapshot == {"execution": "synthetic_adapter",
            "research_execution_owner": rows[0].environment_snapshot["research_execution_owner"]}
    assert uuid.UUID(rows[0].environment_snapshot["research_execution_owner"]["token"])
    assert adapter.calls == 1


@pytest.mark.parametrize("terminal_status", ["cancelled", "failed", "succeeded"])
async def test_terminal_run_is_not_replayed(client, terminal_status):
    _, ids = await seed_runs()
    adapter = ProbeAdapter()
    adapter.release.set()
    async with sessionmaker()() as db:
        row = await db.get(Run, ids[0])
        row.status = terminal_status
        row.raw_output = {"original": True}
        await db.commit()
    result = await runner.execute_run_instance(ids[0], adapter)
    assert result.status == terminal_status
    assert result.raw_output == {"original": True}
    assert adapter.calls == 0
    assert ids[0] not in runner._CANCEL_EVENTS


async def test_duplicate_dispatch_does_not_replace_live_cancellation(client):
    _, ids = await seed_runs()
    adapter = ProbeAdapter()
    first = asyncio.create_task(runner.execute_run_instance(ids[0], adapter))
    second = None
    try:
        await asyncio.wait_for(adapter.entered.wait(), 2)
        original_event = runner._CANCEL_EVENTS[ids[0]]
        second = asyncio.create_task(runner.execute_run_instance(ids[0], adapter))
        # A duplicate must return the existing running row without entering the adapter.
        duplicate = await asyncio.wait_for(asyncio.shield(second), 2)
        assert duplicate.status == "running"
        assert adapter.calls == 1
        assert runner._CANCEL_EVENTS[ids[0]] is original_event
    finally:
        adapter.release.set()
        await asyncio.gather(first, *([second] if second else []), return_exceptions=True)
    assert ids[0] not in runner._CANCEL_EVENTS


async def test_cancelled_batch_closes_unstarted_runs(client):
    exp_id, ids = await seed_runs(2)
    adapter = ProbeAdapter()
    # A cooperative adapter reports cancellation after the user signals it.
    async def execute_cancelled(**kwargs):
        adapter.calls += 1
        await runner.cancel_run(kwargs["run_id"], "User stop")
        return AdapterRunResult(status="cancelled", raw_output={"stopped": True}, environment_snapshot={})
    adapter.execute_run = execute_cancelled
    await runner.run_experiment_batch(exp_id, ids, adapter)
    async with sessionmaker()() as db:
        rows = [await db.get(Run, rid) for rid in ids]
        assert [row.status for row in rows] == ["cancelled", "cancelled"]
        assert all(row.finished_at is not None for row in rows)
        assert rows[1].error_code == "batch_cancelled"
        assert (await db.get(Experiment, exp_id)).state == "cancelled"
    assert adapter.calls == 1


async def test_cancel_request_survives_late_success_with_raw_evidence(client):
    _, ids = await seed_runs()
    adapter = ProbeAdapter()
    task = asyncio.create_task(runner.execute_run_instance(ids[0], adapter))
    try:
        await asyncio.wait_for(adapter.entered.wait(), 2)
        assert await runner.cancel_run(ids[0], "Stop requested by researcher")
    finally:
        adapter.release.set()
        result = await task
    assert result.status == "cancelled"
    assert result.error_code == "cancelled"
    assert result.error_message == "Stop requested by researcher"
    assert result.raw_output == {"probe": "completed"}
    assert result.environment_snapshot == {"execution": "synthetic_adapter",
            "research_execution_owner": result.environment_snapshot["research_execution_owner"]}
    assert uuid.UUID(result.environment_snapshot["research_execution_owner"]["token"])
    async with sessionmaker()() as db:
        persisted = await db.get(Run, ids[0])
        assert persisted.status == "cancelled"
        assert persisted.raw_output == result.raw_output


async def test_simultaneous_claims_execute_adapter_once(client):
    _, ids = await seed_runs()
    adapter = ProbeAdapter()
    tasks = [asyncio.create_task(runner.execute_run_instance(ids[0], adapter)) for _ in range(4)]
    try:
        await asyncio.wait_for(adapter.entered.wait(), 2)
        # The three losing claims must finish while the adapter still waits.
        done, pending = await asyncio.wait(tasks, timeout=2, return_when=asyncio.FIRST_COMPLETED)
        assert done
        await asyncio.wait_for(asyncio.gather(*(task for task in tasks if task not in pending)), 2)
        assert adapter.calls == 1
    finally:
        adapter.release.set()
        results = await asyncio.gather(*tasks, return_exceptions=True)
    assert all(isinstance(result, Run) for result in results)
    assert adapter.calls == 1


async def test_cancel_during_artifact_copy_preserves_checkpoint_and_removes_copy(client, monkeypatch, tmp_path):
    from pathlib import Path

    from sqlalchemy import select

    from ttakkari.artifacts import service as artifacts
    from ttakkari.research.adapters.base import MeasurementRecord
    from ttakkari.research.models import Measurement, ResearchArtifact

    _, ids = await seed_runs()
    original = tmp_path / "original.txt"
    original.write_text("keep original", encoding="utf-8")
    adapter = ProbeAdapter()
    adapter.release.set()
    async def execute(**kwargs):
        return AdapterRunResult(status="succeeded", raw_output={"observed": True},
                                environment_snapshot={"execution": "synthetic_adapter"},
                                artifacts=[{"path": str(original), "role": "probe"}])
    adapter.execute_run = execute
    adapter.to_measurements = lambda *_: [MeasurementRecord(
        metric_key="probe", metric_label="Probe", value=1, unit="count", direction="higher_is_better")]
    register = artifacts.register_file
    copies = []
    async def register_then_cancel(*args, **kwargs):
        result = await register(*args, **kwargs)
        copies.append(Path(result.stored_path))
        assert copies[-1].is_file()
        assert await runner.cancel_run(ids[0], "Stop during artifact copy")
        return result
    monkeypatch.setattr(artifacts, "register_file", register_then_cancel)
    result = await runner.execute_run_instance(ids[0], adapter)
    assert result.status == "cancelled"
    assert result.error_message == "Stop during artifact copy"
    assert result.raw_output == {"observed": True}
    assert result.environment_snapshot == {"execution": "synthetic_adapter",
            "research_execution_owner": result.environment_snapshot["research_execution_owner"]}
    assert uuid.UUID(result.environment_snapshot["research_execution_owner"]["token"])
    assert original.read_text(encoding="utf-8") == "keep original"
    assert copies and all(not path.exists() for path in copies)
    async with sessionmaker()() as db:
        assert len((await db.scalars(select(Measurement).where(Measurement.run_id == ids[0]))).all()) == 1
        assert not (await db.scalars(select(ResearchArtifact).where(ResearchArtifact.run_id == ids[0]))).all()


@pytest.mark.parametrize("cancel_first", [False, True])
async def test_adapter_exception_preserves_accepted_cancellation(client, cancel_first):
    _, ids = await seed_runs()
    adapter = ProbeAdapter()

    async def fail_after_stop(**kwargs):
        if cancel_first:
            assert await runner.cancel_run(kwargs["run_id"], "Researcher stopped this run")
        raise RuntimeError("synthetic adapter exception")

    adapter.execute_run = fail_after_stop
    result = await runner.execute_run_instance(ids[0], adapter)
    assert result.status == ("cancelled" if cancel_first else "failed")
    assert result.error_code == ("cancelled" if cancel_first else "RuntimeError")
    assert "synthetic adapter exception" in result.error_message
    if cancel_first:
        assert "Researcher stopped this run" in result.error_message
    async with sessionmaker()() as db:
        saved = await db.get(Run, ids[0])
        assert (saved.status, saved.error_code, saved.error_message) == (
            result.status, result.error_code, result.error_message)


async def test_duplicate_batch_does_not_advance_or_finalize_live_owner(client):
    from sqlalchemy import select

    from ttakkari.research.models import Analysis

    exp_id, ids = await seed_runs(2)
    adapter = ProbeAdapter()
    first = asyncio.create_task(runner.run_experiment_batch(exp_id, ids, adapter))
    duplicate = None
    try:
        await asyncio.wait_for(adapter.entered.wait(), 2)
        duplicate = asyncio.create_task(runner.run_experiment_batch(exp_id, ids, adapter))
        await asyncio.wait_for(asyncio.shield(duplicate), 2)
        async with sessionmaker()() as db:
            exp = await db.get(Experiment, exp_id)
            assert exp.state == "running" and exp.finished_at is None
            assert (await db.get(Run, ids[1])).status == "queued"
            assert not (await db.scalars(select(Analysis).where(Analysis.experiment_id == exp_id))).all()
        assert adapter.calls == 1
    finally:
        adapter.release.set()
        await asyncio.gather(first, *([duplicate] if duplicate else []), return_exceptions=True)
    assert adapter.calls == 2
    async with sessionmaker()() as db:
        exp = await db.get(Experiment, exp_id)
        assert exp.state == "completed" and exp.finished_at is not None
        assert len((await db.scalars(select(Analysis).where(Analysis.experiment_id == exp_id))).all()) == 1


@pytest.mark.parametrize("sibling_status", ["queued", "running"])
async def test_partial_batch_does_not_finalize_pending_sibling(client, sibling_status):
    from sqlalchemy import select

    from ttakkari.research.models import Analysis

    exp_id, ids = await seed_runs(2)
    async with sessionmaker()() as db:
        sibling = await db.get(Run, ids[1])
        sibling.status = sibling_status
        await db.commit()
    adapter = ProbeAdapter()
    adapter.release.set()
    await runner.run_experiment_batch(exp_id, ids[:1], adapter)
    async with sessionmaker()() as db:
        exp = await db.get(Experiment, exp_id)
        assert exp.state == "running" and exp.finished_at is None
        assert (await db.get(Run, ids[1])).status == sibling_status
        assert not (await db.scalars(select(Analysis).where(Analysis.experiment_id == exp_id))).all()


@pytest.mark.parametrize("gate_allowed", [False, True])
async def test_completed_batch_replay_preserves_terminal_receipt_and_analysis(client, monkeypatch, gate_allowed):
    from contextlib import asynccontextmanager

    from sqlalchemy import select

    from ttakkari.research.models import Analysis

    exp_id, ids = await seed_runs()
    adapter = ProbeAdapter()
    adapter.release.set()
    await runner.run_experiment_batch(exp_id, ids, adapter)
    async with sessionmaker()() as db:
        exp = await db.get(Experiment, exp_id)
        exp.receipt = {"original_result": "must remain"}
        finished = exp.finished_at
        analysis = await db.scalar(select(Analysis).where(Analysis.experiment_id == exp_id))
        analyzed = analysis.created_at
        await db.commit()

    @asynccontextmanager
    async def gate(*args, **kwargs):
        yield gate_allowed

    monkeypatch.setattr(runner.generation, "enter_accepted_work", gate)
    await runner.run_experiment_batch(exp_id, ids, adapter)
    async with sessionmaker()() as db:
        exp = await db.get(Experiment, exp_id)
        assert (exp.state, exp.finished_at, exp.receipt) == (
            "completed", finished, {"original_result": "must remain"})
        analysis = await db.scalar(select(Analysis).where(Analysis.experiment_id == exp_id))
        assert analysis.created_at == analyzed
    assert adapter.calls == 1


async def test_running_research_owner_is_durable_and_live_owner_is_not_recovered(client):
    exp_id, ids = await seed_runs(2)
    adapter = ProbeAdapter()
    task = asyncio.create_task(runner.run_experiment_batch(exp_id, ids, adapter))
    try:
        await asyncio.wait_for(adapter.entered.wait(), 1)
        async with sessionmaker()() as db:
            first, second = [await db.get(Run, rid) for rid in ids]
            owner = first.environment_snapshot.get("research_execution_owner")
            assert owner and uuid.UUID(owner["token"])
            assert owner["generation_id"] is None
            assert second.environment_snapshot["research_execution_owner"] == owner
        assert await runner.recover_orphaned_runs() == 0
        async with sessionmaker()() as db:
            assert (await db.get(Run, ids[0])).status == "running"
            assert (await db.get(Run, ids[1])).status == "queued"
    finally:
        adapter.release.set()
        await task
    async with sessionmaker()() as db:
        first = await db.get(Run, ids[0])
        assert first.status == "succeeded"
        assert first.environment_snapshot["research_execution_owner"] == owner
        assert first.environment_snapshot["execution"] == "synthetic_adapter"


async def test_owner_process_exit_recovers_checkpoint_without_reexecution(client, auth, tmp_path, monkeypatch):
    import signal
    import subprocess
    import sys

    from ttakkari.config import get_settings
    from ttakkari.research.adapters.llm_inference import LlmInferenceAdapter
    from ttakkari.research.models import Measurement
    from ttakkari.research.resources import RESOURCE_KEY

    exp_id, ids = await seed_runs(3)
    token = uuid.uuid4()
    owner = {"token": str(token), "generation_id": None, "contract": 1}
    lock_path = get_settings().home / "runtime" / "research" / f"{token}.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    script = (
        "import fcntl,sys; f=open(sys.argv[1],'a'); "
        "fcntl.flock(f,fcntl.LOCK_EX|fcntl.LOCK_NB); print('locked',flush=True); sys.stdin.read()"
    )
    child = await asyncio.to_thread(subprocess.Popen, [sys.executable, "-c", script, str(lock_path)],
                                    stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    cleanup_calls = []

    def cleanup(host, data_dir, launch):
        assert child.poll() == -signal.SIGKILL
        cleanup_calls.append(launch["launch_id"])
        return {"success": True, "attempted": True, "stopped": True, "launch_id": launch["launch_id"]}

    monkeypatch.setattr(LlmInferenceAdapter, "_cleanup_owned_launch", staticmethod(cleanup))
    launch_id = uuid.uuid4().hex
    try:
        assert await asyncio.to_thread(child.stdout.readline) == "locked\n"
        async with sessionmaker()() as db:
            for i, rid in enumerate(ids):
                row = await db.get(Run, rid)
                row.status = ["succeeded", "running", "queued"][i]
                row.raw_output = {"checkpoint": i}
                row.environment_snapshot = {"research_execution_owner": owner, "device": "synthetic"}
                if i == 1:
                    row.adapter_id = "llm_inference"
                    row.environment_snapshot = {**row.environment_snapshot, RESOURCE_KEY: {
                        "contract": 1, "owner_token": str(token), "kind": "host_gpu", "state": "owned",
                        "launch": {"launch_id": launch_id, "supervisor_pid": 1234, "supervisor_start": 123.5,
                                   "server_pid": 1235, "server_start": 124.5},
                    }}
            db.add(Measurement(run_id=ids[1], experiment_id=exp_id,
                               configuration_id=row.configuration_id, metric_key="saved",
                               metric_label="saved", value=7))
            await db.commit()
        assert await runner.recover_orphaned_runs() == 0
        assert cleanup_calls == []
        child.kill()  # Abrupt owner death: no Python finally/cleanup runs in the child.
        assert await asyncio.to_thread(child.wait, 3) == -signal.SIGKILL
        assert await runner.recover_orphaned_runs() == 2
        assert cleanup_calls == [launch_id]
        async with sessionmaker()() as db:
            rows = [await db.get(Run, rid) for rid in ids]
            assert [r.status for r in rows] == ["succeeded", "failed", "cancelled"]
            assert rows[1].error_code == rows[2].error_code == "owner_lost"
            assert [r.raw_output for r in rows] == [{"checkpoint": i} for i in range(3)]
            assert all(r.environment_snapshot["device"] == "synthetic" for r in rows)
            assert all(r.environment_snapshot["research_execution_owner"] == owner for r in rows)
            assert (await db.get(Experiment, exp_id)).state == "failed"
            saved = (await db.execute(text("select value from research_measurements where run_id=:id"),
                                      {"id": ids[1]})).scalar_one()
            assert saved == 7
            study_id = rows[1].study_id
        restored = await client.get(f"/api/research/runs/{ids[1]}", headers=auth)
        assert restored.status_code == 200
        assert restored.json()["environment_snapshot"]["research_execution_owner"] == owner
        assert restored.json()["raw_output"] == {"checkpoint": 1}
        assert restored.json()["measurements"][0]["value"] == 7
        restored_study = await client.get(f"/api/research/studies/{study_id}/resume", headers=auth)
        assert restored_study.status_code == 200
        interruptions = [event for event in restored_study.json()["timeline"]
                         if event["kind"] == "execution_finished" and event["payload"].get("error_code") == "owner_lost"]
        assert {event["payload"]["run_id"] for event in interruptions} == set(map(str, ids[1:]))
        cleanup_states = {event["payload"]["run_id"]: event["payload"]["external_resource_cleanup"] for event in interruptions}
        assert cleanup_states == {str(ids[1]): "verified", str(ids[2]): "unverified"}
        assert await runner.recover_orphaned_runs() == 0
        adapter = ProbeAdapter()
        adapter.release.set()
        await runner.run_experiment_batch(exp_id, ids, adapter)
        assert adapter.calls == 0
    finally:
        if child.poll() is None:
            child.kill()
            await asyncio.to_thread(child.wait)
        child.stdout.close()
        child.stdin.close()


async def test_recovery_does_not_guess_legacy_or_malformed_ownership(client):
    _, ids = await seed_runs(2)
    async with sessionmaker()() as db:
        (await db.get(Run, ids[0])).status = "running"
        second = await db.get(Run, ids[1])
        second.status = "running"
        second.environment_snapshot = {"research_execution_owner": {"token": "../unknown", "contract": 1}}
        await db.commit()
    assert await runner.recover_orphaned_runs() == 0
    async with sessionmaker()() as db:
        assert [(await db.get(Run, rid)).status for rid in ids] == ["running", "running"]


async def test_reserved_research_owner_blocks_retirement_before_task_enters_gate(client, tmp_path, monkeypatch):
    from ttakkari.generation import GenerationRuntime
    from ttakkari.models import RuntimeGeneration
    from ttakkari.research.ownership import ExecutionOwner

    runtime = GenerationRuntime(tmp_path, revision="a" * 40, api_url="http://127.0.0.1:18791")
    await runtime.register()
    monkeypatch.setattr(runner.generation, "current", runtime)
    async with sessionmaker()() as db:
        (await db.get(RuntimeGeneration, runtime.id)).state = "draining"
        await db.commit()
    owner = ExecutionOwner.acquire()
    try:
        assert owner.metadata["generation_id"] == str(runtime.id)
        assert not await runtime.can_retire()
    finally:
        owner.close()
    try:
        assert await runtime.can_retire()
    finally:
        await runtime.close()


async def test_research_recovery_is_wired_for_remote_api_and_legacy_lifecycles(client, monkeypatch):
    from ttakkari import generation_service
    from ttakkari.config import get_settings

    operations = [op for _, op, _ in generation_service.maintenance_jobs(get_settings())]
    assert runner.recover_orphaned_runs in operations
    calls = []

    async def recovered():
        calls.append("recovered")

    monkeypatch.setattr(runner, "recover_orphaned_runs", recovered)
    from types import SimpleNamespace
    # Research executes in the API even when the agent executor is remote.
    remote = SimpleNamespace(remote=True)
    runtime = SimpleNamespace(draining=False)
    await generation_service.coordinate_runs(runtime, remote, "standby")
    await generation_service.coordinate_runs(runtime, remote, "draining")
    assert calls == []
    await generation_service.coordinate_runs(runtime, remote, "active")
    assert calls == ["recovered"]


async def test_cancel_before_background_task_starts_leaves_recoverable_reservation(client, monkeypatch):
    from types import SimpleNamespace

    from ttakkari.research import planner
    from ttakkari.research.ownership import owner_token, probe

    exp_id, existing = await seed_runs()
    async with sessionmaker()() as db:
        (await db.get(Run, existing[0])).status = "succeeded"
        await db.commit()

    async def valid_plan(*args, **kwargs):
        return SimpleNamespace(valid=True)

    monkeypatch.setattr(planner, "validate_experiment_plan", valid_plan)
    original_create = asyncio.create_task
    tasks = []

    def cancel_before_first_instruction(coro):
        task = original_create(coro)
        if coro.__name__ == "run_experiment_batch":
            task.cancel()
            tasks.append(task)
        return task

    with monkeypatch.context() as m:
        m.setattr(asyncio, "create_task", cancel_before_first_instruction)
        async with sessionmaker()() as db:
            rows = await runner.start_experiment_runs(db, exp_id)
            rid = rows[0].id
            token = owner_token(rows[0].environment_snapshot)
            assert token is not None
            assert probe(token) is None  # held before the background coroutine runs
    await asyncio.gather(*tasks, return_exceptions=True)
    handle = probe(token)
    assert handle is not None
    handle.close()
    assert exp_id not in runner._ACTIVE_TASKS
    assert await runner.recover_orphaned_runs() == 1
    async with sessionmaker()() as db:
        row = await db.get(Run, rid)
        assert row.status == "cancelled" and row.error_code == "owner_lost"
        assert row.started_at is None


async def test_stale_research_owner_cannot_publish_over_changed_token(client):
    _, ids = await seed_runs()
    adapter = ProbeAdapter()
    task = asyncio.create_task(runner.execute_run_instance(ids[0], adapter))
    try:
        await asyncio.wait_for(adapter.entered.wait(), 1)
        replacement = {"contract": 1, "token": str(uuid.uuid4()), "generation_id": None}
        async with sessionmaker()() as db:
            row = await db.get(Run, ids[0])
            row.environment_snapshot = {"research_execution_owner": replacement, "new_owner": True}
            row.raw_output = {"new_checkpoint": True}
            await db.commit()
    finally:
        adapter.release.set()
        await task
    async with sessionmaker()() as db:
        row = await db.get(Run, ids[0])
        assert row.status == "running"
        assert row.environment_snapshot == {"research_execution_owner": replacement, "new_owner": True}
        assert row.raw_output == {"new_checkpoint": True}


async def test_generation_cutover_preserves_research_owner_until_batch_finishes(client, tmp_path, monkeypatch):
    from ttakkari.generation import GenerationRuntime, activate
    from ttakkari.models import RuntimeLease

    a = GenerationRuntime(tmp_path, revision="a" * 40, api_url="http://127.0.0.1:18791")
    b = GenerationRuntime(tmp_path, revision="b" * 40, api_url="http://127.0.0.1:18792")
    await a.register()
    await b.register()
    await activate(a.id, expected=None)
    monkeypatch.setattr(runner.generation, "current", a)
    exp_id, ids = await seed_runs(2)
    adapter = ProbeAdapter()
    task = asyncio.create_task(runner.run_experiment_batch(exp_id, ids, adapter))
    try:
        await asyncio.wait_for(adapter.entered.wait(), 1)
        await activate(b.id, expected=a.id)
        assert await a.refresh() == "draining"
        assert not await a.can_retire()
        assert await runner.recover_orphaned_runs() == 0
        async with sessionmaker()() as db:
            assert await db.get(RuntimeLease, ids[0]) is None
            owner = (await db.get(Run, ids[0])).environment_snapshot["research_execution_owner"]
            assert owner["generation_id"] == str(a.id)
    finally:
        adapter.release.set()
        await task
    try:
        assert await a.can_retire()
        async with sessionmaker()() as db:
            for rid in ids:
                row = await db.get(Run, rid)
                assert row.status == "succeeded"
                assert row.environment_snapshot["research_execution_owner"]["generation_id"] == str(a.id)
        assert adapter.calls == 2
    finally:
        await b.close()
        await a.close()


async def test_batch_rejects_reservation_from_another_generation(client, tmp_path, monkeypatch):
    from ttakkari.generation import GenerationRuntime, activate
    from ttakkari.research.ownership import ExecutionOwner

    a = GenerationRuntime(tmp_path, revision="a" * 40, api_url="http://127.0.0.1:18791")
    b = GenerationRuntime(tmp_path, revision="b" * 40, api_url="http://127.0.0.1:18792")
    await a.register()
    await b.register()
    await activate(b.id, expected=None)
    monkeypatch.setattr(runner.generation, "current", a)
    exp_id, ids = await seed_runs()
    owner = ExecutionOwner.acquire()
    async with sessionmaker()() as db:
        row = await db.get(Run, ids[0])
        row.environment_snapshot = owner.snapshot({})
        await db.commit()
    monkeypatch.setattr(runner.generation, "current", b)
    adapter = ProbeAdapter()
    adapter.release.set()
    try:
        await runner.run_experiment_batch(exp_id, ids, adapter, _owner=owner)
        assert adapter.calls == 0
        async with sessionmaker()() as db:
            row = await db.get(Run, ids[0])
            assert row.status == "cancelled" and row.error_code == "GENERATION_REJECTED"
    finally:
        owner.close()
        await b.close()
        await a.close()
