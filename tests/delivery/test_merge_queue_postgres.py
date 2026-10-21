"""Real PostgreSQL locking and crash recovery tests for the delivery queue.

These tests intentionally use the process-isolated PostgreSQL fixture from
tests/conftest.py and independent sessions. SQLite cannot establish these
locking or recovery guarantees.
"""
from __future__ import annotations

import asyncio
import importlib.util
import uuid
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import select, text

from ttakkari.db import sessionmaker
from ttakkari.delivery.merge_queue import claim_next_change, process_single_change
from ttakkari.delivery.provenance import register_change_set
from ttakkari.delivery.worktree import create_session_worktree, run_git
from ttakkari.delivery_models import ChangeSet, ChangeSetStatus


@pytest.fixture
def session_factory():
    return sessionmaker()


@pytest.fixture
def delivery_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    for args in (["init", "-b", "main"], ["config", "user.name", "Delivery Test"],
                 ["config", "user.email", "delivery-test@example.invalid"]):
        result = run_git(list(args), repo)
        assert result.returncode == 0, result.stderr
    (repo / "README.md").write_text("base\n", encoding="utf-8")
    assert run_git(["add", "README.md"], repo).returncode == 0
    assert run_git(["commit", "-m", "base"], repo).returncode == 0
    return repo


async def _change(repo: Path, root: Path, name: str) -> tuple[ChangeSet, Path]:
    session_id = uuid.uuid4()
    wt = create_session_worktree(repo, session_id, worktrees_root=root / "worktrees")
    (wt.worktree_path / f"{name}.txt").write_text(name + "\n", encoding="utf-8")
    assert run_git(["add", f"{name}.txt"], wt.worktree_path).returncode == 0
    assert run_git(["commit", "-m", f"{name}\n\nTtakkari-Session: {session_id}"], wt.worktree_path).returncode == 0
    head = run_git(["rev-parse", "HEAD"], wt.worktree_path).stdout.strip()
    async with sessionmaker()() as db:
        cs = await register_change_set(db, session_id=session_id, run_id=None,
            repo_path=str(repo), branch_name=wt.branch_name, worktree_path=str(wt.worktree_path),
            base_sha=wt.base_sha, target_branch="main")
        cs.head_sha = head
        cs.status = ChangeSetStatus.submitted
        await db.commit()
        return cs, wt.worktree_path


@pytest.mark.asyncio
async def test_postgres_skip_locked_claims_distinct_rows_across_sessions(session_factory):
    """A held row lock makes a competing real PostgreSQL claimant take another row."""
    ids = [uuid.uuid4(), uuid.uuid4()]
    async with session_factory() as setup:
        for ident in ids:
            setup.add(ChangeSet(id=ident, session_id=uuid.uuid4(), repo_path="/tmp/repo",
                branch_name=f"work/{ident}", worktree_path=f"/tmp/{ident}", base_sha="0" * 40,
                status=ChangeSetStatus.submitted, target_branch="main"))
        await setup.commit()

    async with session_factory() as first, session_factory() as second:
        a = await claim_next_change(first)
        b = await claim_next_change(second)
        assert a is not None and b is not None
        assert a.id != b.id
        # Keep the first transaction's row lock held while the second claim executes.
        await second.commit()
        await first.commit()


@pytest.mark.asyncio
async def test_same_target_integrators_serialize_and_second_reads_advanced_ref(
    delivery_repo, tmp_path, session_factory
):
    """The second integrator cannot validate against a stale target while the first owns its advisory lock."""
    first_cs, _ = await _change(delivery_repo, tmp_path, "first")
    second_cs, _ = await _change(delivery_repo, tmp_path, "second")
    entered = asyncio.Event()
    release = asyncio.Event()
    second_observed_first = []

    async def blocking_stage(name, candidate, sha):
        if sha != first_cs.head_sha:
            second_observed_first.append((candidate / "first.txt").exists())
        if sha == first_cs.head_sha and name == "stage1":
            entered.set()
            await release.wait()
        return True, f"{name} passed for {sha}", None

    async def run_change(change_id):
        async with session_factory() as db:
            change = await db.get(ChangeSet, change_id)
            return await process_single_change(db, change, delivery_repo, blocking_stage, blocking_stage)

    one = asyncio.create_task(run_change(first_cs.id))
    await asyncio.wait_for(entered.wait(), timeout=5)
    two = asyncio.create_task(run_change(second_cs.id))
    await asyncio.sleep(1.2)  # acquire loop polls the PostgreSQL advisory lock once per second
    assert not second_observed_first
    release.set()
    first_result, second_result = await asyncio.gather(one, two)
    assert first_result.success and second_result.success
    assert second_observed_first == [True, True]


@pytest.mark.asyncio
async def test_new_session_recovers_after_ref_cas_before_final_database_commit(
    delivery_repo, tmp_path, session_factory, monkeypatch
):
    """A crash immediately after update-ref leaves durable proof for idempotent recovery."""
    change, _ = await _change(delivery_repo, tmp_path, "recover")

    async def passed(name, _candidate, sha):
        return True, f"{name} passed for {sha}", None

    import ttakkari.delivery.merge_queue as queue
    real_run_git = queue.run_git
    crashed = False

    def fail_after_cas(args, cwd):
        nonlocal crashed
        result = real_run_git(args, cwd)
        if args and args[0] == "update-ref" and not crashed:
            crashed = True
            raise RuntimeError("injected crash after Git ref CAS")
        return result

    monkeypatch.setattr(queue, "run_git", fail_after_cas)
    with pytest.raises(RuntimeError, match="injected crash"):
        async with session_factory() as db:
            row = await db.get(ChangeSet, change.id)
            await process_single_change(db, row, delivery_repo, passed, passed)
    monkeypatch.setattr(queue, "run_git", real_run_git)

    promoted = run_git(["rev-parse", "main"], delivery_repo).stdout.strip()
    async with session_factory() as fresh:
        persisted = await fresh.scalar(select(ChangeSet).where(ChangeSet.id == change.id))
        assert persisted is not None and persisted.candidate_sha == promoted
        assert persisted.status == ChangeSetStatus.ready_for_merge
        recovered = await process_single_change(fresh, persisted, delivery_repo, passed, passed)
        assert recovered.success and recovered.status == ChangeSetStatus.merged
        assert recovered.rebased_sha == promoted
    assert run_git(["rev-parse", "main"], delivery_repo).stdout.strip() == promoted


@pytest.mark.asyncio
async def test_delivery_migration_round_trip_uses_postgres_jsonb(session_factory):
    """Run the migration itself in the disposable process database and check native JSONB columns."""
    path = Path(__file__).resolve().parents[2] / "alembic/versions/0062_delivery_change_sets.py"
    spec = importlib.util.spec_from_file_location("delivery_migration_0062", path)
    migration = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(migration)

    async with session_factory() as db:
        await db.execute(text("DROP TABLE IF EXISTS change_sets CASCADE"))
        conn = await db.connection()

        def run(sync, fn):
            with Operations.context(MigrationContext.configure(sync)):
                fn()

        await conn.run_sync(run, migration.upgrade)
        columns = {row.column_name: row.data_type for row in
                   (await db.execute(text("SELECT column_name, data_type FROM information_schema.columns WHERE table_name='change_sets'"))).all()}
        assert columns["stage1_result"] == "jsonb"
        assert columns["stage2_result"] == "jsonb"
        assert columns["signature_attempts"] == "jsonb"
        await conn.run_sync(run, migration.downgrade)
        assert await db.scalar(text("SELECT to_regclass('change_sets')")) is None
        await db.commit()
