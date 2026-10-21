"""Executor boundary tests without a PostgreSQL server; DB tests live in tests/."""
from __future__ import annotations

import asyncio
import os
import signal
import subprocess
import sys
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import psutil
import pytest

from ttakkari import generation
from ttakkari.runs.manager import RunManager, _Handle


def test_claim_owner_is_independent_of_api_cutover():
    assert hasattr(generation, "claim_owner"), "executor ownership selection is missing"
    executor, old, new = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    control = SimpleNamespace(active_generation=old, executor_generation=executor)
    assert generation.claim_owner(control) == executor
    control.active_generation = new
    assert generation.claim_owner(control) == executor
    control.executor_generation = None
    assert generation.claim_owner(control) == new


def test_remote_api_start_never_creates_engine_task(monkeypatch):
    runtime = SimpleNamespace(role="api", execution_mode="remote")
    manager = RunManager(runtime=runtime)
    def unexpected(*args, **kwargs):
        raise AssertionError("API tried to create an engine task")
    monkeypatch.setattr(asyncio, "create_task", unexpected)
    assert manager.start(uuid.uuid4()) is False
    assert manager._handles == {}


async def test_remote_api_release_waiting_is_a_durable_command():
    runtime = SimpleNamespace(role="api", execution_mode="remote", command=AsyncMock())
    manager = RunManager(runtime=runtime)
    await manager.release_waiting()
    runtime.command.assert_awaited_once_with("release_waiting")


async def test_remote_cancel_does_not_inspect_or_kill_processes():
    rid = uuid.uuid4()
    runtime = SimpleNamespace(role="api", execution_mode="remote", command=AsyncMock())
    manager = RunManager(runtime=runtime)
    assert await manager.cancel(rid)
    runtime.command.assert_awaited_once_with("cancel", run_id=rid)


async def test_shutdown_does_not_release_lease_while_child_exit_is_unknown(monkeypatch):
    runtime = SimpleNamespace(role="executor", execution_mode="local", release=AsyncMock())
    manager = RunManager(runtime=runtime)
    rid, token = uuid.uuid4(), uuid.uuid4()
    manager._handles[rid] = _Handle(task=asyncio.current_task(),
        proc=SimpleNamespace(returncode=None, pid=987654), owner=runtime, token=token)
    assert hasattr(manager, "_release_attempt"), "safe release is missing"
    await manager._release_attempt(rid, runtime, token)
    runtime.release.assert_not_awaited()
    manager._handles[rid].proc.returncode = -9
    def gone(*_):
        raise ProcessLookupError
    monkeypatch.setattr(os, "killpg", gone)
    await manager._release_attempt(rid, runtime, token)
    runtime.release.assert_awaited_once_with(rid, token)


async def test_attempt_history_keeps_lease_when_an_earlier_group_outlives_its_leader():
    rid, token, owner = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    runtime = SimpleNamespace(id=owner, release=AsyncMock())
    leader = await asyncio.create_subprocess_exec(sys.executable, "-c",
        "import subprocess,sys; subprocess.Popen([sys.executable,'-c','import time; time.sleep(8)'],"
        "stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)",
        start_new_session=True, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
    leader_started_at = psutil.Process(leader.pid).create_time()
    await leader.wait()
    last = await asyncio.create_subprocess_exec(sys.executable, "-c", "pass", start_new_session=True)
    last_started_at = psutil.Process(last.pid).create_time()
    await last.wait()
    try:
        handle = _Handle(task=asyncio.current_task(), proc=last, owner=runtime, token=token)
        handle.process_groups = [(leader.pid, leader_started_at, leader.pid, owner, token),
                                 (last.pid, last_started_at, last.pid, owner, token)]
        manager = RunManager(runtime=runtime)
        manager._handles[rid] = handle

        await manager._release_attempt(rid, runtime, token)

        runtime.release.assert_not_awaited()
    finally:
        try:
            os.killpg(leader.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


def test_executor_cli_is_explicit_and_not_enabled_by_serve():
    result = subprocess.run([sys.executable, "-m", "ttakkari.cli", "executor", "--help"],
                            capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr
    assert "--activate" in result.stdout


@pytest.mark.parametrize("leases,apis,error", [
    (1, [(2, "remote")], "lease"),
    (0, [(1, "remote")], "protocol"),
    (0, [(2, "local")], "remote"),
    (0, [], "API"),
])
def test_executor_activation_refuses_unsafe_handoff(leases, apis, error):
    from ttakkari.executor import validate_handoff
    with pytest.raises(RuntimeError, match=error):
        validate_handoff(leases=leases, apis=apis)


def test_executor_activation_accepts_only_idle_remote_protocol2_apis():
    from ttakkari.executor import validate_handoff
    validate_handoff(leases=0, apis=[(2, "remote"), (2, "remote")])


async def test_api_remote_tick_does_not_consume_executor_commands(monkeypatch):
    from ttakkari import generation_service
    runtime = SimpleNamespace(role="api", execution_mode="remote")
    manager = RunManager(runtime=runtime)
    consume, resume = AsyncMock(), AsyncMock()
    monkeypatch.setattr(generation_service, "_commands", consume)
    import ttakkari.runs.manager as run_module
    monkeypatch.setattr(run_module, "resume_queued", resume)
    assert hasattr(generation_service, "coordinate_runs"), "API/executor loop boundary is missing"
    await generation_service.coordinate_runs(runtime, manager, "active")
    consume.assert_not_awaited()
    resume.assert_not_awaited()


async def test_executor_tick_consumes_commands_before_resuming_queue(monkeypatch):
    import ttakkari.runs.manager as run_module
    from ttakkari import generation_service
    order = []
    async def consume(*_args):
        order.append("commands")
    async def resume():
        order.append("resume")
    monkeypatch.setattr(generation_service, "_commands", consume)
    monkeypatch.setattr(run_module, "resume_queued", resume)
    runtime = SimpleNamespace(role="executor", execution_mode="local")
    manager = RunManager(runtime=runtime)
    assert hasattr(generation_service, "coordinate_runs"), "executor loop is missing"
    await generation_service.coordinate_runs(runtime, manager, "active")
    assert order == ["commands", "resume"]


async def test_lost_fence_prevents_finish_side_effects():
    runtime = SimpleNamespace(role="executor", execution_mode="local",
                             fence=AsyncMock(side_effect=generation.OwnershipLost("stale")))
    manager = RunManager(runtime=runtime)
    rid = uuid.uuid4()
    manager._handles[rid] = _Handle(task=asyncio.current_task(), owner=runtime, token=uuid.uuid4())
    db = SimpleNamespace(info={})
    run = SimpleNamespace(id=rid, status="running")
    with pytest.raises(generation.OwnershipLost):
        await manager._finish(db, run, "failed")
    assert run.status == "running"


def test_remote_mode_requires_generation_lifecycle():
    from ttakkari.config import Settings
    with pytest.raises(ValueError, match="runtime_generation"):
        Settings(_env_file=None, run_executor_mode="remote", runtime_generation=False)


def test_flush_fence_blocks_stale_side_effect_before_autoflush():
    from sqlalchemy import (
        Column,
        Integer,
        MetaData,
        String,
        Table,
        create_engine,
        select,
    )
    from sqlalchemy.orm import Session

    from ttakkari.models import RuntimeLease

    engine = create_engine("sqlite://")
    RuntimeLease.__table__.create(engine)
    effects = Table("effect", MetaData(), Column("id", Integer, primary_key=True), Column("value", String))
    effects.create(engine)
    rid, owner, token = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    with Session(engine) as seed:
        seed.add(RuntimeLease(run_id=rid, generation_id=owner, token=token,
                              session_id=uuid.uuid4(), lane="workers"))
        seed.commit()
    # A mapped lease change stands for any ORM side effect (Run, event,
    # engine-session update) in the owner's session, before an implicit flush.
    with Session(engine) as writer:
        lease = writer.get(RuntimeLease, rid)
        writer.info["runtime_lease"] = (SimpleNamespace(id=owner), rid, uuid.uuid4())
        lease.lane = "desk"
        with pytest.raises(generation.OwnershipLost):
            writer.flush()
        writer.rollback()
    with Session(engine) as reader:
        assert reader.scalar(select(RuntimeLease.lane)) == "workers"
    with Session(engine) as writer:
        lease = writer.get(RuntimeLease, rid)
        writer.info["runtime_lease"] = (SimpleNamespace(id=owner), rid, token)
        lease.lane = "leads"
        writer.commit()
    with Session(engine) as reader:
        assert reader.scalar(select(RuntimeLease.lane)) == "leads"
    engine.dispose()


async def test_command_ack_waits_for_durable_terminal_state(monkeypatch):
    from ttakkari import generation_service
    rid, cid = uuid.uuid4(), uuid.uuid4()
    runtime = SimpleNamespace(commands=AsyncMock(return_value=[SimpleNamespace(kind="cancel", run_id=rid, id=cid)]),
                              ack=AsyncMock())
    manager = SimpleNamespace(_handles={rid: object()}, cancel=AsyncMock())
    class Query:
        async def __aenter__(self):
            return self
        async def __aexit__(self, *_):
            pass
        async def get(self, *_):
            return SimpleNamespace(status="running")
    monkeypatch.setattr(generation_service, "sessionmaker", lambda: Query)
    await generation_service._commands(runtime, manager)
    runtime.ack.assert_not_awaited()


async def test_desk_cold_child_is_owned_and_reaped_before_cancel_returns(tmp_path, monkeypatch):
    import ttakkari.runs.manager as run_module
    from ttakkari.config import get_settings
    from ttakkari.runs import desk

    assert hasattr(run_module, "attempt_handle"), "desk child ownership context is missing"
    monkeypatch.setattr(get_settings(), "home", tmp_path)
    monkeypatch.setattr(get_settings(), "desk_warm_pool", False)
    monkeypatch.setattr(desk, "_claude_cmd", lambda *_: [sys.executable, "-c", "import time; time.sleep(60)"])
    monkeypatch.setattr(desk, "_env", lambda _: {})
    handle = _Handle(task=asyncio.current_task())
    scope = run_module.attempt_handle.set(handle)
    try:
        task = asyncio.create_task(desk._ask(SimpleNamespace(engine="claude"), "system", "prompt"))
        for _ in range(100):
            if handle.proc is not None:
                break
            await asyncio.sleep(0.01)
        assert handle.proc is not None
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert handle.proc.returncode is not None
    finally:
        run_module.attempt_handle.reset(scope)
        if handle.proc and handle.proc.returncode is None:
            handle.proc.kill()
            await handle.proc.wait()


async def test_remote_api_cannot_claim_even_before_executor_activation(tmp_path, monkeypatch):
    runtime = generation.GenerationRuntime(tmp_path, revision="a"*40, api_url="http://127.0.0.1:18791")
    runtime.execution_mode = "remote"
    def unexpected():
        raise AssertionError("remote API entered the claim transaction")
    monkeypatch.setattr(generation, "sessionmaker", unexpected)
    assert await runtime.claim(uuid.uuid4(), "workers", 8) is None


@pytest.mark.parametrize("kinds,accepted", [(["cancel", "reset", "release_waiting"], True),
                                          (["cancel", "future-command"], False)])
def test_handoff_rejects_unknown_command_protocol(kinds, accepted):
    from ttakkari.executor import validate_handoff
    if accepted:
        validate_handoff(leases=0, apis=[(2, "remote")], pending_kinds=kinds)
    else:
        with pytest.raises(RuntimeError, match="command protocol"):
            validate_handoff(leases=0, apis=[(2, "remote")], pending_kinds=kinds)


async def test_exited_leader_does_not_authorize_release_with_live_descendants(monkeypatch):
    runtime = SimpleNamespace(role="executor", execution_mode="local", release=AsyncMock())
    manager = RunManager(runtime=runtime)
    rid, token = uuid.uuid4(), uuid.uuid4()
    manager._handles[rid] = _Handle(task=asyncio.current_task(),
        proc=SimpleNamespace(returncode=0, pid=987654), owner=runtime, token=token)
    monkeypatch.setattr(os, "killpg", lambda *_: None)  # group remains alive
    await manager._release_attempt(rid, runtime, token)
    runtime.release.assert_not_awaited()
