"""Registered GitHub account metadata and explicit active-account switching for owner agents."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import text

from ttakkari.api.agent import _session
from ttakkari.api.deps import Db
from ttakkari.audit import record
from ttakkari.github import service
from ttakkari.github.errors import GithubError
from ttakkari.orchestrator.runtime import OrchestratorSession

router = APIRouter(prefix="/api/agent/github", tags=["github-agent"])


class SwitchAccount(BaseModel):
    model_config = ConfigDict(extra="forbid")
    account: str = Field(min_length=1, max_length=39)


@router.get("/accounts")
async def accounts(sid: OrchestratorSession, db: Db) -> dict:
    await _session(db, sid)
    rows = await service.get_service().registry.list_accounts()
    return {"accounts": [{"login": a.login, "active": a.active, "state": a.state} for a in rows]}


@router.post("/switch-account")
async def switch_account(body: SwitchAccount, sid: OrchestratorSession, db: Db) -> dict:
    await _session(db, sid)
    # Serialize explicit switching across API generations; never wait behind another switch.
    if not await db.scalar(text("SELECT pg_try_advisory_xact_lock(7171228301)")):
        raise HTTPException(409, "다른 GitHub 계정 전환이 진행 중입니다.")
    try:
        result = await service.get_service().registry.switch_account(body.account)
    except GithubError as exc:
        record(db, actor="agent", action="github.switch_account", outcome="error",
               detail={"session_id": str(sid), "account": body.account, "code": exc.code,
                       "reason": exc.reason})
        await db.commit()
        raise
    record(db, actor="agent", action="github.switch_account",
           detail={"session_id": str(sid), "account": result["account"]})
    await db.commit()
    return result
