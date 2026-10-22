"""Commit provenance and change set registration (ADR 0066 D2).

커밋 메시지에 세션 및 Run 식별자 트레일러를 부착하여
Session -> Run -> Commit -> Validation -> Integration 전 과정의 출처(provenance)를 추적한다.
"""

from __future__ import annotations

import re
import uuid
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession

from ttakkari.delivery.worktree import run_git
from ttakkari.delivery_models import ChangeSet, ChangeSetStatus

TRAILER_SESSION_KEY = "Ttakkari-Session"
TRAILER_RUN_KEY = "Ttakkari-Run"

_TRAILER_PATTERN = re.compile(r"^(Ttakkari-[A-Za-z0-9_-]+):\s*(.+)$", re.MULTILINE)


def format_commit_trailers(
    session_id: uuid.UUID,
    run_id: uuid.UUID | None = None,
) -> str:
    """커밋 메시지 하단에 추가할 Git 트레일러 형식 생성."""
    lines = [f"{TRAILER_SESSION_KEY}: {session_id}"]
    if run_id is not None:
        lines.append(f"{TRAILER_RUN_KEY}: {run_id}")
    return "\n".join(lines)


def attach_commit_trailers(
    message: str,
    session_id: uuid.UUID,
    run_id: uuid.UUID | None = None,
) -> str:
    """기존 커밋 메시지에 트레일러를 올바른 Git 포맷(빈 줄 분리)으로 부착."""
    clean_msg = message.rstrip()
    trailers = format_commit_trailers(session_id, run_id)
    if not clean_msg:
        return f"Work by session {session_id}\n\n{trailers}\n"
    return f"{clean_msg}\n\n{trailers}\n"


def parse_commit_trailers(commit_message: str) -> dict[str, str]:
    """커밋 메시지에서 Ttakkari-* 트레일러 추출."""
    trailers: dict[str, str] = {}
    for match in _TRAILER_PATTERN.finditer(commit_message):
        key = match.group(1)
        val = match.group(2).strip()
        trailers[key] = val
    return trailers


def inspect_commit_provenance(repo_path: Path, commit_sha: str) -> dict[str, str]:
    """저장소의 특정 커밋에서 트레일러 정보를 조회."""
    res = run_git(["log", "-1", "--format=%B", commit_sha], repo_path)
    if res.returncode != 0:
        return {}
    return parse_commit_trailers(res.stdout)


def verify_commit_provenance(
    repo_path: Path,
    commit_sha: str,
    expected_session_id: uuid.UUID,
) -> bool:
    """특정 커밋이 예상된 세션에서 생성되었는지 검증."""
    trailers = inspect_commit_provenance(repo_path, commit_sha)
    session_str = trailers.get(TRAILER_SESSION_KEY)
    if not session_str:
        return False
    return session_str.strip().lower() == str(expected_session_id).lower()


async def register_change_set(
    db: AsyncSession,
    *,
    session_id: uuid.UUID,
    run_id: uuid.UUID | None,
    repo_path: str,
    branch_name: str,
    worktree_path: str,
    base_sha: str,
    target_branch: str = "main",
) -> ChangeSet:
    """새로운 ChangeSet을 데이터베이스에 등록 (D2)."""
    cs = ChangeSet(
        id=uuid.uuid4(),
        session_id=session_id,
        run_id=run_id,
        repo_path=repo_path,
        branch_name=branch_name,
        worktree_path=worktree_path,
        base_sha=base_sha,
        target_branch=target_branch,
        status=ChangeSetStatus.registered,
    )
    db.add(cs)
    await db.flush()
    return cs
