"""Bounded account-scoped git operations for personal agent sessions only."""
from __future__ import annotations

from pathlib import Path
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from ttakkari.api.deps import Db
from ttakkari.audit import record
from ttakkari.broker.policy import BrokerError, validate_workspace_root
from ttakkari.config import get_settings
from ttakkari.github.git_ops import GithubGitOperations, GitOperationError
from ttakkari.github.service import get_service
from ttakkari.models import ChatSession, Workspace
from ttakkari.orchestrator.runtime import OrchestratorSession


class GitOperationIn(BaseModel):
    operation: Literal["clone", "pull"]
    account: str = Field(min_length=1, max_length=39)
    owner: str = Field(min_length=1, max_length=39)
    repo: str = Field(min_length=1, max_length=100)


router = APIRouter(prefix="/api/agent/github", tags=["github-agent"])


async def _personal_session(sid: OrchestratorSession, db: Db) -> tuple[ChatSession, Workspace]:
    session = await db.get(ChatSession, sid)
    if session is None or session.pool == "work":
        raise HTTPException(403, "GitHub 저장소 작업은 개인 세션에서만 사용할 수 있습니다.")
    workspace = await db.get(Workspace, session.workspace_id)
    if workspace is None or workspace.archived_at is not None:
        raise HTTPException(404, "현재 세션의 작업 공간을 찾을 수 없습니다.")
    try:
        root = validate_workspace_root(workspace.root_path, get_settings().allowed_roots)
    except BrokerError as exc:
        raise HTTPException(403, {"code": "workspace_unavailable", "message": "현재 세션의 작업 공간 경로를 허용할 수 없습니다."}) from exc
    workspace.root_path = str(root)
    return session, workspace


def get_git_operations() -> GithubGitOperations:
    return GithubGitOperations(get_service().registry)


@router.post("/git")
async def git_operation(body: GitOperationIn, sid: OrchestratorSession, db: Db, request: Request,
                        ops: Annotated[GithubGitOperations, Depends(get_git_operations)]) -> dict:
    session, workspace = await _personal_session(sid, db)
    try:
        result = await ops.run(body.operation, body.account, body.owner, body.repo, Path(workspace.root_path))
    except GitOperationError as exc:
        raise HTTPException(422 if exc.code in {"invalid_target", "unsupported_operation", "unsafe_path"}
                            else 409 if exc.code in {"destination_exists", "working_tree_dirty", "upstream_mismatch", "remote_mismatch"}
                            else 503, {"code": exc.code, "message": exc.message}) from exc
    record(db, actor="agent", action=f"github.git.{body.operation}", target_type="github_repository",
           target_id=body.repo, detail={"account": result["account"], "owner": body.owner,
                                        "session": str(session.id), "path": result["path"]},
           ip=request.client.host if request.client else None)
    await db.commit()
    return result
