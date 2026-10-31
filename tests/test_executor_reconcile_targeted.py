from __future__ import annotations

import uuid

import pytest


@pytest.mark.asyncio
async def test_targeted_reconcile_refuses_changed_owner_before_apply(monkeypatch):
    from ttakkari import executor_reconcile

    run_id = uuid.uuid4()
    owner_id = uuid.uuid4()
    applied = []
    monkeypatch.setattr(executor_reconcile, "_read_orphans", lambda *_: _resolved([{
        "run_id": str(run_id), "owner_id": str(owner_id), "owner_revision": "a" * 40,
        "run_status": "running", "disposition": "safe_to_release", "groups": [],
    }]))
    monkeypatch.setattr(executor_reconcile, "_apply_one", lambda entry: _resolved(applied.append(entry) or True))

    result = await executor_reconcile.reconcile_one(
        run_id, expected_owner_generation_id=uuid.uuid4(), expected_owner_revision="a" * 40,
        expected_run_status="running", expected_claim_owner_generation_id=uuid.uuid4(),
        expected_claim_owner_revision="c" * 40, expected_claim_owner_epoch=1,
    )

    assert result == {"run_id": str(run_id), "reconciled": False, "action": "fence_mismatch"}
    assert applied == []


@pytest.mark.asyncio
async def test_targeted_reconcile_applies_only_exact_fenced_run(monkeypatch):
    from ttakkari import executor_reconcile

    run_id = uuid.uuid4()
    owner_id = uuid.uuid4()
    claim_owner_id = uuid.uuid4()
    entry = {"run_id": str(run_id), "owner_id": str(owner_id), "owner_revision": "b" * 40,
             "run_status": "running", "disposition": "safe_to_release", "groups": []}
    applied = []
    monkeypatch.setattr(executor_reconcile, "_read_orphans", lambda *_: _resolved([entry]))
    monkeypatch.setattr(executor_reconcile, "_apply_one", lambda value: _resolved(applied.append(value) or True))

    result = await executor_reconcile.reconcile_one(
        run_id, expected_owner_generation_id=owner_id, expected_owner_revision="b" * 40,
        expected_run_status="running", expected_claim_owner_generation_id=claim_owner_id,
        expected_claim_owner_revision="c" * 40, expected_claim_owner_epoch=1,
    )

    assert result == {"run_id": str(run_id), "reconciled": True, "action": "interrupted_and_released"}
    assert len(applied) == 1
    assert applied[0] == {**entry, "expected_claim_owner_fence": {
        "generation_id": claim_owner_id, "revision": "c" * 40, "epoch": 1,
    }}


async def _resolved(value):
    return value


@pytest.mark.asyncio
async def test_coordinator_reaper_passes_live_handles_and_survives_failures(monkeypatch):
    from types import SimpleNamespace

    from ttakkari import executor_reconcile, generation_service
    from ttakkari.runs import test_database

    run_id, gen_id = uuid.uuid4(), uuid.uuid4()
    seen = {}

    async def reaper(*, generation_id, handles, stale_after=120.0):
        seen["reaper"] = (generation_id, set(handles))
        return 1

    def leaked(handles, **_):
        seen["leaked"] = set(handles)
        return ["ttakkari-run-test-1-aaaa"]

    monkeypatch.setattr(executor_reconcile, "reap_handleless_runs", reaper)
    monkeypatch.setattr(test_database, "reap_leaked", leaked)
    runtime, manager = SimpleNamespace(id=gen_id), SimpleNamespace(_handles={run_id: object()})
    await generation_service._reap_handleless(runtime, manager)
    assert seen == {"reaper": (gen_id, {run_id}), "leaked": {run_id}}

    async def boom(**_):
        raise RuntimeError("db unavailable")

    monkeypatch.setattr(executor_reconcile, "reap_handleless_runs", boom)
    await generation_service._reap_handleless(runtime, manager)  # 예외를 삼키고 다음 주기를 기다린다
