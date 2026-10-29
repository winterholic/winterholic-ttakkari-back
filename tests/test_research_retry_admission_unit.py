"""DB-free regressions for shared Research Run admission and retry validation."""
from __future__ import annotations

import asyncio
import uuid
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from ttakkari.research import runner
from ttakkari.research.models import Configuration, Event, Experiment, Run, Study


class _Rows:
    def __init__(self, rows):
        self._rows = rows

    def scalars(self):
        return self

    def all(self):
        return self._rows


class _AdmissionDB:
    """Small transaction mock: FOR UPDATE serializes sessions until commit."""

    def __init__(self, shared, *, original=None):
        self.shared = shared
        self.original = original
        self.pending = []
        self.locked = False
        self.get_calls = []

    async def get(self, model, identity, **kwargs):
        self.get_calls.append((model, kwargs))
        if model is Experiment:
            if kwargs.get("with_for_update"):
                await self.shared["lock"].acquire()
                self.locked = True
            return self.shared["experiment"]
        if model is Run:
            return self.original
        if model is Study:
            return self.shared["study"]
        if model is Configuration:
            return self.shared["configuration"]
        raise AssertionError(f"unexpected model: {model}")

    async def scalar(self, _query):
        found = next((row.id for row in self.shared["runs"]
                      if row.status in ("queued", "running")), None)
        # Expose the check/insert race that two transactions can hit.
        await asyncio.sleep(0)
        return found

    async def execute(self, _query):
        return _Rows([self.shared["configuration"]])

    def add(self, row):
        self.pending.append(row)

    async def commit(self):
        self.shared["runs"].extend(row for row in self.pending if isinstance(row, Run))
        self.shared["events"].extend(row for row in self.pending if isinstance(row, Event))
        self.pending.clear()
        if self.locked:
            self.locked = False
            self.shared["lock"].release()

    async def rollback(self):
        self.pending.clear()
        if self.locked:
            self.locked = False
            self.shared["lock"].release()


class _Owner:
    def snapshot(self, snapshot):
        return snapshot

    def close(self):
        pass


def _state():
    study_id, experiment_id, config_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    study = SimpleNamespace(id=study_id, scope="personal")
    experiment = SimpleNamespace(
        id=experiment_id, study_id=study_id, adapter_id="probe", method_id=None,
        compared_method_ids=[], strategy="single", search_space={}, fixed_inputs={}, budget={},
    )
    config = SimpleNamespace(id=config_id, order_index=1)
    original = Run(
        id=uuid.uuid4(), experiment_id=experiment_id, configuration_id=config_id,
        study_id=study_id, adapter_id="probe", execution_origin="local_measured",
        repeat_index=3, seed=812, status="failed", environment_snapshot={"source": "original"},
        raw_output={"original": True},
    )
    return {
        "lock": asyncio.Lock(), "study": study, "experiment": experiment,
        "configuration": config, "runs": [original], "events": [], "original": original,
    }


def _valid_plan():
    return SimpleNamespace(valid=True, errors=[])


@pytest.mark.asyncio
@pytest.mark.parametrize("contenders", [("retry", "normal"), ("normal", "retry"), ("normal", "normal")])
async def test_start_and_retry_share_experiment_row_admission_lock(monkeypatch, contenders):
    shared = _state()
    sessions = []
    monkeypatch.setattr(runner, "ExecutionOwner", SimpleNamespace(acquire=lambda: _Owner()))
    monkeypatch.setattr(runner, "run_experiment_batch", lambda *args, **kwargs: _noop())
    monkeypatch.setattr("ttakkari.research.planner.validate_experiment_plan", _validate_plan)

    async def attempt(kind):
        db = _AdmissionDB(shared, original=shared["original"])
        sessions.append(db)
        try:
            if kind == "retry":
                return await runner.retry_run(db, shared["original"].id, background=False)
            return await runner.start_experiment_runs(db, shared["experiment"].id, background=False)
        except HTTPException as exc:
            await db.rollback()
            return exc

    outcomes = await asyncio.gather(*(attempt(kind) for kind in contenders))
    admitted = [value for value in outcomes if not isinstance(value, HTTPException)]
    rejected = [value for value in outcomes if isinstance(value, HTTPException)]
    assert len(admitted) == 1
    assert len(rejected) == 1 and rejected[0].status_code == 409
    assert sum(row.status == "queued" for row in shared["runs"]) == 1
    assert all(any(model is Experiment and kwargs.get("with_for_update") is True
                   for model, kwargs in db.get_calls) for db in sessions)


async def _noop():
    return None


async def _validate_plan(*args, **kwargs):
    return _valid_plan()


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["retry", "normal"])
async def test_admission_reuses_plan_validation_before_owner_run_or_event(monkeypatch, kind):
    shared = _state()
    db = _AdmissionDB(shared, original=shared["original"])
    owner_calls = []
    experiment = shared["experiment"]
    experiment.method_id = uuid.uuid4()
    experiment.compared_method_ids = [uuid.uuid4()]
    experiment.strategy = "repeat"
    experiment.search_space = {"temperature": [0.2, 0.8]}
    experiment.fixed_inputs = {"dataset": "current"}
    experiment.budget = {"max_runs": 4}
    validated = []

    async def invalid_plan(*args, **kwargs):
        validated.append(kwargs)
        return SimpleNamespace(valid=False, errors=["hypothesis_stage"])

    monkeypatch.setattr("ttakkari.research.planner.validate_experiment_plan", invalid_plan)
    monkeypatch.setattr(runner, "ExecutionOwner", SimpleNamespace(acquire=lambda: owner_calls.append(True)))
    with pytest.raises(HTTPException) as error:
        if kind == "retry":
            await runner.retry_run(db, shared["original"].id, background=False)
        else:
            await runner.start_experiment_runs(db, experiment.id, background=False)
    assert error.value.status_code == 422
    assert len(shared["runs"]) == 1 and shared["events"] == []
    assert owner_calls == []
    assert len(validated) == 1
    assert validated[0] == {
        "study_id": experiment.study_id,
        "method_id": experiment.method_id,
        "compared_method_ids": experiment.compared_method_ids,
        "adapter_id": experiment.adapter_id,
        "strategy": experiment.strategy,
        "search_space": experiment.search_space,
        "fixed_inputs": experiment.fixed_inputs,
        "budget": experiment.budget,
        "scope": "personal",
    }
    await db.rollback()

