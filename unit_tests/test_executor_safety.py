"""Executor lifecycle regression checks. All OS/process/launchd operations are fakes."""
from __future__ import annotations

import asyncio
import json
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from ttakkari import executor, generation
from ttakkari import generation_deployment as deployment
from ttakkari.models import utcnow
from ttakkari.runs.manager import RunManager, _Handle


def database(monkeypatch, module, *, row=None, leases=0, apis=None, pending=()):
    db = AsyncMock()
    control = SimpleNamespace(executor_generation=row.id if row else None)
    db.get.side_effect = lambda model, key: control if model is deployment.RuntimeControl else row
    result = MagicMock()
    result.all.return_value = [(2, "remote")] if apis is None else apis
    db.execute.return_value = result
    db.scalar.return_value = leases
    db.scalars.return_value = pending
    context = MagicMock()
    context.__aenter__ = AsyncMock(return_value=db)
    context.__aexit__ = AsyncMock(return_value=None)
    monkeypatch.setattr(module, "sessionmaker", lambda: lambda: context)
    return db


def pinned_release(root, revision):
    release = root / "releases" / revision
    (release / ".venv/bin").mkdir(parents=True)
    (release / ".venv/bin/python").touch()
    (release / ".release.json").write_text(json.dumps({"revision": revision}))
    return release


def command(release):
    return [str(release / ".venv/bin/python"), "-m", "ttakkari.cli", "executor", "--activate"]


@pytest.fixture
def process_environment(tmp_path, monkeypatch):
    old = "a" * 40
    release = pinned_release(tmp_path, old)
    row = SimpleNamespace(id=uuid.uuid4(), role="executor", state="active", protocol=2,
                          revision=old, pid=424242, process_started_at=100.0, heartbeat_at=utcnow())
    process = MagicMock()
    process.create_time.return_value = row.process_started_at
    process.cmdline.return_value = command(release)
    process.is_running.return_value = True
    monkeypatch.setattr(deployment.psutil, "Process", lambda pid: process)
    monkeypatch.setattr(deployment.os, "kill", MagicMock())
    monkeypatch.setattr(deployment.Releases, "contract", lambda path: {"run_executor": {"activation": "enabled"}})
    monkeypatch.setattr(deployment.Slots, "executor_spec", staticmethod(lambda path, revision: (command(path), {})))
    slots = deployment.Slots(tmp_path)
    slots._launchctl = AsyncMock()
    database(monkeypatch, deployment, row=row)
    return slots, row, process


async def test_ownership_loss_never_reports_clean_drain_with_live_child(tmp_path, monkeypatch):
    runtime = executor.ExecutorRuntime(tmp_path, revision="a" * 40)
    runtime.state = "active"
    runtime.refresh = AsyncMock(side_effect=generation.OwnershipLost("pointer changed"))
    runtime.commands = AsyncMock(return_value=[])
    manager = RunManager(runtime=runtime)
    task = asyncio.create_task(asyncio.Event().wait())
    rid = uuid.uuid4()
    manager._handles[rid] = _Handle(task=task, proc=SimpleNamespace(returncode=None), owner=runtime)
    database(monkeypatch, executor, row=SimpleNamespace(state="active", id=runtime.id))
    try:
        assert await executor.drain(runtime, manager, poll_interval=0.001, timeout=0.02) is False
        assert rid in manager._handles and not task.done()
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_ownership_loss_waits_for_natural_completion(tmp_path, monkeypatch):
    runtime = executor.ExecutorRuntime(tmp_path, revision="a" * 40)
    runtime.state = "active"
    runtime.refresh = AsyncMock(side_effect=generation.OwnershipLost("pointer changed"))
    runtime.commands = AsyncMock(return_value=[])
    manager = RunManager(runtime=runtime)
    rid = uuid.uuid4()
    database(monkeypatch, executor, row=SimpleNamespace(state="active", id=runtime.id))

    async def complete():
        await asyncio.sleep(0.025)
        manager._handles.pop(rid)

    task = asyncio.create_task(complete())
    manager._handles[rid] = _Handle(task=task, owner=runtime)
    try:
        assert await executor.drain(runtime, manager, poll_interval=0.001, timeout=1) is True
        assert task.done() and not manager._handles
    finally:
        await task


@pytest.mark.parametrize("state", ["active", "offline"])
async def test_serve_drain_timeout_is_not_permission_to_shutdown(tmp_path, monkeypatch, state):
    from ttakkari.artifacts import preview
    from ttakkari.meetings.local_stt import jobs
    from ttakkari.runs import manager as managers
    from ttakkari.runs import profiles, usage_store

    runtime = SimpleNamespace(state=state, register=AsyncMock(), listen=AsyncMock(), close=AsyncMock())
    monkeypatch.setattr(executor, "get_settings", lambda: SimpleNamespace(home=tmp_path, release_revision="a" * 40))
    monkeypatch.setattr(executor, "ExecutorRuntime", lambda *args, **kwargs: runtime)
    monkeypatch.setattr("ttakkari.deployment.revision", lambda: "a" * 40)
    monkeypatch.setattr(executor, "start_background_services", AsyncMock(return_value=[]))
    drain = AsyncMock(side_effect=[False, True])
    monkeypatch.setattr(executor, "drain", drain)
    monkeypatch.setattr(executor, "dispose_engine", AsyncMock())
    monkeypatch.setattr(managers.manager, "shutdown", AsyncMock())
    monkeypatch.setattr(profiles, "flush_persist", AsyncMock())
    monkeypatch.setattr(usage_store, "install", lambda: None)
    monkeypatch.setattr(usage_store, "flush", AsyncMock())
    monkeypatch.setattr(preview, "stop_worker", AsyncMock())
    monkeypatch.setattr(jobs, "stop", AsyncMock())
    loop = asyncio.get_running_loop()
    monkeypatch.setattr(loop, "add_signal_handler", lambda sig, callback: callback())
    monkeypatch.setattr(loop, "remove_signal_handler", lambda sig: None)
    monkeypatch.setattr(generation, "current", None)
    await executor.serve(drain_timeout=0)
    assert drain.await_count == 2
    assert drain.await_args_list[1].kwargs["timeout"] is None


async def test_api_revision_change_preserves_healthy_executor(process_environment):
    slots, row, process = process_environment
    slots.stop_executor = AsyncMock(side_effect=AssertionError("healthy executor must survive API-only revision"))
    newer = pinned_release(slots.root, "b" * 40)
    ready = await slots.ensure_executor(newer, "b" * 40)
    assert ready["revision"] == row.revision
    slots.stop_executor.assert_not_awaited()
    slots._launchctl.assert_not_awaited()
    process.send_signal.assert_not_called()
    deployment.os.kill.assert_not_called()


@pytest.mark.parametrize(("leases", "apis", "pending", "message"), [
    (1, [(2, "remote")], (), "lease boundary"),
    (0, [(1, "local")], (), "protocol2 remote"),
    (0, [(2, "remote")], ("unknown",), "unknown pending command"),
])
async def test_activation_checks_gates_before_stopping(tmp_path, monkeypatch, leases, apis, pending, message):
    release = pinned_release(tmp_path, "b" * 40)
    slots = deployment.Slots(tmp_path)
    slots.executor_snapshot = AsyncMock(return_value=None)
    slots.stop_executor = AsyncMock()
    monkeypatch.setattr(deployment.Releases, "contract", lambda path: {"run_executor": {"activation": "enabled"}})
    monkeypatch.setattr(deployment.Slots, "executor_spec", staticmethod(lambda path, rev: (command(path), {})))
    database(monkeypatch, deployment, leases=leases, apis=apis, pending=pending)
    with pytest.raises(RuntimeError, match=message):
        await slots.ensure_executor(release, "b" * 40)
    slots.stop_executor.assert_not_awaited()


async def test_stop_timeout_never_boots_out_live_executor(process_environment):
    slots, _row, _process = process_environment
    with pytest.raises(RuntimeError, match="drain|running|exit"):
        await slots.stop_executor(timeout=0)
    slots._launchctl.assert_not_awaited()


@pytest.mark.parametrize("identity", ["pid_reused", "wrong_command", "unobservable"])
async def test_stop_refuses_unverified_identity(process_environment, identity):
    slots, row, process = process_environment
    if identity == "pid_reused":
        process.create_time.return_value = row.process_started_at + 100
    elif identity == "wrong_command":
        process.cmdline.return_value = ["python", "-m", "unrelated"]
    else:
        process.cmdline.side_effect = deployment.psutil.AccessDenied(row.pid)
    with pytest.raises(RuntimeError):
        await slots.stop_executor(timeout=0)
    slots._launchctl.assert_not_awaited()
    process.send_signal.assert_not_called()
    deployment.os.kill.assert_not_called()


async def test_stop_boots_out_only_after_matching_process_exits(process_environment):
    slots, _row, process = process_environment
    process.is_running.side_effect = [True, False]
    await slots.stop_executor(timeout=1)
    process.send_signal.assert_called_once_with(deployment.signal.SIGTERM)
    slots._launchctl.assert_awaited_once()


async def test_stop_without_database_owner_does_not_touch_launchd(tmp_path, monkeypatch):
    slots = deployment.Slots(tmp_path)
    slots._launchctl = AsyncMock()
    database(monkeypatch, deployment)
    await slots.stop_executor()
    slots._launchctl.assert_not_awaited()


async def test_snapshot_rejects_command_from_another_release(process_environment):
    slots, row, process = process_environment
    other = pinned_release(slots.root, "b" * 40)
    process.cmdline.return_value = command(other)
    assert await slots.executor_snapshot(revision=row.revision) is None


async def test_snapshot_rejects_tampered_release_marker(process_environment):
    slots, row, _process = process_environment
    marker = slots.root / "releases" / row.revision / ".release.json"
    marker.write_text(json.dumps({"revision": "b" * 40}))
    assert await slots.executor_snapshot(revision=row.revision) is None


async def test_explicit_executor_replacement_remains_separate_from_api_revision(process_environment):
    slots, _row, _process = process_environment
    target = "b" * 40
    release = pinned_release(slots.root, target)
    slots.executor_snapshot = AsyncMock(side_effect=[None, {"ready": True, "revision": target}])
    slots.stop_executor = AsyncMock()
    ready = await slots.ensure_executor(release, target, replace=True)
    assert ready["revision"] == target
    slots.stop_executor.assert_awaited_once()
    assert slots.executor_snapshot.await_args_list[0].kwargs["revision"] == target
    assert slots._launchctl.await_args.args[0] == "bootstrap"
