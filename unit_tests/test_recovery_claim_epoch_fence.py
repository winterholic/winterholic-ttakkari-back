from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from ttakkari.api import orchestrator

OWNER_ID = uuid.UUID("89f8ccbc-e9de-493d-9191-91307220d85b")
WORKER_ID = uuid.uuid4()
RUN_ID = uuid.uuid4()
SESSION_ID = uuid.uuid4()
REVISION = "a" * 40


@pytest.mark.parametrize("model", [orchestrator.DispatchQueuedRunIn, orchestrator.ReconcileWorkerLeaseIn])
def test_recovery_requests_require_a_strict_nonnegative_claim_epoch(model):
    payload = {
        "run_id": RUN_ID,
        "expected_status": "queued" if model is orchestrator.DispatchQueuedRunIn else "running",
        "expected_pool": "personal",
        "expected_claim_owner_generation_id": OWNER_ID,
        "expected_claim_owner_revision": REVISION,
        "expected_lease_owner_generation_id": uuid.uuid4(),
        "expected_lease_owner_revision": "b" * 40,
        "idempotency_key": "key",
    }
    with pytest.raises(ValidationError):
        model.model_validate(payload)
    with pytest.raises(ValidationError):
        model.model_validate({**payload, "expected_claim_owner_epoch": -1})
    with pytest.raises(ValidationError):
        model.model_validate({**payload, "expected_claim_owner_epoch": True})
    with pytest.raises(ValidationError):
        model.model_validate({**payload, "expected_claim_owner_epoch": "7"})


def _dispatch_body(epoch: int) -> orchestrator.DispatchQueuedRunIn:
    return orchestrator.DispatchQueuedRunIn(
        run_id=RUN_ID, expected_status="queued", expected_pool="personal",
        expected_claim_owner_generation_id=OWNER_ID, expected_claim_owner_revision=REVISION,
        expected_claim_owner_epoch=epoch, idempotency_key=f"dispatch:{WORKER_ID}:{RUN_ID}",
    )


async def test_api_rejects_aba_claim_epoch_before_dispatch(monkeypatch):
    from pathlib import Path

    from ttakkari.generation import GenerationRuntime
    from ttakkari.models import RuntimeControl, RuntimeGeneration

    control = SimpleNamespace(active_generation=OWNER_ID, executor_generation=None, epoch=19)
    owner = SimpleNamespace(id=OWNER_ID, revision=REVISION, state="active")

    async def manager_session(_db, _sid):
        return SimpleNamespace(id=SESSION_ID, pool="personal")

    async def owned_worker(_db, _caller, _worker_id):
        return SimpleNamespace(id=WORKER_ID, pool="personal")

    class FakeDb:
        async def get(self, model, _key):
            if model is RuntimeControl:
                return control
            if model is RuntimeGeneration:
                return owner
            raise AssertionError(model)

        async def scalar(self, _statement):
            raise AssertionError("stale epoch must fail before the exact Run lookup")

    async def forbidden_dispatch(*_args, **_kwargs):
        raise AssertionError("stale epoch reached manager.dispatch")

    monkeypatch.setattr(orchestrator, "_manager", manager_session)
    monkeypatch.setattr(orchestrator, "_owned_worker", owned_worker)
    monkeypatch.setattr(orchestrator.manager, "dispatch", forbidden_dispatch)
    runtime = GenerationRuntime(Path("/tmp"), revision=REVISION,
                                api_url="http://127.0.0.1:18791")
    runtime.id = OWNER_ID
    runtime.execution_mode = "local"
    monkeypatch.setattr(orchestrator.manager, "_runtime", runtime)

    with pytest.raises(HTTPException) as exc:
        await orchestrator.dispatch_queued_worker_run(WORKER_ID, _dispatch_body(18), SESSION_ID, FakeDb())
    assert exc.value.status_code == 409


@pytest.mark.parametrize("operation", ["dispatch", "reconcile"])
@pytest.mark.parametrize("owner_state,runtime_mode,expected_status", [
    ("active", "local", None), ("draining", "local", 409), ("active", "remote", 409),
])
async def test_recovery_routes_use_real_generation_runtime_contract(
    tmp_path, monkeypatch, operation, owner_state, runtime_mode, expected_status
):
    from pathlib import Path

    from ttakkari.generation import GenerationRuntime
    from ttakkari.models import RuntimeControl, RuntimeGeneration

    control = SimpleNamespace(active_generation=OWNER_ID, executor_generation=None, epoch=19)
    owner = SimpleNamespace(id=OWNER_ID, revision=REVISION, state=owner_state)
    caller = SimpleNamespace(id=SESSION_ID, pool="personal")
    target = SimpleNamespace(id=WORKER_ID, pool="personal")
    run = SimpleNamespace(id=RUN_ID, session_id=WORKER_ID, status="queued" if operation == "dispatch" else "running")

    async def manager_session(_db, _sid):
        return caller

    async def owned_worker(_db, _caller, _worker_id):
        return target

    class FakeDb:
        async def get(self, model, _key):
            if model is RuntimeControl:
                return control
            if model is RuntimeGeneration:
                return owner
            raise AssertionError(model)

        async def scalar(self, _statement):
            return run

        async def refresh(self, _row):
            return None

    runtime = GenerationRuntime(Path(tmp_path), revision=REVISION, api_url="http://127.0.0.1:18791")
    runtime.id = OWNER_ID
    runtime.execution_mode = runtime_mode
    monkeypatch.setattr(orchestrator, "_manager", manager_session)
    monkeypatch.setattr(orchestrator, "_owned_worker", owned_worker)
    monkeypatch.setattr(orchestrator.manager, "_runtime", runtime)

    if operation == "dispatch":
        dispatch = AsyncMock(return_value=RUN_ID)
        monkeypatch.setattr(orchestrator.manager, "dispatch", dispatch)
        body = _dispatch_body(19)
        endpoint = orchestrator.dispatch_queued_worker_run
    else:
        reconcile = AsyncMock(return_value={"reconciled": True})
        monkeypatch.setattr("ttakkari.executor_reconcile.reconcile_one", reconcile)
        body = orchestrator.ReconcileWorkerLeaseIn(
            run_id=RUN_ID, expected_status="running", expected_pool="personal",
            expected_claim_owner_generation_id=OWNER_ID, expected_claim_owner_revision=REVISION,
            expected_claim_owner_epoch=19, expected_lease_owner_generation_id=uuid.uuid4(),
            expected_lease_owner_revision="b" * 40,
            idempotency_key=f"reconcile:{WORKER_ID}:{RUN_ID}:{uuid.uuid4()}",
        )
        # Bind the idempotency key to the supplied lease owner exactly.
        body = body.model_copy(update={
            "idempotency_key": f"reconcile:{WORKER_ID}:{RUN_ID}:{body.expected_lease_owner_generation_id}"
        })
        endpoint = orchestrator.reconcile_worker_lease

    if expected_status is not None:
        with pytest.raises(HTTPException) as exc:
            await endpoint(WORKER_ID, body, SESSION_ID, FakeDb())
        assert exc.value.status_code == expected_status
        if operation == "dispatch":
            dispatch.assert_not_awaited()
        else:
            reconcile.assert_not_awaited()
    else:
        response = await endpoint(WORKER_ID, body, SESSION_ID, FakeDb())
        if operation == "dispatch":
            assert response["run_id"] == str(RUN_ID)
            dispatch.assert_awaited_once()
        else:
            assert response["reconciled"]
            reconcile.assert_awaited_once()


async def test_compensation_reads_persisted_drain_state_with_generation_runtime(tmp_path, monkeypatch):
    from pathlib import Path

    from ttakkari import executor_reconcile, generation
    from ttakkari.generation import GenerationRuntime
    from ttakkari.models import RuntimeGeneration
    from ttakkari.runs import compensation
    from ttakkari.runs.manager import manager

    runtime = GenerationRuntime(Path(tmp_path), revision=REVISION, api_url="http://127.0.0.1:18791")
    runtime.id = OWNER_ID
    runtime.execution_mode = "local"
    owner = SimpleNamespace(id=OWNER_ID, state="draining")
    control = SimpleNamespace(active_generation=OWNER_ID, executor_generation=None)

    class FakeDb:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def get(self, model, _key):
            assert model is RuntimeGeneration
            return owner

        async def commit(self):
            return None

    db = FakeDb()
    reconcile = AsyncMock()
    monkeypatch.setattr(compensation, "sessionmaker", lambda: lambda: db)
    monkeypatch.setattr(generation, "_control", AsyncMock(return_value=control))
    monkeypatch.setattr(manager, "_runtime", runtime)
    monkeypatch.setattr(executor_reconcile, "reconcile_orphans", reconcile)

    assert await compensation.compensate_runs() == {"requeued": 0, "orphans": 0}
    reconcile.assert_not_awaited()


async def test_compensation_passes_exact_claim_fence_to_orphan_apply(tmp_path, monkeypatch):
    from pathlib import Path

    from ttakkari import executor_reconcile, generation
    from ttakkari.generation import GenerationRuntime
    from ttakkari.models import RuntimeGeneration
    from ttakkari.runs import compensation
    from ttakkari.runs.manager import manager

    runtime = GenerationRuntime(Path(tmp_path), revision=REVISION, api_url="http://127.0.0.1:18791")
    runtime.id = OWNER_ID
    runtime.execution_mode = "local"
    owner = SimpleNamespace(id=OWNER_ID, revision=REVISION, state="active")
    control = SimpleNamespace(active_generation=OWNER_ID, executor_generation=None, epoch=19)

    class FakeDb:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def get(self, model, _key):
            assert model is RuntimeGeneration
            return owner

        async def scalars(self, _statement):
            return []

        async def commit(self):
            return None

    db = FakeDb()
    reconcile = AsyncMock(return_value={"reconciled": 0})
    monkeypatch.setattr(compensation, "sessionmaker", lambda: lambda: db)
    monkeypatch.setattr(generation, "_control", AsyncMock(return_value=control))
    monkeypatch.setattr(manager, "_runtime", runtime)
    monkeypatch.setattr(executor_reconcile, "reconcile_orphans", reconcile)

    assert await compensation.compensate_runs() == {"requeued": 0, "orphans": 0}
    reconcile.assert_awaited_once_with(apply=True, expected_claim_owner_fence={
        "generation_id": OWNER_ID, "revision": REVISION, "epoch": 19,
    })


async def test_compensation_without_runtime_fails_closed(monkeypatch):
    from ttakkari import executor_reconcile, generation
    from ttakkari.runs import compensation
    from ttakkari.runs.manager import manager

    class FakeDb:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

    db = FakeDb()
    reconcile = AsyncMock()
    monkeypatch.setattr(compensation, "sessionmaker", lambda: lambda: db)
    monkeypatch.setattr(manager, "_runtime", None)
    monkeypatch.setattr(generation, "current", None)
    monkeypatch.setattr(executor_reconcile, "reconcile_orphans", reconcile)

    assert await compensation.compensate_runs() == {"requeued": 0, "orphans": 0}
    reconcile.assert_not_awaited()


async def test_api_rejects_spoofed_idempotency_key_before_owner_read(monkeypatch):
    async def manager_session(_db, _sid):
        return SimpleNamespace(id=SESSION_ID, pool="personal")

    async def owned_worker(_db, _caller, _worker_id):
        return SimpleNamespace(id=WORKER_ID, pool="personal")

    class FakeDb:
        async def get(self, *_args):
            pytest.fail("spoofed key reached claim-owner lookup")

    monkeypatch.setattr(orchestrator, "_manager", manager_session)
    monkeypatch.setattr(orchestrator, "_owned_worker", owned_worker)
    body = _dispatch_body(19).model_copy(update={"idempotency_key": "dispatch:another-worker:another-run"})
    with pytest.raises(HTTPException) as exc:
        await orchestrator.dispatch_queued_worker_run(WORKER_ID, body, SESSION_ID, FakeDb())
    assert exc.value.status_code == 422


async def test_manager_rechecks_claim_epoch_while_holding_session_lock(monkeypatch):
    from pathlib import Path

    from ttakkari.generation import GenerationRuntime
    from ttakkari.runs.manager import RunManager

    m = RunManager()
    runtime = GenerationRuntime(Path("/tmp"), revision=REVISION, api_url="http://127.0.0.1:18791")
    runtime.id = OWNER_ID
    runtime.execution_mode = "local"
    runtime.refresh = lambda: _active()
    m._runtime = runtime
    control = SimpleNamespace(active_generation=OWNER_ID, executor_generation=None, epoch=20)
    session = SimpleNamespace(id=WORKER_ID, kind="worker", pool="personal")
    scalar_calls = 0

    class FakeDb:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def scalar(self, _statement):
            nonlocal scalar_calls
            scalar_calls += 1
            assert "FOR UPDATE" in str(_statement).upper()
            return session if scalar_calls == 1 else control

        async def get(self, model, _key):
            if model.__name__ == "RuntimeGeneration":
                return SimpleNamespace(id=OWNER_ID, revision=REVISION, state="active")
            raise AssertionError(model)

        async def rollback(self):
            self.rolled_back = True

    fake_db = FakeDb()
    monkeypatch.setattr("ttakkari.runs.manager.sessionmaker", lambda: lambda: fake_db)
    monkeypatch.setattr(m, "start", lambda *_args, **_kwargs: pytest.fail("stale epoch scheduled a Run"))

    result = await m.dispatch(
        WORKER_ID, expected_run_id=RUN_ID, expected_pool="personal",
        expected_claim_owner_generation_id=OWNER_ID,
        expected_claim_owner_revision=REVISION, expected_claim_owner_epoch=19,
    )

    assert result is None
    assert fake_db.rolled_back
    assert scalar_calls == 2


async def _active():
    return "active"


async def test_reconcile_checks_claim_fence_under_control_lock_before_lease_read(monkeypatch):
    from ttakkari import executor_reconcile

    control = SimpleNamespace(active_generation=OWNER_ID, executor_generation=None, epoch=19)
    entry = {
        "owner_id": str(uuid.uuid4()), "_lease_token": uuid.uuid4(), "_group_rows": [],
        "expected_claim_owner_fence": {"generation_id": OWNER_ID, "revision": REVISION, "epoch": 18},
    }

    async def locked_control(_db):
        return control

    class FakeDb:
        async def scalar(self, _statement):
            pytest.fail("stale claim epoch reached lease lookup")

    monkeypatch.setattr(executor_reconcile, "_control", locked_control)
    assert await executor_reconcile._apply_locked(FakeDb(), entry, RUN_ID, uuid.uuid4(), []) is False


async def test_reconcile_passes_exact_claim_epoch_fence_to_locked_apply(monkeypatch):
    from ttakkari import executor_reconcile

    lease_owner_id = uuid.uuid4()
    entry = {
        "run_id": str(RUN_ID), "owner_id": str(lease_owner_id), "owner_revision": "b" * 40,
        "run_status": "running", "disposition": "safe_to_release", "groups": [],
    }
    applied = []

    async def read_one(_run_id):
        return [entry]

    async def apply_one(value):
        applied.append(value)
        return True

    monkeypatch.setattr(executor_reconcile, "_read_orphans", read_one)
    monkeypatch.setattr(executor_reconcile, "_apply_one", apply_one)
    result = await executor_reconcile.reconcile_one(
        RUN_ID, expected_owner_generation_id=lease_owner_id, expected_owner_revision="b" * 40,
        expected_run_status="running", expected_claim_owner_generation_id=OWNER_ID,
        expected_claim_owner_revision=REVISION, expected_claim_owner_epoch=19,
    )

    assert result["reconciled"] is True
    assert applied == [{**entry, "expected_claim_owner_fence": {
        "generation_id": OWNER_ID, "revision": REVISION, "epoch": 19,
    }}]


async def test_live_owner_is_preserved_before_database_or_process_access(monkeypatch):
    from ttakkari import executor_reconcile

    entry = {"disposition": "owner_alive"}

    monkeypatch.setattr(executor_reconcile, "sessionmaker",
                        lambda: pytest.fail("live lease owner reached DB/process cleanup"))
    assert await executor_reconcile._apply_one(entry) is False


async def test_read_session_execution_receipt_includes_claim_epoch():
    from ttakkari.models import SessionKind
    from ttakkari.runs import diagnostics

    values = iter([SimpleNamespace(epoch=19, active_generation=OWNER_ID, executor_generation=None), 0, None])

    class FakeDb:
        async def scalar(self, _statement):
            return next(values)

    execution, _ = await diagnostics.session_execution(
        FakeDb(), SimpleNamespace(id=SESSION_ID, kind=SessionKind.worker), [])
    assert execution["claim_owner_epoch"] == 19


def test_mcp_recovery_tools_type_epoch_as_nonnegative_strict_integer():
    from typing import get_type_hints

    from ttakkari.orchestrator import mcp_server

    for function in (mcp_server.dispatch_queued_worker_run, mcp_server.reconcile_worker_lease):
        annotation = get_type_hints(function, include_extras=True)["expected_claim_owner_epoch"]
        constraints = annotation.__metadata__[0].metadata
        assert any(type(item).__name__ == "Ge" and item.ge == 0 for item in constraints)
        assert any(type(item).__name__ == "Strict" and item.strict is True for item in constraints)
