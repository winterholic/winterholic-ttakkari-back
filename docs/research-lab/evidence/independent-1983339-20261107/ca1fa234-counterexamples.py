"""Temporary DB-free counterexamples for frozen commit ca1fa234."""
from __future__ import annotations

import contextlib
import uuid
from types import SimpleNamespace
from typing import ClassVar
from unittest.mock import AsyncMock, MagicMock

import pytest

from ttakkari.models import utcnow
from ttakkari.research import runner
from ttakkari.research.adapters.base import AdapterRunResult
from ttakkari.research.models import Configuration, Experiment, Run, Study
from ttakkari.research.ownership import OWNER_KEY


class DB:
    def __init__(self, records):
        self.records = records

    async def get(self, cls, ident, **kwargs):
        return self.records.get(cls, {}).get(ident)

    async def refresh(self, obj, *args, **kwargs):
        return None

    async def scalar(self, query):
        return uuid.uuid4()

    async def commit(self):
        return None

    async def rollback(self):
        return None

    def add(self, obj):
        return None

    @property
    def no_autoflush(self):
        return contextlib.nullcontext()


class SessionManager:
    def __init__(self, db):
        self.db = db

    def __call__(self):
        return self

    async def __aenter__(self):
        return self.db

    async def __aexit__(self, *args):
        return None


class Owner:
    metadata: ClassVar[dict[str, object]] = {"generation_id": None, "token": "synthetic-owner-token"}

    @classmethod
    def acquire(cls):
        return cls()

    def snapshot(self, environment, previous=None):
        return {**(environment or {}), OWNER_KEY: self.metadata}

    def close(self):
        return None


class Guard:
    samples: ClassVar[list[int]] = [1]
    error_code = "memory_guard_stopped"

    def snapshot(self):
        return {"error_code": self.error_code, "samples": self.samples}


def run_fixture():
    study_id, experiment_id, configuration_id, run_id = [uuid.uuid4() for _ in range(4)]
    study = Study(id=study_id, scope="personal", title="synthetic", created_at=utcnow())
    experiment = Experiment(id=experiment_id, study_id=study_id, title="synthetic", budget={}, created_at=utcnow())
    configuration = Configuration(id=configuration_id, experiment_id=experiment_id, label="synthetic", parameters={}, config_hash="synthetic", created_at=utcnow())
    run = Run(id=run_id, study_id=study_id, experiment_id=experiment_id, configuration_id=configuration_id,
              adapter_id="synthetic", status="queued", created_at=utcnow())
    db = DB({Study: {study_id: study}, Experiment: {experiment_id: experiment},
             Configuration: {configuration_id: configuration}, Run: {run_id: run}})
    return run_id, run, db


def patch_execution(monkeypatch, db, guard):
    monkeypatch.setattr(runner, "sessionmaker", lambda: SessionManager(db))
    monkeypatch.setattr(runner, "ExecutionOwner", Owner)
    monkeypatch.setattr(runner, "_fence_owner", lambda run, owner: None)
    monkeypatch.setattr(runner, "worker_memory_limit", lambda memory_mb: contextlib.nullcontext(guard))
    from ttakkari.research import resources
    monkeypatch.setattr(resources, "resource_checkpoints", lambda callback: contextlib.nullcontext())


@pytest.mark.asyncio
async def test_adapter_failed_with_memory_guard_error_stays_failed(monkeypatch):
    run_id, _, db = run_fixture()
    patch_execution(monkeypatch, db, Guard())
    adapter = MagicMock()
    adapter.execute_run = AsyncMock(return_value=AdapterRunResult(
        status="failed", error_code="adapter_failed", error_message="synthetic adapter failure",
        raw_output={"sentinel": "synthetic-only"}, environment_snapshot={}, wall_seconds=0.01,
    ))
    registry = MagicMock()
    registry.get_adapter.return_value = adapter

    result = await runner.execute_run_instance(run_id, adapter_registry=registry)

    assert result.status == "failed"
    assert result.error_code == "adapter_failed"
    assert result.environment_snapshot["research_memory_guard"]["error_code"] == "memory_guard_stopped"


@pytest.mark.asyncio
async def test_adapter_timeout_before_deadline_is_not_experiment_timeout(monkeypatch):
    run_id, _, db = run_fixture()
    patch_execution(monkeypatch, db, None)
    adapter = MagicMock()
    adapter.execute_run = AsyncMock(side_effect=TimeoutError("synthetic adapter-local timeout"))
    registry = MagicMock()
    registry.get_adapter.return_value = adapter

    result = await runner.execute_run_instance(run_id, adapter_registry=registry)

    # An adapter-local TimeoutError before any experiment deadline should be a
    # failed adapter run. This assertion is expected to expose the target defect.
    assert result.status == "failed"
    assert result.error_code == "TimeoutError"
    assert "adapter-local" in result.error_message


@pytest.mark.asyncio
async def test_unknown_cancelled_run_does_not_stop_later_batch_sibling(monkeypatch):
    experiment_id, run1_id, run2_id = [uuid.uuid4() for _ in range(3)]
    experiment = Experiment(id=experiment_id, title="synthetic", budget={"max_total_seconds": 60}, created_at=utcnow())
    db = DB({Experiment: {experiment_id: experiment}})
    owner = Owner()
    executed = []
    run1 = SimpleNamespace(id=run1_id, status="cancelled", error_code="adapter_internal_abort")
    run2 = SimpleNamespace(id=run2_id, status="succeeded", error_code=None)

    @contextlib.asynccontextmanager
    async def accepted_work(*args, **kwargs):
        yield True

    async def execute(run_id, **kwargs):
        executed.append(run_id)
        return run1 if run_id == run1_id else run2

    monkeypatch.setattr(runner, "sessionmaker", lambda: SessionManager(db))
    monkeypatch.setattr(runner.generation, "enter_accepted_work", accepted_work)
    monkeypatch.setattr(runner, "execute_run_instance", execute)
    monkeypatch.setattr(runner, "_finish_experiment_if_terminal", AsyncMock(return_value=False))

    await runner._run_owned_batch(experiment_id, [run1_id, run2_id], MagicMock(), owner)

    assert executed == [run1_id, run2_id]


@pytest.mark.asyncio
async def test_preview_fallback_is_explicitly_labeled_incomplete(monkeypatch):
    from ttakkari.research.adapters.llm_inference import LlmInferenceAdapter

    adapter = LlmInferenceAdapter()
    bench = {"runs": [{"run": 1, "output_preview": "synthetic preview only"}]}
    host = MagicMock()
    host.start = MagicMock()
    def get_current():
        launch_id = host.start.call_args.kwargs.get("launch_id") if host.start.call_args else "synthetic-launch"
        return {"launch_id": launch_id, "phase": "ready"}
    host._current.side_effect = get_current
    host.status.return_value = {"ready": True}
    host.bench.return_value = bench
    host.stop.return_value = {"stopped_now": True, "running": False}
    monkeypatch.setattr("ttakkari.host_gpu.HostGpu", lambda _: host)
    monkeypatch.setattr("ttakkari.research.adapters.llm_inference.checkpoint_resource", AsyncMock())
    monkeypatch.setattr(adapter, "validate_config", lambda cfg, scope: [])
    monkeypatch.setattr(adapter, "_cleanup_owned_launch", lambda *args: {"success": True})

    result = await adapter.execute_run(uuid.uuid4(), {"model": "synthetic"}, scope="personal")
    item = result.raw_output["sample_trajectories"][0]

    assert item["response"] == "synthetic preview only"
    assert item.get("response_completeness") == "preview_fallback"
