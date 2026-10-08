from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from ttakkari.broker.policy import BrokerError
from ttakkari.db import get_db
from ttakkari.models import Artifact, ChatSession, Workspace

Db = Annotated[AsyncSession, Depends(get_db)]

BROKER_STATUS = {
    "not_found": status.HTTP_404_NOT_FOUND,
    "gone": status.HTTP_410_GONE,
    "bad_path": status.HTTP_400_BAD_REQUEST,
    "not_a_file": status.HTTP_400_BAD_REQUEST,
    "not_a_dir": status.HTTP_400_BAD_REQUEST,
}


def broker_http(e: BrokerError) -> HTTPException:
    return HTTPException(BROKER_STATUS.get(e.code, status.HTTP_403_FORBIDDEN), {"code": e.code, "message": str(e)})


async def get_workspace(db: AsyncSession, ws_id: uuid.UUID) -> Workspace:
    ws = await db.get(Workspace, ws_id)
    if ws is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "워크스페이스가 없습니다.")
    return ws


async def get_session(db: AsyncSession, session_id: uuid.UUID) -> ChatSession:
    s = await db.get(ChatSession, session_id)
    if s is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "세션이 없습니다.")
    return s


async def get_artifact(db: AsyncSession, art_id: uuid.UUID) -> Artifact:
    a = await db.get(Artifact, art_id)
    if a is None or a.deleted_at is not None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Artifact 가 없습니다.")
    return a
