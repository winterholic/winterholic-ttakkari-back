"""Unit tests for commit provenance and trailers (ADR 0066 D2)."""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from ttakkari.delivery.provenance import (
    attach_commit_trailers,
    format_commit_trailers,
    inspect_commit_provenance,
    parse_commit_trailers,
    register_change_set,
    verify_commit_provenance,
)
from ttakkari.delivery.worktree import run_git
from ttakkari.delivery_models import Base, ChangeSet, ChangeSetStatus


@pytest.fixture
def temp_git_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    run_git(["init", "-b", "main"], repo)
    run_git(["config", "user.name", "Tester"], repo)
    run_git(["config", "user.email", "tester@example.com"], repo)
    (repo / "file.txt").write_text("hello\n", encoding="utf-8")
    run_git(["add", "file.txt"], repo)
    run_git(["commit", "-m", "init"], repo)
    return repo


@pytest.fixture
async def in_memory_db():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with session_factory() as session:
        yield session
    await engine.dispose()


def test_format_and_parse_trailers():
    session_id = uuid.uuid4()
    run_id = uuid.uuid4()

    trailers_str = format_commit_trailers(session_id, run_id)
    assert f"Ttakkari-Session: {session_id}" in trailers_str
    assert f"Ttakkari-Run: {run_id}" in trailers_str

    msg = attach_commit_trailers("Add feature X\n\nDetail description", session_id, run_id)
    assert "Add feature X" in msg
    assert f"Ttakkari-Session: {session_id}" in msg
    assert f"Ttakkari-Run: {run_id}" in msg

    parsed = parse_commit_trailers(msg)
    assert parsed.get("Ttakkari-Session") == str(session_id)
    assert parsed.get("Ttakkari-Run") == str(run_id)


def test_verify_commit_provenance_in_git(temp_git_repo: Path):
    session_id = uuid.uuid4()
    run_id = uuid.uuid4()

    (temp_git_repo / "new.txt").write_text("content", encoding="utf-8")
    run_git(["add", "new.txt"], temp_git_repo)

    commit_msg = attach_commit_trailers("Worker change", session_id, run_id)
    run_git(["commit", "-m", commit_msg], temp_git_repo)

    head_sha = run_git(["rev-parse", "HEAD"], temp_git_repo).stdout.strip()

    assert verify_commit_provenance(temp_git_repo, head_sha, session_id)
    assert not verify_commit_provenance(temp_git_repo, head_sha, uuid.uuid4())


@pytest.mark.asyncio
async def test_register_change_set(in_memory_db: AsyncSession):
    session_id = uuid.uuid4()
    run_id = uuid.uuid4()

    cs = await register_change_set(
        in_memory_db,
        session_id=session_id,
        run_id=run_id,
        repo_path="/path/to/repo",
        branch_name=f"work/{session_id}",
        worktree_path=f"/path/to/worktree/{session_id}",
        base_sha="abcdef1234567890abcdef1234567890abcdef12",
    )

    assert cs.id is not None
    assert cs.session_id == session_id
    assert cs.run_id == run_id
    assert cs.status == ChangeSetStatus.registered
    assert cs.repair_attempts == 0
