"""Harness-managed worktree isolation and lifecycle management (ADR 0066 D1).

따까리가 관리하는 Git 저장소에 코드 쓰기 작업을 하는 worker에게만
session별 worktree와 work/<session_id> 브랜치를 자동 할당하고 관리한다.
읽기 전용, 비git, 게임 격리 작업은 제외된다.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ttakkari.delivery_models import ChangeSetStatus

log = logging.getLogger(__name__)

DEFAULT_WORKTREE_ROOT = Path.home() / ".ttakkari" / "worktrees"


@dataclass(frozen=True)
class SessionWorktree:
    session_id: uuid.UUID
    repo_path: Path
    worktree_path: Path
    branch_name: str
    base_sha: str


@dataclass(frozen=True)
class WorktreeStatus:
    is_dirty: bool
    uncommitted_files: list[str]
    head_sha: str | None


@dataclass(frozen=True)
class CleanupResult:
    removed: bool
    preserved: bool
    reason: str


def should_isolate_worker(
    *,
    is_git: bool,
    is_write: bool = True,
    is_game: bool = False,
    is_verification: bool = False,
) -> bool:
    """결정 기준 D1: 따까리 관리 Git 저장소에 쓰기 작업을 하는 worker에 한해 격리.

    - 비 Git 저장소(is_git=False)는 제외
    - 읽기 전용/조사(is_write=False)는 제외
    - 검증 작업(is_verification=True)은 detached worktree 별도 처리
    - 게임 스튜디오(ADR 0058)는 자체 workspace 경로를 사용하므로 제외
    """
    if not is_git or not is_write or is_game or is_verification:
        return False
    return True


def run_git(args: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    """Git 명령어 실행 래퍼."""
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
    )


def validate_writable_roots(
    extra_writable_roots: list[str | Path],
    worktree_path: Path,
) -> list[Path]:
    """샌드박스 쓰기 경로(extra_writable_roots)에 worktree 경로가 포함되는지 검증하고 확보."""
    resolved_roots = [Path(p).resolve() for p in extra_writable_roots]
    resolved_wt = worktree_path.resolve()

    if resolved_wt not in resolved_roots:
        resolved_roots.append(resolved_wt)

    return resolved_roots


def create_session_worktree(
    repo_path: Path,
    session_id: uuid.UUID,
    base_ref: str = "HEAD",
    worktrees_root: Path | None = None,
) -> SessionWorktree:
    """작업 세션 전용 worktree와 브랜치 생성 (ADR 0066 D1).

    위치: ~/.ttakkari/worktrees/<session_id>
    브랜치: work/<session_id>
    """
    repo = repo_path.resolve()
    base_dir = (worktrees_root or DEFAULT_WORKTREE_ROOT).resolve()
    base_dir.mkdir(parents=True, exist_ok=True)

    wt_path = base_dir / str(session_id)
    branch = f"work/{session_id}"

    # 현재 base_ref의 SHA 확인
    rev_res = run_git(["rev-parse", base_ref], repo)
    if rev_res.returncode != 0:
        raise RuntimeError(f"Failed to resolve base ref '{base_ref}': {rev_res.stderr.strip()}")
    base_sha = rev_res.stdout.strip()

    # 이미 존재하는 worktree/브랜치인지 점검
    if wt_path.exists():
        # 기존 worktree가 남아 있는 경우 상태 점검
        status = inspect_worktree_status(wt_path)
        if status.is_dirty:
            log.warning("Worktree already exists and is dirty: %s", wt_path)
        return SessionWorktree(
            session_id=session_id,
            repo_path=repo,
            worktree_path=wt_path,
            branch_name=branch,
            base_sha=base_sha,
        )

    # 브랜치가 이미 존재하면 기존 브랜치를 체크아웃하는 worktree 생성, 없으면 새 브랜치 생성
    branch_check = run_git(["show-ref", "--verify", f"refs/heads/{branch}"], repo)
    if branch_check.returncode == 0:
        add_res = run_git(["worktree", "add", str(wt_path), branch], repo)
    else:
        add_res = run_git(["worktree", "add", "-b", branch, str(wt_path), base_ref], repo)

    if add_res.returncode != 0:
        raise RuntimeError(f"git worktree add failed: {add_res.stderr.strip()}")

    # worktree 설정: worktreeConfig 활성화 및 Ttakkari 세션 트레일러/식별값 설정
    run_git(["config", "extensions.worktreeConfig", "true"], wt_path)
    run_git(["config", "--worktree", "ttakkari.session-id", str(session_id)], wt_path)

    return SessionWorktree(
        session_id=session_id,
        repo_path=repo,
        worktree_path=wt_path,
        branch_name=branch,
        base_sha=base_sha,
    )


def create_detached_worktree(
    repo_path: Path,
    commit_sha: str,
    target_dir: Path,
) -> Path:
    """검증/조사용 detached worktree 생성."""
    repo = repo_path.resolve()
    target = target_dir.resolve()
    target.parent.mkdir(parents=True, exist_ok=True)

    add_res = run_git(["worktree", "add", "--detach", str(target), commit_sha], repo)
    if add_res.returncode != 0:
        raise RuntimeError(f"git worktree add --detach failed: {add_res.stderr.strip()}")

    return target


def inspect_worktree_status(worktree_path: Path) -> WorktreeStatus:
    """작업 트리의 미커밋/dirty 변경 및 HEAD SHA 점검."""
    if not worktree_path.exists():
        return WorktreeStatus(is_dirty=False, uncommitted_files=[], head_sha=None)

    status_res = run_git(["status", "--porcelain"], worktree_path)
    uncommitted: list[str] = []
    if status_res.returncode == 0 and status_res.stdout.strip():
        for line in status_res.stdout.strip().splitlines():
            parts = line.strip().split(maxsplit=1)
            if len(parts) == 2:
                uncommitted.append(parts[1])

    head_res = run_git(["rev-parse", "HEAD"], worktree_path)
    head_sha = head_res.stdout.strip() if head_res.returncode == 0 else None

    return WorktreeStatus(
        is_dirty=len(uncommitted) > 0,
        uncommitted_files=uncommitted,
        head_sha=head_sha,
    )


def cleanup_session_worktree(
    worktree: SessionWorktree,
    status: ChangeSetStatus | str,
    force: bool = False,
) -> CleanupResult:
    """작업 트리 정리 (ADR 0066 D1 수명주기).

    - 통합 완료(merged) 시에만 안전하게 제거.
    - 미통합 변경이나 dirty 파일이 있으면 절대 삭제하지 않고 보존(abandoned).
    - force=True 시에만 강제 삭제.
    """
    wt_path = worktree.worktree_path
    repo = worktree.repo_path

    if not wt_path.exists():
        return CleanupResult(removed=False, preserved=False, reason="already_removed")

    wt_status = inspect_worktree_status(wt_path)

    # 1. dirty 상태 보호: 미커밋 변경 손실 방지
    if wt_status.is_dirty and not force:
        log.warning(
            "Preserving dirty worktree %s for session %s (files: %s)",
            wt_path,
            worktree.session_id,
            wt_status.uncommitted_files[:5],
        )
        return CleanupResult(
            removed=False,
            preserved=True,
            reason=f"dirty_uncommitted_changes ({len(wt_status.uncommitted_files)} files)",
        )

    # 2. 미통합 커밋 보호: merged 상태가 아니고 force가 아니면 보존
    is_merged = status == ChangeSetStatus.merged or status == "merged"
    if not is_merged and not force:
        log.info("Preserving unmerged worktree %s (status=%s)", wt_path, status)
        return CleanupResult(
            removed=False,
            preserved=True,
            reason=f"unmerged_change_set (status={status})",
        )

    # 3. 안전한 정리 실행
    remove_res = run_git(["worktree", "remove", "--force", str(wt_path)], repo)
    if remove_res.returncode != 0:
        # git worktree remove가 실패하면 안전하게 shutil.rmtree 후 prune
        shutil.rmtree(wt_path, ignore_errors=True)
        run_git(["worktree", "prune"], repo)

    # 브랜치 삭제
    run_git(["branch", "-D", worktree.branch_name], repo)

    return CleanupResult(removed=True, preserved=False, reason="safely_cleaned_up")
