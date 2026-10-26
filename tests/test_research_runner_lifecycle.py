"""Real PostgreSQL probes for cancellation and duplicate Research Run claims."""
from __future__ import annotations

import asyncio
import os
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
    assert result.environment_snapshot == {"execution": "synthetic_adapter"}
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
    assert result.environment_snapshot == {"execution": "synthetic_adapter"}
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
