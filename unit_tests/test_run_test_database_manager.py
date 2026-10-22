from __future__ import annotations

import asyncio
import threading
import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import ClassVar

import pytest

from ttakkari.agents.base import LaunchSpec
from ttakkari.runs import test_database
from ttakkari.runs.manager import RunManager

URL = "postgresql+asyncpg://ttk_test:secret@127.0.0.1:55432/ttakkari_test"


class FakeTestDatabase:
    instances: ClassVar[list[FakeTestDatabase]] = []
    start_gate: threading.Event | None = None
    start_error: Exception | None = None

    def __init__(self, run_id, workspace, **_kwargs):
        self.run_id = run_id
        self.workspace = workspace
        self.started = False
        self.closed = 0
        FakeTestDatabase.instances.append(self)

    def start(self):
        if FakeTestDatabase.start_gate is not None:
            FakeTestDatabase.start_gate.wait(5)
        if FakeTestDatabase.start_error is not None:
            raise FakeTestDatabase.start_error
        self.started = True
        return URL

    def child_env(self):
        return {"TTAKKARI_TEST_DB": URL, "PGHOST": "127.0.0.1", "PGPORT": "55432"}

    def close(self):
        self.closed += 1


class FakeAdapter:
    engine = "codex"

    def build_command(self, spec, env):
        return ["fixture-engine"]


@pytest.fixture(autouse=True)
def provisioning_enabled(monkeypatch):
    monkeypatch.delenv("TTAKKARI_TEST_RUNNER", raising=False)
    monkeypatch.setattr(test_database, "RunTestDatabase", FakeTestDatabase)
    FakeTestDatabase.instances = []
    FakeTestDatabase.start_gate = None
    FakeTestDatabase.start_error = None


def spec() -> LaunchSpec:
    return LaunchSpec(
        cwd=Path("/workspace"), prompt="work", engine_session_id=None, model="m", effort="high",
        outbox=Path("/outbox"), write_roots=[Path("/workspace")], system_note="", session_id=str(uuid.uuid4()),
    )


def run():
    return SimpleNamespace(id=uuid.uuid4(), pid=None)


async def attempt(manager, env, the_run=None):
    return await manager._attempt(
        db=None, run=the_run or run(), adapter=FakeAdapter(), spec=spec(), env=env,
        root=Path("/workspace"), h=None,
    )


async def test_attempt_hands_only_the_owned_test_url_to_the_engine_and_closes_after_exit(monkeypatch):
    seen = {}

    async def no_op(*_args, **_kwargs):
        pass

    class Proc:
        pid = 4242
        returncode = 0
        stderr = None

        async def wait(self):
            return 0

    async def spawn(*_args, **kwargs):
        seen["env"] = dict(kwargs["env"])
        seen["open_during_spawn"] = FakeTestDatabase.instances[0].closed == 0
        return Proc()

    async def drained(*_args, **_kwargs):
        return None

    manager = RunManager()
    monkeypatch.setattr("ttakkari.runs.manager.asyncio.create_subprocess_exec", spawn)
    monkeypatch.setattr("ttakkari.runs.manager._drain_stderr", drained)
    monkeypatch.setattr(manager, "_commit", no_op)
    monkeypatch.setattr(manager, "_pump", no_op)
    await attempt(manager, {})

    (db,) = FakeTestDatabase.instances
    assert seen["env"]["TTAKKARI_TEST_DB"] == URL
    assert "TTAKKARI_DATABASE_URL" not in seen["env"]
    assert seen["open_during_spawn"]
    assert db.closed == 1


async def test_two_attempts_get_independent_databases(monkeypatch):
    async def denied(*_args, **_kwargs):
        raise FileNotFoundError("fixture")

    monkeypatch.setattr("ttakkari.runs.manager.asyncio.create_subprocess_exec", denied)
    manager = RunManager()
    await attempt(manager, {})
    await attempt(manager, {})

    first, second = FakeTestDatabase.instances
    assert first is not second
    assert first.closed == 1 and second.closed == 1


@pytest.mark.parametrize("failure", [FileNotFoundError("missing"), PermissionError("denied"),
                                      asyncio.CancelledError()])
async def test_spawn_failure_or_cancellation_closes_the_cluster(monkeypatch, failure):
    async def fail(*_args, **_kwargs):
        raise failure

    monkeypatch.setattr("ttakkari.runs.manager.asyncio.create_subprocess_exec", fail)
    manager = RunManager()
    if isinstance(failure, FileNotFoundError):
        result = await attempt(manager, {})
        assert result[3] is True
    else:
        with pytest.raises(type(failure)):
            await attempt(manager, {})

    (db,) = FakeTestDatabase.instances
    assert db.closed == 1


async def test_provisioning_failure_never_launches_the_engine_and_cleans_up(monkeypatch):
    spawned = []

    async def spawn(*_args, **_kwargs):
        spawned.append(True)

    monkeypatch.setattr("ttakkari.runs.manager.asyncio.create_subprocess_exec", spawn)
    FakeTestDatabase.start_error = RuntimeError("initdb failed")
    env: dict[str, str] = {}
    with pytest.raises(RuntimeError, match="initdb failed"):
        await attempt(RunManager(), env)

    (db,) = FakeTestDatabase.instances
    assert not spawned
    assert "TTAKKARI_TEST_DB" not in env
    assert db.closed == 1


async def test_cancellation_while_the_cluster_is_starting_waits_then_closes(monkeypatch):
    async def spawn(*_args, **_kwargs):
        raise AssertionError("engine must not start after cancellation")

    monkeypatch.setattr("ttakkari.runs.manager.asyncio.create_subprocess_exec", spawn)
    FakeTestDatabase.start_gate = threading.Event()
    task = asyncio.create_task(attempt(RunManager(), {}))
    for _ in range(100):
        if FakeTestDatabase.instances:
            break
        await asyncio.sleep(0.01)
    assert FakeTestDatabase.instances
    task.cancel()
    await asyncio.sleep(0.05)
    FakeTestDatabase.start_gate.set()
    with pytest.raises(asyncio.CancelledError):
        await task

    (db,) = FakeTestDatabase.instances
    assert db.started
    assert db.closed == 1


async def test_cancellation_during_pump_terminates_then_closes(monkeypatch):
    order = []

    class Proc:
        pid = 777
        returncode = None
        stderr = None

        async def wait(self):
            return 0

    async def spawn(*_args, **_kwargs):
        return Proc()

    async def drained(*_args, **_kwargs):
        return None

    async def no_op(*_args, **_kwargs):
        pass

    async def cancelled_pump(*_args, **_kwargs):
        raise asyncio.CancelledError

    async def terminate(_proc):
        order.append("terminate")

    manager = RunManager()
    monkeypatch.setattr("ttakkari.runs.manager.asyncio.create_subprocess_exec", spawn)
    monkeypatch.setattr("ttakkari.runs.manager._drain_stderr", drained)
    monkeypatch.setattr(manager, "_commit", no_op)
    monkeypatch.setattr(manager, "_pump", cancelled_pump)
    monkeypatch.setattr(manager, "_terminate", terminate)
    original_close = FakeTestDatabase.close

    def close(self):
        order.append("close")
        original_close(self)

    monkeypatch.setattr(FakeTestDatabase, "close", close)
    with pytest.raises(asyncio.CancelledError):
        await attempt(manager, {})

    assert order[-1] == "close" and "terminate" in order
    (db,) = FakeTestDatabase.instances
    assert db.closed == 1


async def test_test_runner_process_does_not_provision_nested_clusters(monkeypatch):
    monkeypatch.setenv("TTAKKARI_TEST_RUNNER", "1")

    async def denied(*_args, **_kwargs):
        raise FileNotFoundError("fixture")

    monkeypatch.setattr("ttakkari.runs.manager.asyncio.create_subprocess_exec", denied)
    await attempt(RunManager(), {})

    assert FakeTestDatabase.instances == []
