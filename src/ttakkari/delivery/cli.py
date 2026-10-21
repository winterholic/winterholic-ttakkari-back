"""CLI interface for AI-native delivery pipeline (ADR 0066).

사용자 및 오케스트레이터가 CLI를 통해 격리 worktree 상태를 조회하고,
ChangeSet을 제출하며, 직렬 통합 루프를 실행하거나 상태를 감시할 수 있다.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import uuid
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from ttakkari.config import get_settings
from ttakkari.delivery_models import Base, ChangeSet, ChangeSetStatus
from ttakkari.delivery.merge_queue import claim_next_change, process_single_change, submit_change
from ttakkari.delivery.provenance import inspect_commit_provenance
from ttakkari.delivery.worktree import (
    cleanup_session_worktree,
    inspect_worktree_status,
    SessionWorktree,
)


async def _dummy_validator(stage: str, repo: Path, sha: str) -> tuple[bool, str, tuple[str, str] | None]:
    """기본 무검증 통과 러너 (실제 통합 환경에서는 pytest/ruff 호출로 교체 가능)."""
    return True, f"{stage} passed on {sha[:8]}", None


async def cmd_status(db: AsyncSession, limit: int = 20) -> int:
    """최근 ChangeSet 상태 목록 출력."""
    stmt = select(ChangeSet).order_by(ChangeSet.created_at.desc()).limit(limit)
    rows = list(await db.scalars(stmt))

    if not rows:
        print("등록된 ChangeSet이 없습니다.")
        return 0

    print(f"{'ID':<36} | {'Status':<18} | {'Session':<36} | {'Head SHA':<8} | {'Branch'}")
    print("-" * 120)
    for cs in rows:
        head = (cs.head_sha or "")[:8]
        print(f"{str(cs.id):<36} | {cs.status:<18} | {str(cs.session_id):<36} | {head:<8} | {cs.branch_name}")

    return 0


async def cmd_submit(db: AsyncSession, change_set_id: uuid.UUID, head_sha: str | None = None) -> int:
    """ChangeSet 제출."""
    try:
        cs = await submit_change(db, change_set_id, head_sha)
        print(f"ChangeSet {cs.id} submitted successfully (head: {cs.head_sha[:8]}, status: {cs.status}).")
        return 0
    except Exception as e:
        print(f"Error submitting ChangeSet: {e}", file=sys.stderr)
        return 1


async def cmd_process_once(db: AsyncSession, repo_path: Path) -> int:
    """큐에서 1개의 ChangeSet을 가져와 직렬 검증 및 통합 처리."""
    cs = await claim_next_change(db)
    if cs is None:
        print("대기 중인 제출(submitted) ChangeSet이 없습니다.")
        return 0

    print(f"Processing ChangeSet {cs.id} for session {cs.session_id} (head: {cs.head_sha[:8]})...")
    res = await process_single_change(
        db, cs, repo_path, _dummy_validator, _dummy_validator
    )
    print(f"Result: {res.status} (Success: {res.success}, Reason: {res.reason})")
    return 0 if res.success else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Ttakkari AI-Native Delivery Pipeline CLI")
    sub = parser.add_subparsers(dest="subcommand", required=True)

    # status
    p_status = sub.add_parser("status", help="ChangeSet 상태 목록 조회")
    p_status.add_argument("--limit", type=int, default=20)

    # submit
    p_submit = sub.add_parser("submit", help="ChangeSet 제출")
    p_submit.add_argument("change_set_id", type=uuid.UUID)
    p_submit.add_argument("--head-sha", type=str, default=None)

    # process-once
    p_proc = sub.add_parser("process-once", help="큐에서 1개 항목 직렬 처리")
    p_proc.add_argument("--repo", type=Path, default=Path.cwd())

    # inspect-provenance
    p_prov = sub.add_parser("inspect-provenance", help="커밋 트레일러 출처 확인")
    p_prov.add_argument("commit_sha", type=str)
    p_prov.add_argument("--repo", type=Path, default=Path.cwd())

    args = parser.parse_args(argv)

    if args.subcommand == "inspect-provenance":
        trailers = inspect_commit_provenance(args.repo, args.commit_sha)
        print(json.dumps(trailers, indent=2, ensure_ascii=False))
        return 0

    s = get_settings()
    engine = create_async_engine(str(s.database_url))
    session_factory = async_sessionmaker(engine, expire_on_commit=False)

    async def _run() -> int:
        async with session_factory() as db:
            if args.subcommand == "status":
                return await cmd_status(db, args.limit)
            elif args.subcommand == "submit":
                return await cmd_submit(db, args.change_set_id, args.head_sha)
            elif args.subcommand == "process-once":
                return await cmd_process_once(db, args.repo)
            return 0

    return asyncio.run(_run())


if __name__ == "__main__":
    sys.exit(main())
