"""FND-01 Foundation regression tests:
1. TTS accepted work persistence (chunks_json in DB, survives memory eviction)
2. 2-tier drain contract (Admission rejected vs Accepted Work drained, zero generation_draining)
3. Restart reconcile without blind replay (auto_requeued_count == 0, ALIVE/ABSENT/UNKNOWN preserved)
"""
from __future__ import annotations

import asyncio
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import psutil
import pytest

from ttakkari.generation import GenerationRuntime
from ttakkari.models import Run, RunStatus
from ttakkari.tts_models import TtsJob


# -------------------------------------------------------------------------
# 1. TTS Chunks Persistence & Memory Eviction Recovery
# -------------------------------------------------------------------------
def test_tts_job_model_has_chunks_json_column():
    """TtsJob ORM model must expose chunks_json column (expand-only JSONB)."""
    assert hasattr(TtsJob, "chunks_json"), "TtsJob must have chunks_json column"
    column = TtsJob.__table__.c.get("chunks_json")
    assert column is not None, "tts_jobs table must have chunks_json column"
    assert column.nullable is True, "chunks_json column must be nullable (expand-only)"


@pytest.mark.asyncio
async def test_tts_run_claimed_recovers_from_chunks_json_when_memory_evicted(monkeypatch):
    """When _pending_texts is evicted (e.g., restart), run_claimed restores from chunks_json instead of failing with input_missing."""
    from ttakkari.tts import jobs

    job_id = uuid.uuid4()
    owner = jobs.owner_id()
    chunks = ["chunk one", "chunk two"]
    keys = ["k1", "k2"]

    job = TtsJob(
        id=job_id,
        owner_id=owner,
        status="running",
        params={"chunk_keys": keys, "voice": "v1", "speed": 1.0, "lang": "ko", "text_length": 18},
        chunks_json=chunks,
    )

    class FakeSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def get(self, model, jid, **kwargs):
            if model is TtsJob and jid == job_id:
                return job
            return None

    cas_calls = []

    async def fake_cas(jid, *, expected, status, owner=None, error_code=None, metadata=None):
        cas_calls.append({"jid": jid, "status": status, "error_code": error_code, "metadata": metadata})
        return True

    monkeypatch.setattr(jobs, "sessionmaker", lambda: (lambda: FakeSession()))
    monkeypatch.setattr(jobs, "cas", fake_cas)
    monkeypatch.setattr(jobs.cache, "lookup_many", AsyncMock(return_value={"k1": SimpleNamespace(duration_seconds=1.0), "k2": SimpleNamespace(duration_seconds=1.0)}))

    # Ensure memory is cleared (evicted)
    jobs._pending_texts.pop(job_id, None)

    # run_claimed should NOT fail with input_missing; it should read job.chunks_json
    await jobs.run_claimed(job_id)

    # It should succeed rather than fail with input_missing
    failed_calls = [c for c in cas_calls if c["status"] == "failed" and c["error_code"] == "input_missing"]
    assert not failed_calls, f"Job unexpectedly failed with input_missing: {failed_calls}"
    succeeded_calls = [c for c in cas_calls if c["status"] == "succeeded"]
    assert len(succeeded_calls) == 1, f"Expected 1 succeeded call, got {cas_calls}"


# -------------------------------------------------------------------------
# 2. 2-Tier Drain Contract: Admission vs Accepted Work
# -------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_generation_runtime_admission_vs_accepted_work(tmp_path, monkeypatch):
    """Admission is allowed only when active; accepted work drain is allowed in active and draining."""
    runtime = GenerationRuntime(tmp_path, revision="rev1", api_url="http://127.0.0.1:8791")
    runtime.id = uuid.uuid4()

    # Stub refresh to return current local state
    async def fake_refresh():
        return runtime.state

    monkeypatch.setattr(runtime, "refresh", fake_refresh)

    # State: active
    runtime.state = "active"
    assert await runtime.enter_admission("http") is True
    async with runtime.enter_accepted_work("tts", accepted_generation_id=runtime.id) as ok:
        assert ok is True

    # State: draining
    runtime.state = "draining"
    # Admission must be rejected during draining
    assert await runtime.enter_admission("http") is False

    # Accepted work belonging to this generation MUST be allowed during draining
    async with runtime.enter_accepted_work("tts", accepted_generation_id=runtime.id) as ok:
        assert ok is True

    # Accepted work belonging to another generation must be rejected
    other_gen = uuid.uuid4()
    async with runtime.enter_accepted_work("tts", accepted_generation_id=other_gen) as ok:
        assert ok is False


@pytest.mark.asyncio
async def test_generation_can_retire_holds_while_tts_accepted_work_pending(tmp_path, monkeypatch):
    """can_retire() must return False while draining if active accepted work or pending tts jobs remain."""
    runtime = GenerationRuntime(tmp_path, revision="rev1", api_url="http://127.0.0.1:8791")
    runtime.id = uuid.uuid4()
    runtime.state = "draining"

    async def fake_refresh():
        return runtime.state

    async def fake_snapshot():
        return {
            "generation": str(runtime.id),
            "state": "draining",
            "owned_runs": 0,
            "work": [],
            "http_inflight": 0,
        }

    monkeypatch.setattr(runtime, "refresh", fake_refresh)
    monkeypatch.setattr(runtime, "snapshot", fake_snapshot)

    # 1. When in-flight accepted work is tracked, can_retire must be False
    async with runtime.enter_accepted_work("tts", accepted_generation_id=runtime.id):
        assert await runtime.can_retire() is False

    # 2. When no in-flight work and no pending DB jobs, can_retire must be True
    assert await runtime.can_retire() is True


@pytest.mark.asyncio
async def test_tts_guard_loop_does_not_fail_queued_on_draining(monkeypatch):
    """When generation transitions to draining, guard_loop must NOT execute _fail_queued_draining."""
    from ttakkari.tts import jobs

    gen_id = uuid.uuid4()
    mock_runtime = MagicMock()
    mock_runtime.id = gen_id
    mock_runtime.refresh = AsyncMock(return_value="draining")

    monkeypatch.setattr("ttakkari.generation.current", mock_runtime)

    # Check that jobs._has_owned_queued returns False so the loop exits after one iteration
    has_queued_mock = AsyncMock(side_effect=[True, False])
    monkeypatch.setattr(jobs, "_has_owned_queued", has_queued_mock)

    fail_owned_mock = AsyncMock()
    monkeypatch.setattr(jobs, "fail_owned", fail_owned_mock)

    # Run one pass of _guard_loop
    task = asyncio.create_task(jobs._guard_loop())
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    # fail_owned should NOT be called with error_code="generation_draining"
    drain_fails = [c for c in fail_owned_mock.call_args_list if c.kwargs.get("error_code") == "generation_draining"]
    assert len(drain_fails) == 0, f"Unexpected generation_draining failures: {drain_fails}"


# -------------------------------------------------------------------------
# 3. Restart Reconcile Without Blind Replay
# -------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_resume_interrupted_disables_unconditional_auto_requeue(monkeypatch):
    """resume_interrupted() must NOT blindly auto-requeue interrupted runs (auto_requeued_count == 0)."""
    from ttakkari.runs import manager

    class FakeResumeSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def scalars(self, *args, **kwargs):
            # Even if interrupted runs exist in DB
            interrupted_run = Run(
                id=uuid.uuid4(),
                session_id=uuid.uuid4(),
                status=RunStatus.interrupted,
                error="서버 종료로 중단",
                resume_attempts=0,
            )
            return [interrupted_run]

        async def scalar(self, *args, **kwargs):
            return None  # No pending cancel

        async def execute(self, *args, **kwargs):
            mock_res = MagicMock()
            mock_res.scalars.return_value = []
            return mock_res

        async def commit(self):
            pass

    monkeypatch.setattr(manager, "sessionmaker", lambda: (lambda: FakeResumeSession()))

    # Default call without force must return 0 and never requeue runs blindly
    count = await manager.resume_interrupted()
    assert count == 0, f"Expected 0 auto-requeued runs, got {count}"

    # Explicit force=True allows resuming
    monkeypatch.setattr(manager, "_interrupted_handoff", AsyncMock(return_value="handoff"))
    monkeypatch.setattr(manager.events, "append", lambda *args, **kwargs: None)
    monkeypatch.setattr(manager.manager, "_arm", lambda *args, **kwargs: None)
    count_forced = await manager.resume_interrupted(force=True)
    assert count_forced == 1, f"Expected 1 forced-requeued run, got {count_forced}"


@pytest.mark.asyncio
async def test_recover_interrupted_preserves_alive_and_unknown_process_states(monkeypatch):
    """recover_interrupted must classify ALIVE, ABSENT, UNKNOWN:
    - ALIVE: process alive -> preserved (skipped_live_lease)
    - UNKNOWN: psutil.AccessDenied / OSError -> preserved (skipped_unknown)
    - ABSENT: NoSuchProcess -> marked interrupted
    """
    from ttakkari.runs import manager

    run_alive = Run(id=uuid.uuid4(), status=RunStatus.running, pid=1001, prompt="p1")
    run_unknown = Run(id=uuid.uuid4(), status=RunStatus.running, pid=1002, prompt="p2")
    run_absent = Run(id=uuid.uuid4(), status=RunStatus.running, pid=1003, prompt="p3")

    class FakeRecoverSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def scalar(self, *args, **kwargs):
            return None  # No runtime_control table active gen

        async def scalars(self, query, *args, **kwargs):
            query_str = str(query)
            if "run_events" in query_str:
                return []
            return [run_alive, run_unknown, run_absent]

        async def execute(self, *args, **kwargs):
            mock_res = MagicMock()
            mock_res.scalars.return_value = []
            return mock_res

        async def commit(self):
            pass

    monkeypatch.setattr(manager, "sessionmaker", lambda: (lambda: FakeRecoverSession()))
    monkeypatch.setattr(manager, "_interrupted_handoff", AsyncMock(return_value="handoff"))
    monkeypatch.setattr(manager.events, "append", lambda *args, **kwargs: None)

    def fake_process_info(pid):
        if pid == 1001:
            # ALIVE
            return "alive"
        elif pid == 1002:
            # UNKNOWN (e.g. AccessDenied)
            raise psutil.AccessDenied(pid=pid)
        elif pid == 1003:
            # ABSENT
            raise psutil.NoSuchProcess(pid=pid)
        raise ValueError(f"unexpected pid {pid}")

    monkeypatch.setattr(manager, "_inspect_run_process", fake_process_info)

    recovered = await manager.recover_interrupted()

    # Only run_absent should be marked interrupted
    assert recovered == 1, f"Expected 1 absent process recovered, got {recovered}"
    assert run_absent.status == RunStatus.interrupted
    assert run_alive.status == RunStatus.running, "ALIVE process must be preserved"
    assert run_unknown.status == RunStatus.running, "UNKNOWN process must be preserved"


# -------------------------------------------------------------------------
# ACC-01 & ACC-03 Acceptance Contract Tests
# -------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_acc01_tts_drain_e2e_contract(tmp_path, monkeypatch):
    """ACC-01 Contract:
    - Slot A active accepts jobs with persisted chunks_json.
    - Slot A transitions to draining. New admissions are rejected.
    - Slot A finishes in-flight and queued jobs to succeeded (0 generation_draining errors).
    - can_retire() is False until all jobs finish, then True.
    """
    from ttakkari.tts import jobs

    gen_a = GenerationRuntime(tmp_path / "a", revision="r1", api_url="http://127.0.0.1:8791")
    gen_a.id = uuid.uuid4()
    gen_a.state = "active"

    # 3 jobs belonging to gen_a
    job1 = TtsJob(id=uuid.uuid4(), owner_id=gen_a.id, status="running", params={"chunk_keys": ["k1"], "voice": "v", "speed": 1.0, "lang": "ko", "text_length": 5}, chunks_json=["chunk1"])
    job2 = TtsJob(id=uuid.uuid4(), owner_id=gen_a.id, status="queued", params={"chunk_keys": ["k2"], "voice": "v", "speed": 1.0, "lang": "ko", "text_length": 5}, chunks_json=["chunk2"])
    job3 = TtsJob(id=uuid.uuid4(), owner_id=gen_a.id, status="queued", params={"chunk_keys": ["k3"], "voice": "v", "speed": 1.0, "lang": "ko", "text_length": 5}, chunks_json=["chunk3"])
    all_jobs = {job1.id: job1, job2.id: job2, job3.id: job3}

    monkeypatch.setattr("ttakkari.generation.current", gen_a)
    monkeypatch.setattr(gen_a, "refresh", AsyncMock(side_effect=lambda: gen_a.state))

    # Mock DB operations
    class FakeSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def get(self, model, jid, **kwargs):
            return all_jobs.get(jid)

        async def scalar(self, query, *args, **kwargs):
            query_str = str(query)
            if "status IN" in query_str or "status = 'queued'" in query_str:
                has_active = any(j.owner_id == gen_a.id and j.status in ("queued", "running") for j in all_jobs.values())
                return uuid.uuid4() if has_active else None
            return None

        async def execute(self, *args, **kwargs):
            mock_res = MagicMock()
            mock_res.scalars.return_value = []
            return mock_res

        async def scalars(self, *args, **kwargs):
            return []

        async def commit(self):
            pass

    monkeypatch.setattr(jobs, "sessionmaker", lambda: (lambda: FakeSession()))
    monkeypatch.setattr("ttakkari.generation.sessionmaker", lambda: (lambda: FakeSession()))
    monkeypatch.setattr(jobs.cache, "lookup_many", AsyncMock(return_value={"k1": SimpleNamespace(duration_seconds=1.0), "k2": SimpleNamespace(duration_seconds=1.0), "k3": SimpleNamespace(duration_seconds=1.0)}))

    cas_errors = []

    async def tracking_cas(jid, *, expected, status, owner=None, error_code=None, metadata=None):
        if error_code:
            cas_errors.append(error_code)
        if jid in all_jobs:
            all_jobs[jid].status = status
        return True

    monkeypatch.setattr(jobs, "cas", tracking_cas)

    # 1. Slot A is active: enter_admission is True
    assert await gen_a.enter_admission() is True

    # 2. Slot A switches to draining
    gen_a.state = "draining"
    assert await gen_a.enter_admission() is False, "New admission must be rejected during draining"

    # can_retire must be False because job1, job2, job3 are queued/running
    assert await gen_a.can_retire() is False

    # Finish job1
    await jobs.run_claimed(job1.id)
    assert job1.status == "succeeded"
    assert await gen_a.can_retire() is False

    # Claim & run job2 and job3 in draining mode
    claimed_ids = [job2.id, job3.id]

    async def fake_claim(me):
        if claimed_ids:
            cid = claimed_ids.pop(0)
            all_jobs[cid].status = "running"
            return cid
        return None

    monkeypatch.setattr(jobs, "_claim", fake_claim)
    monkeypatch.setattr(jobs, "_has_owned_queued", AsyncMock(side_effect=lambda me: bool(claimed_ids)))

    # Run tick for job2
    worked = await jobs.tick()
    assert worked is True
    assert job2.status == "succeeded"
    assert await gen_a.can_retire() is False

    # Run tick for job3
    worked = await jobs.tick()
    assert worked is True
    assert job3.status == "succeeded"

    # All 3 jobs succeeded with 0 generation_draining errors
    assert len([e for e in cas_errors if e == "generation_draining"]) == 0
    assert job1.status == "succeeded"
    assert job2.status == "succeeded"
    assert job3.status == "succeeded"

    # Now that all jobs are succeeded and no inflight accepted work remains, can_retire becomes True!
    assert await gen_a.can_retire() is True


@pytest.mark.asyncio
async def test_acc03_restart_reconcile_without_blind_replay(monkeypatch):
    """ACC-03 Contract:
    - Dead process (ABSENT) gets reconciled to interrupted.
    - Live process (ALIVE) and AccessDenied (UNKNOWN) are preserved.
    - Subsequent restart / resume_interrupted NEVER auto-requeues (auto_requeued_count == 0).
    """
    from ttakkari.runs import manager

    # 1. Verify that recover_interrupted marks absent process as interrupted
    interrupted_run = Run(
        id=uuid.uuid4(),
        session_id=uuid.uuid4(),
        status=RunStatus.running,
        pid=9999,
        prompt="do something",
    )

    class FakeAcc03Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def scalar(self, *args, **kwargs):
            return None

        async def scalars(self, query, *args, **kwargs):
            query_str = str(query)
            if "run_events" in query_str:
                return []
            return [interrupted_run]

        async def execute(self, *args, **kwargs):
            mock_res = MagicMock()
            mock_res.scalars.return_value = []
            return mock_res

        async def commit(self):
            pass

    monkeypatch.setattr(manager, "sessionmaker", lambda: (lambda: FakeAcc03Session()))
    monkeypatch.setattr(manager, "_interrupted_handoff", AsyncMock(return_value="handoff"))
    monkeypatch.setattr(manager.events, "append", lambda *args, **kwargs: None)
    monkeypatch.setattr(manager, "_inspect_run_process", lambda pid: "absent")

    reconciled = await manager.recover_interrupted()
    assert reconciled == 1
    assert interrupted_run.status == RunStatus.interrupted

    # 2. Verify that resume_interrupted on restart does NOT auto-requeue
    requeued = await manager.resume_interrupted()
    assert requeued == 0, f"ACC-03 violated: auto_requeued_count must be 0, got {requeued}"
    assert interrupted_run.status == RunStatus.interrupted, "Run status must remain interrupted, not queued"

