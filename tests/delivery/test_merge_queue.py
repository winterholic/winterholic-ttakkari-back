"""Integration tests for serial merge queue and automated integration loop (ADR 0066 D3)."""

from __future__ import annotations

import tempfile
import uuid
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from ttakkari.delivery.merge_queue import (
    claim_next_change,
    process_single_change,
    submit_change,
)
from ttakkari.delivery.provenance import register_change_set
from ttakkari.delivery.worktree import (
    create_session_worktree,
    run_git,
    SessionWorktree,
)
from ttakkari.delivery_models import Base, ChangeSet, ChangeSetStatus


@pytest.fixture
def temp_git_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "origin_repo"
    repo.mkdir()
    run_git(["init", "-b", "main"], repo)
    run_git(["config", "user.name", "Tester"], repo)
    run_git(["config", "user.email", "tester@example.com"], repo)

    initial_file = repo / "README.md"
    initial_file.write_text("# Origin Main\n", encoding="utf-8")
    run_git(["add", "README.md"], repo)
    run_git(["commit", "-m", "Initial commit"], repo)

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


async def dummy_pass_runner(stage: str, repo: Path, sha: str) -> tuple[bool, str, tuple[str, str] | None]:
    return True, f"{stage} passed", None


async def dummy_fail_stage1_runner(stage: str, repo: Path, sha: str) -> tuple[bool, str, tuple[str, str] | None]:
    return False, "AssertionError in route inventory", ("test_route_inventory", "AssertionError")


async def dummy_fail_stage2_runner(stage: str, repo: Path, sha: str) -> tuple[bool, str, tuple[str, str] | None]:
    return False, "Security gate verification failed", ("test_security_gate", "SecurityViolation")


@pytest.mark.asyncio
async def test_clean_change_promoted_and_merged(temp_git_repo: Path, tmp_path: Path, in_memory_db: AsyncSession):
    """정상적인 변경이 Stage 1/2를 통과하고 main에 fast-forward 승격되는 흐름 검증."""
    wt_root = tmp_path / "worktrees"
    session_id = uuid.uuid4()
    wt = create_session_worktree(temp_git_repo, session_id, worktrees_root=wt_root)

    # worker 작업 수행
    feature_file = wt.worktree_path / "feature.py"
    feature_file.write_text("print('feature ready')\n", encoding="utf-8")
    run_git(["add", "feature.py"], wt.worktree_path)
    run_git(["commit", "-m", "Implement feature"], wt.worktree_path)

    head_sha = run_git(["rev-parse", "HEAD"], wt.worktree_path).stdout.strip()

    # ChangeSet 등록 및 제출
    cs = await register_change_set(
        in_memory_db,
        session_id=session_id,
        run_id=None,
        repo_path=str(temp_git_repo),
        branch_name=wt.branch_name,
        worktree_path=str(wt.worktree_path),
        base_sha=wt.base_sha,
    )
    await submit_change(in_memory_db, cs.id, head_sha)

    # 통합기가 큐에서 claim
    claimed = await claim_next_change(in_memory_db)
    assert claimed is not None
    assert claimed.id == cs.id
    assert claimed.status == ChangeSetStatus.rebasing

    # 직렬 통합 처리 실행
    result = await process_single_change(
        in_memory_db, claimed, temp_git_repo, dummy_pass_runner, dummy_pass_runner
    )

    assert result.success
    assert result.status == ChangeSetStatus.merged

    # main 저장소에 커밋이 반영되었는지 확인
    main_head = run_git(["rev-parse", "main"], temp_git_repo).stdout.strip()
    assert main_head == head_sha, "main 브랜치가 worker의 커밋으로 승격되지 않았습니다."
    assert (temp_git_repo / "feature.py").exists()

    # worktree는 안전하게 정리되었는지 확인
    assert not wt.worktree_path.exists()


@pytest.mark.asyncio
async def test_conflict_does_not_block_subsequent_normal_change(
    temp_git_repo: Path, tmp_path: Path, in_memory_db: AsyncSession
):
    """충돌이 발생한 ChangeSet은 conflict 상태로 분리되고, 뒤따르는 정상 변경은 승격되는지 검증."""
    wt_root = tmp_path / "worktrees"

    # Worker A (충돌 유발자): README.md 수정
    session_a = uuid.uuid4()
    wt_a = create_session_worktree(temp_git_repo, session_a, worktrees_root=wt_root)
    (wt_a.worktree_path / "README.md").write_text("# Conflicting Change from A\n", encoding="utf-8")
    run_git(["add", "README.md"], wt_a.worktree_path)
    run_git(["commit", "-m", "Change README in A"], wt_a.worktree_path)
    head_a = run_git(["rev-parse", "HEAD"], wt_a.worktree_path).stdout.strip()

    cs_a = await register_change_set(
        in_memory_db,
        session_id=session_a,
        run_id=None,
        repo_path=str(temp_git_repo),
        branch_name=wt_a.branch_name,
        worktree_path=str(wt_a.worktree_path),
        base_sha=wt_a.base_sha,
    )
    await submit_change(in_memory_db, cs_a.id, head_a)

    # 그 사이 main에서 README.md가 다르게 수정되어 커밋됨
    (temp_git_repo / "README.md").write_text("# Main modified concurrently\n", encoding="utf-8")
    run_git(["add", "README.md"], temp_git_repo)
    run_git(["commit", "-m", "Advance main with conflict"], temp_git_repo)

    # Worker B (정상 변경): 완전히 다른 파일 service.py 추가
    session_b = uuid.uuid4()
    # 최신 main 기준으로 B worktree 생성
    wt_b = create_session_worktree(temp_git_repo, session_b, worktrees_root=wt_root)
    (wt_b.worktree_path / "service.py").write_text("def run(): pass\n", encoding="utf-8")
    run_git(["add", "service.py"], wt_b.worktree_path)
    run_git(["commit", "-m", "Add service in B"], wt_b.worktree_path)
    head_b = run_git(["rev-parse", "HEAD"], wt_b.worktree_path).stdout.strip()

    cs_b = await register_change_set(
        in_memory_db,
        session_id=session_b,
        run_id=None,
        repo_path=str(temp_git_repo),
        branch_name=wt_b.branch_name,
        worktree_path=str(wt_b.worktree_path),
        base_sha=wt_b.base_sha,
    )
    await submit_change(in_memory_db, cs_b.id, head_b)

    # 1. 큐에서 A 처리 -> 충돌 감지 및 conflict 격리
    claimed_a = await claim_next_change(in_memory_db)
    assert claimed_a.id == cs_a.id
    res_a = await process_single_change(
        in_memory_db, claimed_a, temp_git_repo, dummy_pass_runner, dummy_pass_runner
    )
    assert not res_a.success
    assert res_a.status == ChangeSetStatus.conflict
    assert claimed_a.conflict_files is not None
    assert "충돌 발생" in (claimed_a.rejection_reason or "")

    # 2. 큐에서 B 처리 -> 충돌 없이 정상 승격!
    claimed_b = await claim_next_change(in_memory_db)
    assert claimed_b is not None
    assert claimed_b.id == cs_b.id
    res_b = await process_single_change(
        in_memory_db, claimed_b, temp_git_repo, dummy_pass_runner, dummy_pass_runner
    )
    assert res_b.success
    assert res_b.status == ChangeSetStatus.merged
    assert (temp_git_repo / "service.py").exists()


@pytest.mark.asyncio
async def test_stage1_failure_rejected_and_subsequent_runs(
    temp_git_repo: Path, tmp_path: Path, in_memory_db: AsyncSession
):
    """Stage 1 검증 실패 시 rejected 상태로 분리되고 실패 서명이 기록되는지 검증."""
    wt_root = tmp_path / "worktrees"
    session_id = uuid.uuid4()
    wt = create_session_worktree(temp_git_repo, session_id, worktrees_root=wt_root)

    (wt.worktree_path / "bad.py").write_text("# bad\n", encoding="utf-8")
    run_git(["add", "bad.py"], wt.worktree_path)
    run_git(["commit", "-m", "bad commit"], wt.worktree_path)
    head_sha = run_git(["rev-parse", "HEAD"], wt.worktree_path).stdout.strip()

    cs = await register_change_set(
        in_memory_db,
        session_id=session_id,
        run_id=None,
        repo_path=str(temp_git_repo),
        branch_name=wt.branch_name,
        worktree_path=str(wt.worktree_path),
        base_sha=wt.base_sha,
    )
    await submit_change(in_memory_db, cs.id, head_sha)

    claimed = await claim_next_change(in_memory_db)
    res = await process_single_change(
        in_memory_db, claimed, temp_git_repo, dummy_fail_stage1_runner, dummy_pass_runner
    )

    assert not res.success
    assert res.status == ChangeSetStatus.rejected
    assert claimed.failure_signature == "test_route_inventory::AssertionError"
    assert claimed.repair_attempts == 1
    assert "Stage 1 검증 실패" in (claimed.rejection_reason or "")


@pytest.mark.asyncio
async def test_diff_guard_blocks_and_rejects(
    temp_git_repo: Path, tmp_path: Path, in_memory_db: AsyncSession
):
    """금지된 diff(예: 테스트 삭제)가 포함된 ChangeSet은 즉시 거절되는지 검증."""
    wt_root = tmp_path / "worktrees"
    session_id = uuid.uuid4()

    # origin에 테스트 파일 생성
    test_file = temp_git_repo / "tests" / "test_api.py"
    test_file.parent.mkdir(parents=True, exist_ok=True)
    test_file.write_text("def test_api_call():\n    assert True\n", encoding="utf-8")
    run_git(["add", "tests/test_api.py"], temp_git_repo)
    run_git(["commit", "-m", "Add test_api"], temp_git_repo)

    wt = create_session_worktree(temp_git_repo, session_id, worktrees_root=wt_root)

    # worker가 테스트를 삭제함!
    (wt.worktree_path / "tests" / "test_api.py").write_text("# deleted test\n", encoding="utf-8")
    run_git(["add", "tests/test_api.py"], wt.worktree_path)
    run_git(["commit", "-m", "Delete test"], wt.worktree_path)
    head_sha = run_git(["rev-parse", "HEAD"], wt.worktree_path).stdout.strip()

    cs = await register_change_set(
        in_memory_db,
        session_id=session_id,
        run_id=None,
        repo_path=str(temp_git_repo),
        branch_name=wt.branch_name,
        worktree_path=str(wt.worktree_path),
        base_sha=wt.base_sha,
    )
    await submit_change(in_memory_db, cs.id, head_sha)

    claimed = await claim_next_change(in_memory_db)
    res = await process_single_change(
        in_memory_db, claimed, temp_git_repo, dummy_pass_runner, dummy_pass_runner
    )

    assert not res.success
    assert res.status == ChangeSetStatus.rejected
    assert "DiffGuard 위반" in (claimed.rejection_reason or "")
    assert claimed.diff_guard_result["is_valid"] is False
