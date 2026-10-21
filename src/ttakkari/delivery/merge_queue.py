"""Serial integration loop and PostgreSQL merge queue (ADR 0066 D3).

독립된 프로세스/루프가 큐에서 ChangeSet을 하나씩 순차(FOR UPDATE SKIP LOCKED)로 꺼내
최신 main 브랜치 위로 rebase/검증하고, 검증된 변경만 fast-forward 승격한다.
충돌이나 검증 실패는 별도 상태(conflict, rejected)로 격리되어
다른 정상 변경을 영구히 막지 않는다.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
import tempfile
import uuid
from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import and_, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from ttakkari.delivery.diff_guard import check_forbidden_diff
from ttakkari.delivery.provenance import verify_commit_provenance
from ttakkari.delivery.repair_policy import (
    check_repair_budget,
    extract_failure_signature,
    record_repair_attempt,
)
from ttakkari.delivery.worktree import (
    SessionWorktree,
    cleanup_session_worktree,
    inspect_worktree_status,
    run_git,
)
from ttakkari.delivery_models import ChangeSet, ChangeSetStatus
from ttakkari.models import utcnow

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class IntegrationResult:
    change_set_id: uuid.UUID
    status: ChangeSetStatus
    success: bool
    rebased_sha: str | None = None
    reason: str | None = None
    conflict_files: list[str] | None = None
    failure_signature: str | None = None


# 검증 실행 함수 타입 (stage_name, repo_path, commit_sha -> (passed, failure_detail, failure_signature))
ValidationRunner = Callable[
    [str, Path, str],
    Coroutine[Any, Any, tuple[bool, str, tuple[str, str] | None]],
]
RepairDispatcher = Callable[[AsyncSession, ChangeSet, str], Coroutine[Any, Any, None]]
TARGET_LOCK_TIMEOUT_SECONDS = 900


async def submit_change(
    db: AsyncSession,
    change_set_id: uuid.UUID,
    head_sha: str | None = None,
) -> ChangeSet:
    """Worker가 작업을 완료하고 통합을 위해 ChangeSet을 큐에 제출 (D3)."""
    cs = await db.get(ChangeSet, change_set_id)
    if cs is None:
        raise ValueError(f"ChangeSet '{change_set_id}'를 찾을 수 없습니다.")

    if cs.status not in (ChangeSetStatus.registered, ChangeSetStatus.rejected, ChangeSetStatus.conflict):
        raise RuntimeError(f"ChangeSet이 현재 상태('{cs.status}')에서는 제출될 수 없습니다.")

    wt_path = Path(cs.worktree_path)
    if wt_path.exists():
        wt_status = inspect_worktree_status(wt_path)
        if wt_status.is_dirty:
            raise ValueError("Cannot submit a dirty worktree; commit or discard all changes first.")
        if not head_sha and wt_status.head_sha:
            head_sha = wt_status.head_sha

    if not head_sha:
        raise ValueError("제출할 커밋 SHA(head_sha)를 확인할 수 없습니다.")

    if len(head_sha) != 40 or run_git(["cat-file", "-e", f"{head_sha}^{{commit}}"], Path(cs.repo_path)).returncode != 0:
        raise ValueError("제출할 head_sha가 유효한 Git commit이 아닙니다.")
    if not verify_commit_provenance(Path(cs.repo_path), head_sha, cs.session_id):
        raise ValueError("제출 커밋에 등록된 세션과 일치하는 Ttakkari-Session provenance가 없습니다.")

    cs.head_sha = head_sha
    cs.status = ChangeSetStatus.submitted
    cs.submitted_at = utcnow()
    await db.flush()

    return cs


async def claim_next_change(
    db: AsyncSession,
    target_branch: str = "main",
    claim_timeout_seconds: int = 900,
) -> ChangeSet | None:
    """통합 루프가 큐에서 다음 제출된 ChangeSet을 단일 점유(SKIP LOCKED)."""
    # SQLite 호환성을 위해 with_for_update는 바인딩 엔진에 따라 선택 적용
    stmt = (
        select(ChangeSet)
        .where(
            or_(
                ChangeSet.status.in_((ChangeSetStatus.submitted, ChangeSetStatus.ready_for_merge)),
                and_(
                    ChangeSet.status.in_((ChangeSetStatus.rebasing, ChangeSetStatus.validating_stage1, ChangeSetStatus.validating_stage2)),
                    ChangeSet.claimed_at < datetime.now(UTC) - timedelta(seconds=claim_timeout_seconds),
                ),
            ),
            ChangeSet.target_branch == target_branch,
        )
        .order_by(ChangeSet.submitted_at.asc())
        .limit(1)
    )
    # PostgreSQL 환경에서는 SKIP LOCKED 적용
    if db.bind and db.bind.dialect.name == "postgresql":
        stmt = stmt.with_for_update(skip_locked=True)

    cs = await db.scalar(stmt)
    if cs is not None:
        cs.status = ChangeSetStatus.rebasing
        cs.claimed_at = utcnow()
        await db.flush()

    return cs


async def process_single_change(
    db: AsyncSession,
    change_set: ChangeSet,
    repo_path: Path,
    stage1_runner: ValidationRunner,
    stage2_runner: ValidationRunner,
    repair_dispatcher: RepairDispatcher | None = None,
) -> IntegrationResult:
    """단일 ChangeSet에 대한 직렬 통합 검증 및 승격 실행 (ADR 0066 D3, D4, D5)."""
    if not callable(stage1_runner) or not callable(stage2_runner):
        raise TypeError("A configured stage1 and stage2 validation runner is required.")
    repo = repo_path.resolve()
    head_sha = change_set.head_sha
    if not head_sha:
        change_set.status = ChangeSetStatus.rejected
        change_set.rejection_reason = "Missing head_sha"
        await db.flush()
        return IntegrationResult(change_set.id, ChangeSetStatus.rejected, False, reason="Missing head_sha")

    lock_key = f"{repo}:{change_set.target_branch}"
    postgres_lock = bool(db.bind and db.bind.dialect.name == "postgresql")
    lock_connection = await db.bind.connect() if postgres_lock else None
    candidate_holder: list[Path] = []
    try:
        if lock_connection is not None:
            deadline = asyncio.get_running_loop().time() + TARGET_LOCK_TIMEOUT_SECONDS
            while True:
                acquired = await lock_connection.scalar(
                    text("SELECT pg_try_advisory_lock(hashtext(:lock_key))"),
                    {"lock_key": lock_key},
                )
                if acquired:
                    break
                if asyncio.get_running_loop().time() >= deadline:
                    raise TimeoutError(f"Timed out waiting for integration lock: {lock_key}")
                await asyncio.sleep(1)
        return await _process_locked(db, change_set, repo, stage1_runner, stage2_runner, candidate_holder, repair_dispatcher)
    finally:
        for candidate_dir in candidate_holder:
            run_git(["worktree", "remove", "--force", str(candidate_dir)], repo)
            shutil.rmtree(candidate_dir, ignore_errors=True)
        if lock_connection is not None:
            await lock_connection.execute(text("SELECT pg_advisory_unlock(hashtext(:lock_key))"), {"lock_key": lock_key})
            await lock_connection.close()


async def _process_locked(
    db: AsyncSession,
    change_set: ChangeSet,
    repo: Path,
    stage1_runner: ValidationRunner,
    stage2_runner: ValidationRunner,
    candidate_holder: list[Path],
    repair_dispatcher: RepairDispatcher | None,
) -> IntegrationResult:
    head_sha = change_set.head_sha
    assert head_sha
    branch_check = run_git(["check-ref-format", "--branch", change_set.target_branch], repo)
    if branch_check.returncode != 0:
        return await _reject(db, change_set, f"Invalid target branch: {change_set.target_branch}")
    target_ref = f"refs/heads/{change_set.target_branch}"
    target_res = run_git(["rev-parse", "--verify", target_ref], repo)
    if target_res.returncode != 0:
        return await _reject(db, change_set, "Target ref could not be resolved.")
    target_head = target_res.stdout.strip()
    if change_set.candidate_sha and target_head == change_set.candidate_sha:
        bound_results = (change_set.stage1_result or {}, change_set.stage2_result or {})
        if all(result.get("passed") and result.get("tested_sha") == change_set.candidate_sha for result in bound_results):
            change_set.status = ChangeSetStatus.merged
            change_set.merged_at = utcnow()
            if change_set.candidate_worktree_path:
                _remove_candidate_worktree(repo, Path(change_set.candidate_worktree_path))
                change_set.candidate_worktree_path = None
            cleanup_session_worktree(
                SessionWorktree(change_set.session_id, repo, Path(change_set.worktree_path), change_set.branch_name, change_set.base_sha),
                ChangeSetStatus.merged,
            )
            await db.flush()
            return IntegrationResult(change_set.id, ChangeSetStatus.merged, True, rebased_sha=change_set.candidate_sha, reason="Recovered Git promotion after DB interruption")
        return await _reject(db, change_set, "Target ref equals candidate SHA but bound Stage 1/2 proof is missing.")

    # Confirm every pinned object and ancestry comparison before diff or integration.
    for sha in (change_set.base_sha, head_sha, target_head):
        check = run_git(["cat-file", "-e", f"{sha}^{{commit}}"], repo)
        if len(sha) != 40 or check.returncode != 0:
            return await _reject(db, change_set, f"Invalid or missing commit SHA: {sha}")
    base_check = run_git(["merge-base", "--is-ancestor", change_set.base_sha, head_sha], repo)
    if base_check.returncode != 0:
        return await _reject(db, change_set, "Submitted head is not descended from the pinned base SHA.")
    if not verify_commit_provenance(repo, head_sha, change_set.session_id):
        return await _reject(db, change_set, "Submitted commit provenance does not match the registered session.")

    # 1. diff guard: command failure is itself a rejection, never a skipped check.
    diff_res = run_git(["diff", f"{change_set.base_sha}..{head_sha}"], repo)
    if diff_res.returncode != 0:
        return await _reject(db, change_set, f"git diff failed: {diff_res.stderr.strip()}")
    names = run_git(["diff", "--name-only", "-z", change_set.base_sha, head_sha], repo)
    if names.returncode != 0:
        return await _reject(db, change_set, f"git diff --name-only failed: {names.stderr.strip()}")
    source_pairs: list[tuple[str, str, str]] = []
    for file_path in names.stdout.split("\0"):
        if not file_path or not ("test" in file_path.lower() or file_path.endswith("_test.py")):
            continue
        before = run_git(["show", f"{change_set.base_sha}:{file_path}"], repo)
        after = run_git(["show", f"{head_sha}:{file_path}"], repo)
        if before.returncode != 0 and after.returncode != 0:
            return await _reject(db, change_set, f"Could not inspect changed test source: {file_path}")
        source_pairs.append((file_path, before.stdout if before.returncode == 0 else "", after.stdout if after.returncode == 0 else ""))
    msg_res = run_git(["log", "-1", "--format=%B", head_sha], repo)
    if msg_res.returncode != 0:
        return await _reject(db, change_set, f"Could not read submitted commit message: {msg_res.stderr.strip()}")
    guard_result = check_forbidden_diff(diff_res.stdout, msg_res.stdout, test_source_pairs=source_pairs)
    change_set.diff_guard_result = {"is_valid": guard_result.is_valid, "violations": [v.detail for v in guard_result.violations]}
    if not guard_result.is_valid:
        return await _reject(db, change_set, "DiffGuard violation: " + "; ".join(v.detail for v in guard_result.violations))

    # 2. Produce an immutable merge candidate from the exact pinned target + head.
    ancestor = run_git(["merge-base", "--is-ancestor", target_head, head_sha], repo)
    if ancestor.returncode == 0:
        candidate_sha = head_sha
    else:
        merge_tree = run_git(["merge-tree", "--write-tree", "--name-only", target_head, head_sha], repo)
        if merge_tree.returncode != 0:
            conflicts = [line.strip() for line in (merge_tree.stdout + merge_tree.stderr).splitlines() if "CONFLICT" in line.upper()]
            if not conflicts:
                conflicts = [f"merge-tree failed ({merge_tree.returncode}): {merge_tree.stderr.strip()[:300]}"]
            change_set.status = ChangeSetStatus.conflict
            change_set.conflict_files = conflicts
            change_set.rejection_reason = f"Target merge conflict ({len(conflicts)} entries)"
            await db.flush()
            return IntegrationResult(change_set.id, ChangeSetStatus.conflict, False, conflict_files=conflicts, reason=change_set.rejection_reason)
        tree_sha = merge_tree.stdout.strip().splitlines()[0]
        commit = run_git(["commit-tree", tree_sha, "-p", target_head, "-p", head_sha, "-m", f"Merge {head_sha[:12]} into {change_set.target_branch}"], repo)
        if commit.returncode != 0 or not commit.stdout.strip():
            return await _reject(db, change_set, f"Could not create merge candidate: {commit.stderr.strip()}")
        candidate_sha = commit.stdout.strip()
    change_set.candidate_sha = candidate_sha
    lock_token = uuid.uuid5(uuid.NAMESPACE_URL, f"{repo}:{change_set.target_branch}").hex[:12]
    candidate_prefix = f"ttk-delivery-candidate-{lock_token}-"
    _cleanup_stale_candidate_worktrees(repo, candidate_prefix)
    candidate_dir = Path(tempfile.mkdtemp(prefix=candidate_prefix))
    candidate_dir.rmdir()
    candidate_holder.append(candidate_dir)
    add = run_git(["worktree", "add", "--detach", str(candidate_dir), candidate_sha], repo)
    if add.returncode != 0:
        return await _reject(db, change_set, f"Could not create candidate checkout: {add.stderr.strip()}")
    change_set.candidate_worktree_path = str(candidate_dir)

    # 3. Both stages receive the same detached checkout and SHA. Missing/empty evidence fails closed.
    change_set.status = ChangeSetStatus.validating_stage1
    await db.flush()
    s1_passed, s1_log, s1_sig_data = await stage1_runner("stage1", candidate_dir, candidate_sha)
    if not s1_log.strip():
        s1_passed = False
        s1_log = "Stage 1 returned empty validation evidence."
    change_set.stage1_result = {"passed": s1_passed, "log": s1_log[:2000], "tested_sha": candidate_sha}
    if not s1_passed:
        return await _record_validation_failure(db, change_set, "Stage 1", s1_log, s1_sig_data, repair_dispatcher)

    change_set.status = ChangeSetStatus.validating_stage2
    await db.flush()
    s2_passed, s2_log, s2_sig_data = await stage2_runner("stage2", candidate_dir, candidate_sha)
    if not s2_log.strip():
        s2_passed = False
        s2_log = "Stage 2 returned empty validation evidence."
    change_set.stage2_result = {"passed": s2_passed, "log": s2_log[:2000], "tested_sha": candidate_sha}
    if not s2_passed:
        return await _record_validation_failure(db, change_set, "Stage 2", s2_log, s2_sig_data, repair_dispatcher)

    # Persist the tested candidate before touching Git. On restart, candidate_sha permits
    # idempotent recovery if the ref update succeeded but the final DB commit did not.
    change_set.status = ChangeSetStatus.ready_for_merge
    await db.flush()
    await db.commit()
    current = run_git(["rev-parse", "--verify", target_ref], repo)
    if current.returncode != 0 or current.stdout.strip() != target_head:
        change_set.status = ChangeSetStatus.submitted
        change_set.candidate_sha = None
        change_set.candidate_worktree_path = None
        change_set.rejection_reason = "Pinned target ref advanced before promotion; safe retry required."
        await db.flush()
        return IntegrationResult(change_set.id, ChangeSetStatus.submitted, False, reason=change_set.rejection_reason)
    promote = run_git(["update-ref", target_ref, candidate_sha, target_head], repo)
    if promote.returncode != 0:
        change_set.status = ChangeSetStatus.submitted
        change_set.rejection_reason = f"Target ref compare-and-swap failed: {promote.stderr.strip()}"
        await db.flush()
        return IntegrationResult(change_set.id, ChangeSetStatus.submitted, False, reason=change_set.rejection_reason)
    change_set.status = ChangeSetStatus.merged
    change_set.merged_at = utcnow()
    change_set.candidate_worktree_path = None
    change_set.rejection_reason = None
    await db.flush()

    wt = SessionWorktree(change_set.session_id, repo, Path(change_set.worktree_path), change_set.branch_name, change_set.base_sha)
    cleanup_session_worktree(wt, ChangeSetStatus.merged)
    return IntegrationResult(change_set.id, ChangeSetStatus.merged, True, rebased_sha=candidate_sha, reason="Validated immutable candidate promoted by target-ref CAS")


async def _reject(db: AsyncSession, change_set: ChangeSet, reason: str) -> IntegrationResult:
    change_set.status = ChangeSetStatus.rejected
    change_set.rejection_reason = reason[:2000]
    await db.flush()
    return IntegrationResult(change_set.id, ChangeSetStatus.rejected, False, reason=change_set.rejection_reason)


async def _record_validation_failure(
    db: AsyncSession,
    change_set: ChangeSet,
    stage: str,
    log_text: str,
    sig_data: tuple[str, str] | None,
    repair_dispatcher: RepairDispatcher | None,
) -> IntegrationResult:
    sig = extract_failure_signature(sig_data[0] if sig_data else "unknown_test", sig_data[1] if sig_data else f"{stage}Failure")
    budget = check_repair_budget(change_set, sig)
    if budget.allowed:
        record_repair_attempt(change_set, sig)
        from ttakkari.delivery.repair_policy import format_repair_prompt
        prompt = format_repair_prompt(change_set, sig, log_text)
        if repair_dispatcher is not None:
            await repair_dispatcher(db, change_set, prompt)
            change_set.status = ChangeSetStatus.registered
            change_set.rejection_reason = f"{stage} validation failed; bounded repair run queued ({change_set.repair_attempts}/{3}): {sig}"
        else:
            change_set.status = ChangeSetStatus.rejected
            change_set.rejection_reason = f"{stage} validation failed: {sig}; no repair dispatcher is configured"
    else:
        change_set.status = ChangeSetStatus.rejected
        change_set.rejection_reason = f"{stage} validation failed: {sig} ({budget.reason})"
    change_set.candidate_worktree_path = None
    await db.flush()
    return IntegrationResult(change_set.id, ChangeSetStatus(change_set.status), False, failure_signature=sig, reason=change_set.rejection_reason)


def _remove_candidate_worktree(repo: Path, candidate: Path) -> None:
    if not candidate.name.startswith("ttk-delivery-candidate-"):
        raise RuntimeError(f"Refusing to remove non-candidate worktree path: {candidate}")
    if not candidate.exists():
        run_git(["worktree", "prune"], repo)
        return
    common = run_git(["rev-parse", "--git-common-dir"], candidate)
    expected = run_git(["rev-parse", "--git-common-dir"], repo)
    if common.returncode != 0 or expected.returncode != 0:
        raise RuntimeError("Could not verify candidate worktree ownership.")
    common_path = Path(common.stdout.strip())
    expected_path = Path(expected.stdout.strip())
    if not common_path.is_absolute():
        common_path = candidate / common_path
    if not expected_path.is_absolute():
        expected_path = repo / expected_path
    if common_path.resolve() != expected_path.resolve():
        raise RuntimeError("Candidate worktree belongs to a different Git repository.")
    removed = run_git(["worktree", "remove", "--force", str(candidate)], repo)
    if removed.returncode != 0:
        raise RuntimeError(f"Failed to remove candidate worktree: {removed.stderr.strip()}")


def _cleanup_stale_candidate_worktrees(repo: Path, prefix: str) -> None:
    listed = run_git(["worktree", "list", "--porcelain"], repo)
    if listed.returncode != 0:
        raise RuntimeError(f"Could not inspect Git worktrees before candidate creation: {listed.stderr.strip()}")
    for block in listed.stdout.split("\n\n"):
        worktree_line = next((line for line in block.splitlines() if line.startswith("worktree ")), None)
        if not worktree_line:
            continue
        path = Path(worktree_line.removeprefix("worktree "))
        if path.name.startswith(prefix) and path.exists():
            _remove_candidate_worktree(repo, path)
