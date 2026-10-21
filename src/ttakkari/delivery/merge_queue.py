"""Serial integration loop and PostgreSQL merge queue (ADR 0066 D3).

독립된 프로세스/루프가 큐에서 ChangeSet을 하나씩 순차(FOR UPDATE SKIP LOCKED)로 꺼내
최신 main 브랜치 위로 rebase/검증하고, 검증된 변경만 fast-forward 승격한다.
충돌이나 검증 실패는 별도 상태(conflict, rejected)로 격리되어
다른 정상 변경을 영구히 막지 않는다.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable, Coroutine

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ttakkari.delivery_models import ChangeSet, ChangeSetStatus
from ttakkari.delivery.diff_guard import check_forbidden_diff
from ttakkari.delivery.provenance import verify_commit_provenance
from ttakkari.delivery.repair_policy import (
    check_repair_budget,
    extract_failure_signature,
    record_repair_attempt,
)
from ttakkari.delivery.worktree import (
    cleanup_session_worktree,
    inspect_worktree_status,
    run_git,
    SessionWorktree,
)
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


async def submit_change(
    db: AsyncSession,
    change_set_id: uuid.UUID,
    head_sha: str | None = None,
) -> ChangeSet:
    """Worker가 작업을 완료하고 통합을 위해 ChangeSet을 큐에 제출 (D3)."""
    cs = await db.get(ChangeSet, change_set_id)
    if cs is None:
        raise ValueError(f"ChangeSet '{change_set_id}'를 찾을 수 없습니다.")

    if cs.status in (ChangeSetStatus.merged, ChangeSetStatus.rebasing, ChangeSetStatus.validating_stage1):
        raise RuntimeError(f"ChangeSet이 현재 상태('{cs.status}')에서는 제출될 수 없습니다.")

    wt_path = Path(cs.worktree_path)
    if wt_path.exists():
        wt_status = inspect_worktree_status(wt_path)
        if wt_status.is_dirty:
            cs.is_dirty = True
            log.warning("Submitted worktree is dirty: %s", wt_path)
        if not head_sha and wt_status.head_sha:
            head_sha = wt_status.head_sha

    if not head_sha:
        raise ValueError("제출할 커밋 SHA(head_sha)를 확인할 수 없습니다.")

    cs.head_sha = head_sha
    cs.status = ChangeSetStatus.submitted
    cs.submitted_at = utcnow()
    await db.flush()

    return cs


async def claim_next_change(
    db: AsyncSession,
    target_branch: str = "main",
) -> ChangeSet | None:
    """통합 루프가 큐에서 다음 제출된 ChangeSet을 단일 점유(SKIP LOCKED)."""
    # SQLite 호환성을 위해 with_for_update는 바인딩 엔진에 따라 선택 적용
    stmt = (
        select(ChangeSet)
        .where(
            ChangeSet.status == ChangeSetStatus.submitted,
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
        await db.flush()

    return cs


def check_rebase_or_merge(
    repo_path: Path,
    head_sha: str,
    target_branch: str = "main",
) -> tuple[bool, str | None, list[str]]:
    """최신 target_branch에 대해 충돌 여부를 검사하고 rebase 가능 여부 판정.

    반환: (can_merge_cleanly, rebased_commit_or_tree_sha, conflict_files)
    """
    repo = repo_path.resolve()

    # 1. target_branch HEAD 확인
    target_rev = run_git(["rev-parse", target_branch], repo)
    if target_rev.returncode != 0:
        return False, None, [f"Target branch '{target_branch}' not found"]
    target_head = target_rev.stdout.strip()

    # 2. 이미 최신 target_head의 자손인지 확인 (fast-forward 가능 여부)
    ancestor_check = run_git(["merge-base", "--is-ancestor", target_head, head_sha], repo)
    if ancestor_check.returncode == 0:
        # 이미 최신 main 위이므로 추가 rebase 불필요
        return True, head_sha, []

    # 3. merge-tree를 통한 가상 병합 및 충돌 점검
    merge_tree_res = run_git(
        ["merge-tree", "--write-tree", "--name-only", target_head, head_sha],
        repo,
    )

    if merge_tree_res.returncode == 0:
        # 충돌 없이 합쳐짐
        tree_sha = merge_tree_res.stdout.strip().splitlines()[0]
        return True, tree_sha, []

    # 충돌 발생: 충돌 파일 목록 추출
    conflict_files: list[str] = []
    diff_res = run_git(["diff", "--name-only", "--diff-filter=U"], repo)
    if diff_res.returncode == 0 and diff_res.stdout.strip():
        conflict_files = diff_res.stdout.strip().splitlines()
    else:
        # merge-tree 출력에서 conflict 라인 파싱
        for line in merge_tree_res.stdout.splitlines() + merge_tree_res.stderr.splitlines():
            if "CONFLICT" in line or "conflict" in line:
                conflict_files.append(line.strip())

    if not conflict_files:
        conflict_files = ["Merge conflict detected during dry-run integration"]

    return False, None, conflict_files


async def process_single_change(
    db: AsyncSession,
    change_set: ChangeSet,
    repo_path: Path,
    stage1_runner: ValidationRunner,
    stage2_runner: ValidationRunner,
) -> IntegrationResult:
    """단일 ChangeSet에 대한 직렬 통합 검증 및 승격 실행 (ADR 0066 D3, D4, D5)."""
    repo = repo_path.resolve()
    head_sha = change_set.head_sha
    if not head_sha:
        change_set.status = ChangeSetStatus.rejected
        change_set.rejection_reason = "Missing head_sha"
        await db.flush()
        return IntegrationResult(change_set.id, ChangeSetStatus.rejected, False, reason="Missing head_sha")

    # 1. diff guard 기계 검사 (ADR D5)
    diff_res = run_git(["diff", f"{change_set.base_sha}..{head_sha}"], repo)
    if diff_res.returncode == 0:
        msg_res = run_git(["log", "-1", "--format=%B", head_sha], repo)
        commit_msg = msg_res.stdout if msg_res.returncode == 0 else ""
        guard_result = check_forbidden_diff(diff_res.stdout, commit_msg)
        change_set.diff_guard_result = {
            "is_valid": guard_result.is_valid,
            "violations": [v.detail for v in guard_result.violations],
        }
        if not guard_result.is_valid:
            violations_summary = "; ".join(v.detail for v in guard_result.violations)
            change_set.status = ChangeSetStatus.rejected
            change_set.rejection_reason = f"DiffGuard 위반: {violations_summary}"
            await db.flush()
            return IntegrationResult(
                change_set.id,
                ChangeSetStatus.rejected,
                False,
                reason=change_set.rejection_reason,
            )

    # 2. Rebase 및 충돌 검사
    clean, tree_or_sha, conflicts = check_rebase_or_merge(
        repo, head_sha, change_set.target_branch
    )
    if not clean:
        change_set.status = ChangeSetStatus.conflict
        change_set.conflict_files = conflicts
        change_set.rejection_reason = f"최신 {change_set.target_branch}와 충돌 발생 ({len(conflicts)} 파일)"
        await db.flush()
        return IntegrationResult(
            change_set.id,
            ChangeSetStatus.conflict,
            False,
            conflict_files=conflicts,
            reason=change_set.rejection_reason,
        )

    # 3. Stage 1 검증: 변경 직후 빠른 검증 (계약 묶음, lint, 영향 테스트)
    change_set.status = ChangeSetStatus.validating_stage1
    await db.flush()

    s1_passed, s1_log, s1_sig_data = await stage1_runner("stage1", repo, head_sha)
    change_set.stage1_result = {"passed": s1_passed, "log": s1_log[:2000]}
    if not s1_passed:
        sig = extract_failure_signature(
            s1_sig_data[0] if s1_sig_data else "unknown_test",
            s1_sig_data[1] if s1_sig_data else "Stage1Failure",
        )
        record_repair_attempt(change_set, sig)
        budget = check_repair_budget(change_set, sig)
        change_set.status = ChangeSetStatus.rejected
        change_set.rejection_reason = (
            f"Stage 1 검증 실패: {sig} (수리 예산 초과)"
            if not budget.allowed
            else f"Stage 1 검증 실패: {sig}"
        )
        await db.flush()
        return IntegrationResult(
            change_set.id,
            ChangeSetStatus.rejected,
            False,
            failure_signature=sig,
            reason=change_set.rejection_reason,
        )

    # 4. Stage 2 검증: 통합 검증 (rebase 결과 고위험 영역 및 필수 묶음)
    change_set.status = ChangeSetStatus.validating_stage2
    await db.flush()

    s2_passed, s2_log, s2_sig_data = await stage2_runner("stage2", repo, head_sha)
    change_set.stage2_result = {"passed": s2_passed, "log": s2_log[:2000]}
    if not s2_passed:
        sig = extract_failure_signature(
            s2_sig_data[0] if s2_sig_data else "unknown_test",
            s2_sig_data[1] if s2_sig_data else "Stage2Failure",
        )
        record_repair_attempt(change_set, sig)
        budget = check_repair_budget(change_set, sig)
        change_set.status = ChangeSetStatus.rejected
        change_set.rejection_reason = (
            f"Stage 2 검증 실패: {sig} (수리 예산 초과)"
            if not budget.allowed
            else f"Stage 2 검증 실패: {sig}"
        )
        await db.flush()
        return IntegrationResult(
            change_set.id,
            ChangeSetStatus.rejected,
            False,
            failure_signature=sig,
            reason=change_set.rejection_reason,
        )

    # 5. 모든 검증 통과 -> main 브랜치 승격 (fast-forward)
    change_set.status = ChangeSetStatus.ready_for_merge
    await db.flush()

    # target_branch로 이동 후 fast-forward 병합 실행
    ff_res = run_git(["merge", "--ff-only", head_sha], repo)
    if ff_res.returncode != 0:
        # 그 사이 main이 advance 된 경우: 다음 루프에서 rebase 하도록 submitted로 복구하거나 거절
        change_set.status = ChangeSetStatus.submitted
        change_set.rejection_reason = "Base advanced concurrently right before fast-forward; re-queued"
        await db.flush()
        return IntegrationResult(
            change_set.id,
            ChangeSetStatus.submitted,
            False,
            reason=change_set.rejection_reason,
        )

    change_set.status = ChangeSetStatus.merged
    change_set.merged_at = utcnow()
    await db.flush()

    # worktree 안전한 정리
    wt = SessionWorktree(
        session_id=change_set.session_id,
        repo_path=repo,
        worktree_path=Path(change_set.worktree_path),
        branch_name=change_set.branch_name,
        base_sha=change_set.base_sha,
    )
    cleanup_session_worktree(wt, ChangeSetStatus.merged)

    return IntegrationResult(
        change_set.id,
        ChangeSetStatus.merged,
        True,
        rebased_sha=head_sha,
        reason="Integrated and merged successfully",
    )
