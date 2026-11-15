"""저장소 가치 평가 API. 소유자 로그인 전용이고 에이전트·MCP 라우터는 두지 않는다(ADR 0073)."""
from __future__ import annotations

import uuid
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import BaseModel, Field

from ttakkari.api.deps import Db
from ttakkari.github.service import GithubService, get_service
from ttakkari.models import RepoAppraisal
from ttakkari.repo_appraisal import service
from ttakkari.security.auth import User


async def user_access(user: User) -> None:
    pass


router = APIRouter(prefix="/api/repo-appraisals", tags=["repo-appraisal"],
                   dependencies=[Depends(user_access)])
Gh = Annotated[GithubService, Depends(get_service)]


class StartIn(BaseModel):
    target: str = Field(min_length=1, max_length=2048)
    account: str = Field(min_length=1, max_length=64)
    mode: Literal["quick", "deep"] = "quick"
    force: bool = False


@router.post("", status_code=202)
async def start(body: StartIn, response: Response, db: Db, svc: Gh) -> dict:
    row, reused = await service.start(db, svc, target=body.target, account=body.account,
                                      mode=body.mode, force=body.force)
    await db.commit()
    if reused:
        response.status_code = 200
    delta = await service.previous_score(db, row)
    return service.detail_view(row, (row.weighted_score - delta) if reused and delta is not None
                               and row.weighted_score is not None else None)


@router.get("/{appraisal_id}")
async def detail(appraisal_id: uuid.UUID, db: Db) -> dict:
    row = await db.get(RepoAppraisal, appraisal_id)
    if row is None:
        raise HTTPException(404, {"code": "not_found", "message": "평가가 없습니다."})
    prev = await service.previous_score(db, row)
    delta = (row.weighted_score - prev) if row.status == "done" and prev is not None else None
    return service.detail_view(row, delta)


@router.get("")
async def listing(db: Db, owner: str = Query(min_length=1, max_length=39),
                  repo: str = Query(min_length=1, max_length=100),
                  limit: int = Query(default=20, ge=1, le=50)) -> dict:
    from sqlalchemy import select

    rows = list(await db.scalars(select(RepoAppraisal).where(
        RepoAppraisal.owner == owner, RepoAppraisal.repo == repo)
        .order_by(RepoAppraisal.created_at.desc()).limit(limit)))
    items = []
    for row in rows:
        prev = await service.previous_score(db, row)
        delta = (row.weighted_score - prev) if row.status == "done" and prev is not None else None
        items.append({"id": str(row.id), "owner": row.owner, "repo": row.repo,
                      "head_sha": row.head_sha, "mode": row.mode, "repo_type": row.repo_type,
                      "status": row.status, "weighted_score": row.weighted_score,
                      "grade": row.grade, "llm_used": row.llm_used, "delta": delta,
                      "created_at": service._iso(row.created_at),
                      "finished_at": service._iso(row.finished_at)})
    return {"items": items}
