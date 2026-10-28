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
