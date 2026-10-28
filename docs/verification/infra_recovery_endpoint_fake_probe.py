"""Deterministic API/reconcile boundary probe; never opens a database or signals a process."""
from __future__ import annotations

import asyncio
import uuid
from types import SimpleNamespace

from fastapi import HTTPException
from pydantic import ValidationError

from ttakkari import executor_reconcile, generation
from ttakkari.api import orchestrator as api
from ttakkari.models import RunStatus, SessionKind
from ttakkari.runs.manager import manager


class FakeDB:
    def __init__(self, rows, run=None):
        self.rows, self.run = rows, run

    async def get(self, model, key):
        return self.rows.get((model.__name__, key))

    async def scalar(self, _query):
        return self.run

    async def refresh(self, run):
        run.status = RunStatus.running


async def main() -> None:
    caller_id, worker_id, outside_id, run_id, owner_id = (uuid.uuid4() for _ in range(5))
    caller = SimpleNamespace(id=caller_id, kind=SessionKind.orchestrator, archived_at=None,
                             pool="personal", parent_id=None)
    worker = SimpleNamespace(id=worker_id, kind=SessionKind.worker, archived_at=None,
                             pool="personal", parent_id=caller_id)
    outside = SimpleNamespace(id=worker_id, kind=SessionKind.worker, archived_at=None,
                              pool="personal", parent_id=outside_id)
    body = {
        "run_id": run_id,
        "expected_status": "queued",
        "expected_pool": "personal",
        "expected_claim_owner_generation_id": owner_id,
        "expected_claim_owner_revision": "a" * 40,
        "idempotency_key": f"dispatch:{worker_id}:{run_id}",
    }
    try:
        api.DispatchQueuedRunIn.model_validate({"run_id": str(run_id)})
    except ValidationError:
        pass
    else:
        raise AssertionError("missing required fields were accepted")

    async def reject(rows, payload, status):
        try:
            await api.dispatch_queued_worker_run(
                worker_id, api.DispatchQueuedRunIn(**payload), sid=caller_id, db=FakeDB(rows)
            )
        except HTTPException as exc:
            assert exc.status_code == status
        else:
            raise AssertionError(f"expected HTTP {status}")

    direct = {("ChatSession", caller_id): caller, ("ChatSession", worker_id): worker}
    await reject(direct, {**body, "idempotency_key": "spoof"}, 422)
    await reject(direct, {**body, "expected_pool": "work"}, 404)
    await reject(
        {**direct, ("ChatSession", worker_id): outside,
         ("ChatSession", outside_id): SimpleNamespace(parent_id=None)},
        body,
        404,
    )

    generation_row = SimpleNamespace(id=owner_id, revision="a" * 40, state="active")
    control = SimpleNamespace(executor_generation=None, active_generation=owner_id, epoch=19)
    rows = {
        **direct,
        ("RuntimeControl", 1): control,
        ("RuntimeGeneration", owner_id): generation_row,
    }
    old_claim_owner, old_runtime, old_dispatch = generation.claim_owner, manager._runtime, manager.dispatch
    generation.claim_owner = lambda _control: owner_id
    manager._runtime = SimpleNamespace(
        id=owner_id, draining=False, role="api", execution_mode="local"
    )

    async def fake_dispatch(*_args, **_kwargs):
        return run_id

    manager.dispatch = fake_dispatch
    try:
        await reject(rows, {**body, "expected_claim_owner_revision": "b" * 40}, 409)
        await reject(rows, body, 404)  # Exact target session/run lookup has no row.
        run = SimpleNamespace(id=run_id, session_id=worker_id, status=RunStatus.queued)
        result = await api.dispatch_queued_worker_run(
            worker_id, api.DispatchQueuedRunIn(**body), sid=caller_id, db=FakeDB(rows, run)
        )
        assert result["idempotent"] and control.epoch == 19
    finally:
        generation.claim_owner, manager._runtime, manager.dispatch = (
            old_claim_owner,
            old_runtime,
            old_dispatch,
        )

    assert executor_reconcile.disposition(owner_dead=False, groups=[("present", True)]) == "owner_alive"
    assert await executor_reconcile._apply_one({"disposition": "owner_alive"}) is False
    print("PASS fake API: required input / spoofed key / other pool / cross-tree / exact run lookup / stale revision fail closed")
    print("FAIL fence: stale A+revision accepted with control.epoch=19; API/MCP have no expected epoch")
    print("PASS fake reconcile: live owner disposition=owner_alive; _apply_one returned False before DB/OS action")


if __name__ == "__main__":
    asyncio.run(main())
