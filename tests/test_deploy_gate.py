from __future__ import annotations

import asyncio
import uuid

from ttakkari.db import sessionmaker
from ttakkari.deploy_gate import RestartGate, _active_run_count
from ttakkari.models import Run


def test_gate_rechecks_after_wait_and_excludes_deploying_run(tmp_path):
    counts = iter([0, 1, 0, 0])
    restarts: list[str] = []

    gate = RestartGate(tmp_path / "restart.lock", poll_seconds=0, max_wait_seconds=1)
    asyncio.run(gate.restart_backend(lambda: next(counts), lambda: restarts.append("restart"), own_run_id="self"))

    assert restarts == ["restart"]


def test_gate_skips_duplicate_restart_when_lock_is_held(tmp_path):
    gate = RestartGate(tmp_path / "restart.lock", poll_seconds=0, max_wait_seconds=1)
    first = gate._acquire()
    try:
        called: list[bool] = []
        result = asyncio.run(gate.restart_backend(lambda: 0, lambda: called.append(True)))
    finally:
        gate._release(first)

    assert result is False
    assert called == []


def test_frontend_deploy_is_not_gated(tmp_path):
    gate = RestartGate(tmp_path / "restart.lock")
    called: list[bool] = []

    gate.deploy_frontend(lambda: called.append(True))

    assert called == [True]


def test_active_runs_timeout_without_restarting(tmp_path):
    gate = RestartGate(tmp_path / "restart.lock", poll_seconds=0, max_wait_seconds=0.01)
    called: list[bool] = []

    result = asyncio.run(gate.restart_backend(lambda: 1, lambda: called.append(True)))

    assert result is False
    assert called == []


async def test_deploy_run_is_excluded_but_other_runs_still_block(client, auth, new_session):
    ws, sess = await new_session()
    own_id, other_id = uuid.uuid4(), uuid.uuid4()
    async with sessionmaker()() as db:
        db.add_all([
            Run(id=own_id, session_id=uuid.UUID(sess["id"]), workspace_id=uuid.UUID(ws["id"]),
                prompt="deploy", engine="claude", status="running"),
            Run(id=other_id, session_id=uuid.UUID(sess["id"]), workspace_id=uuid.UUID(ws["id"]),
                prompt="other", engine="claude", status="running"),
            Run(session_id=uuid.UUID(sess["id"]), workspace_id=uuid.UUID(ws["id"]),
                prompt="queued", engine="claude", status="queued"),
        ])
        await db.commit()

    assert await _active_run_count(str(own_id)) == 1
    assert await _active_run_count(None) == 2
