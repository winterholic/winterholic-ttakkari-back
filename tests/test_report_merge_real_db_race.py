"""Real-PostgreSQL two-transaction race checks for report merge vs generation claim (lock order: ChatSession -> queued Run)."""
from __future__ import annotations

import asyncio
import uuid
from pathlib import Path

import pytest

import ttakkari.generation as generation_module
from ttakkari.db import sessionmaker
from ttakkari.generation import GenerationRuntime
from ttakkari.models import (
    ChatSession,
    Run,
    RunOrigin,
    RunStatus,
    RuntimeControl,
    RuntimeGeneration,
    RuntimeLease,
    SessionKind,
    Workspace,
)
from ttakkari.runs.manager import _enqueue_report


async def _seed():
    workspace_id = uuid.uuid4()
    parent_id = uuid.uuid4()
    worker_id = uuid.uuid4()
    report_id = uuid.uuid4()
    completion_id = uuid.uuid4()
    generation_id = uuid.uuid4()
    async with sessionmaker()() as db:
        db.add(Workspace(id=workspace_id, name="race", root_path=f"/tmp/report-race-{uuid.uuid4()}"))
        await db.flush()
        db.add(ChatSession(
            id=parent_id, workspace_id=workspace_id, kind=SessionKind.orchestrator,
            title="parent", engine="claude", model="test-model",
        ))
        db.add(ChatSession(
            id=worker_id, workspace_id=workspace_id, kind=SessionKind.worker,
            parent_id=parent_id, title="race-worker", engine="claude", model="test-model",
        ))
        db.add(Run(
            id=report_id, session_id=parent_id, workspace_id=workspace_id,
            prompt="existing queued report", engine="claude", model="test-model",
            status=RunStatus.queued, origin=RunOrigin.report,
        ))
        db.add(Run(
            id=completion_id, session_id=worker_id, workspace_id=workspace_id,
            prompt="worker prompt", engine="claude", model="test-model",
            status=RunStatus.succeeded, origin=RunOrigin.orchestrator,
            result_text="fresh worker result", last_seq=0,
        ))
        db.add(RuntimeGeneration(
            id=generation_id, revision="a" * 40, role="executor", protocol=2,
            execution_mode="local", api_url="http://127.0.0.1:18791",
            pid=1, process_started_at=1.0, state="active",
        ))
        await db.flush()
        db.add(RuntimeControl(
            id=1, active_generation=generation_id, executor_generation=generation_id, epoch=1,
        ))
        await db.commit()
    runtime = GenerationRuntime(Path("/tmp"), revision="a" * 40, api_url="http://127.0.0.1:18791")
    runtime.id = generation_id
    runtime.role = "executor"
    runtime.execution_mode = "local"
    return runtime, workspace_id, parent_id, worker_id, report_id, completion_id


async def _enqueue(worker_id, completion_id):
    async with sessionmaker()() as db:
        worker = await db.get(ChatSession, worker_id)
        completion = await db.get(Run, completion_id)
        await _enqueue_report(db, worker, completion)
        await db.commit()


class _ClaimSession:
    def __init__(self, inner, *, first_read=None, commit_entered=None, release_commit=None, run_id=None):
        self.inner = inner
        self.first_read = first_read
        self.commit_entered = commit_entered
        self.release_commit = release_commit
        self.run_id = run_id

    async def __aenter__(self):
        await self.inner.__aenter__()
        return self

    async def __aexit__(self, *args):
        return await self.inner.__aexit__(*args)

    async def get(self, entity, ident):
        value = await self.inner.get(entity, ident)
        if entity is Run and ident == self.run_id and self.first_read is not None:
            self.first_read.set()
        return value

    async def scalar(self, stmt):
        return await self.inner.scalar(stmt)

    async def execute(self, stmt):
        return await self.inner.execute(stmt)

    def add(self, value):
        self.inner.add(value)

    async def commit(self):
        if self.commit_entered is not None:
            self.commit_entered.set()
            await self.release_commit.wait()
        await self.inner.commit()


@pytest.mark.asyncio
async def test_real_db_merge_lock_precedes_claim_and_claim_reads_merged_prompt(monkeypatch):
    runtime, _, parent_id, worker_id, report_id, completion_id = await _seed()
    merge_done, allow_merge_commit = asyncio.Event(), asyncio.Event()
    claim_read = asyncio.Event()
    real_factory = sessionmaker()

    def wrapped_sessionmaker():
        return lambda: _ClaimSession(real_factory(), first_read=claim_read, run_id=report_id)

    monkeypatch.setattr(generation_module, "sessionmaker", wrapped_sessionmaker)

    async with sessionmaker()() as merge_db:
        worker = await merge_db.get(ChatSession, worker_id)
        completion = await merge_db.get(Run, completion_id)
        await _enqueue_report(merge_db, worker, completion)
        merge_done.set()  # Real parent and queued-Run row locks are held until commit.
        claim_task = asyncio.create_task(runtime.claim(report_id, "leads", 2))
        await asyncio.wait_for(claim_read.wait(), timeout=3)
        await asyncio.sleep(0.05)
        assert not claim_task.done(), "claim should wait on the parent's row lock while merge is uncommitted"
        await merge_db.commit()

    token = await asyncio.wait_for(claim_task, timeout=3)
    assert token is not None
    async with sessionmaker()() as db:
        report = await db.get(Run, report_id)
        lease = await db.get(RuntimeLease, report_id)
        assert lease is not None
        assert "existing queued report" in report.prompt
        assert "fresh worker result" in report.prompt
        assert len(list(await db.scalars(
            __import__("sqlalchemy").select(Run).where(
                Run.session_id == parent_id, Run.origin == RunOrigin.report,
            )
        ))) == 1


@pytest.mark.asyncio
async def test_real_db_claim_lock_precedes_enqueue_and_new_report_is_separate(monkeypatch):
    runtime, _, parent_id, worker_id, report_id, completion_id = await _seed()
    claim_commit_entered, allow_claim_commit = asyncio.Event(), asyncio.Event()
    enqueue_done = asyncio.Event()
    real_factory = sessionmaker()

    def wrapped_sessionmaker():
        return lambda: _ClaimSession(
            real_factory(), commit_entered=claim_commit_entered,
            release_commit=allow_claim_commit, run_id=report_id,
        )

    monkeypatch.setattr(generation_module, "sessionmaker", wrapped_sessionmaker)
    claim_task = asyncio.create_task(runtime.claim(report_id, "leads", 2))
    await asyncio.wait_for(claim_commit_entered.wait(), timeout=3)  # Locks held; lease insert awaits commit.

    async def enqueue_and_commit():
        await _enqueue(worker_id, completion_id)
        enqueue_done.set()

    enqueue_task = asyncio.create_task(enqueue_and_commit())
    await asyncio.sleep(0.05)
    assert not enqueue_done.is_set(), "enqueue should wait on the parent's row lock while claim is uncommitted"
    allow_claim_commit.set()
    token = await asyncio.wait_for(claim_task, timeout=3)
    assert token is not None
    await asyncio.wait_for(enqueue_task, timeout=3)

    async with sessionmaker()() as db:
        original = await db.get(Run, report_id)
        reports = list(await db.scalars(
            __import__("sqlalchemy").select(Run).where(
                Run.session_id == parent_id, Run.origin == RunOrigin.report,
            ).order_by(Run.created_at, Run.id)
        ))
        assert await db.get(RuntimeLease, report_id) is not None
        assert original.prompt == "existing queued report"
        assert len(reports) == 2
        assert "fresh worker result" in reports[1].prompt
