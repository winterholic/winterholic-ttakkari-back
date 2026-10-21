"""Database-free safety checks for the stop fence and PID proof primitives."""
from __future__ import annotations

import asyncio
import os
import sys
import uuid
from pathlib import Path
from types import SimpleNamespace

from ttakkari.runs import agent_control
from ttakkari.runs.agent_control import (
    _terminate_identified_run,
    fence_keys,
    run_fence_is_current,
)


def test_stop_fence_keys_keep_personal_and_workspaces_separate():
    workspace_a, workspace_b = uuid.uuid4(), uuid.uuid4()
    work_a = fence_keys("work", workspace_a)
    work_b = fence_keys("work", workspace_b)
    personal = fence_keys("personal", None)
    assert f"workspace:{workspace_a}" in work_a
    assert f"workspace:{workspace_b}" in work_b
    assert "pool:work" in work_a and "pool:work" in work_b
    assert "pool:personal" in personal
    assert "pool:all" not in work_a + work_b + personal
    assert set(work_a).isdisjoint(set(work_b) - {"pool:work"})


def test_only_exact_post_stop_epoch_snapshot_is_dispatchable():
    before = {"pool:all": 0, "pool:personal": 0, "session:x": 0}
    stopped = {"pool:all": 0, "pool:personal": 1, "session:x": 0}
    resumed = {"pool:all": 0, "pool:personal": 2, "session:x": 0}
    assert run_fence_is_current({}, before)  # legacy runs predate the first fence
    assert run_fence_is_current(stopped, stopped)
    assert not run_fence_is_current(before, stopped)
    assert not run_fence_is_current(stopped, resumed)


def test_missing_pid_identity_never_signals_a_live_fake_child():
    async def scenario():
        child_script = Path(__file__).parent / "fake_agents" / "fake_claude_sleep.py"
        child = await asyncio.create_subprocess_exec(
            sys.executable, str(child_script), start_new_session=True,
        )
        try:
            await asyncio.sleep(0.1)
            run = SimpleNamespace(pid=child.pid, pid_started_at=None, engine="claude")
            stopped, reason = await _terminate_identified_run(run, manager=None)
            assert not stopped
            assert "식별 정보" in reason
            assert child.returncode is None
            os.kill(child.pid, 0)
        finally:
            if child.returncode is None:
                child.terminate()
                await child.wait()

    asyncio.run(scenario())


def test_pid_reuse_birth_time_mismatch_does_not_signal_fake_child():
    async def scenario():
        child_script = Path(__file__).parent / "fake_agents" / "fake_claude_sleep.py"
        child = await asyncio.create_subprocess_exec(
            sys.executable, str(child_script), start_new_session=True,
        )
        original_identity = agent_control._process_identity
        signalled = []

        async def reused_pid(_pid):
            return (100, child.pid, f"{sys.executable} {child_script}")

        original_killpg = os.killpg

        def record_signal(pid, sig):
            signalled.append((pid, sig))

        agent_control._process_identity = reused_pid
        os.killpg = record_signal
        try:
            run = SimpleNamespace(pid=child.pid, pid_started_at=99.0, engine="claude")
            stopped, reason = await _terminate_identified_run(run, manager=None)
            assert not stopped
            assert "PID 재사용" in reason
            assert signalled == []
            assert child.returncode is None
        finally:
            agent_control._process_identity = original_identity
            os.killpg = original_killpg
            if child.returncode is None:
                child.terminate()
                await child.wait()

    asyncio.run(scenario())
