"""ArtCraft 관제 API. 디자인시스템과 별개 도메인이며 로직은 ttakkari.artcraft 에 있다."""
from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, Field

from ttakkari import artcraft
from ttakkari.api.deps import Db
from ttakkari.audit import record
from ttakkari.security.auth import User, client_ip

router = APIRouter(prefix="/api/artcraft", tags=["artcraft"])


def _http(exc: artcraft.ArtcraftError) -> HTTPException:
    return HTTPException(exc.status, {"code": exc.code, "message": exc.message})


class SmokeIn(BaseModel):
    apps: list[str] | None = Field(default=None, max_length=20)
    mode: Literal["cli", "full"] = "cli"
    confirm: bool = False


class GuiIn(BaseModel):
    action: Literal["start", "stop"]
    confirm: bool = False


class BugIn(BaseModel):
    status: Literal["unreported", "reported", "resolved"]
    note: str | None = Field(default=None, max_length=artcraft.NOTE_LIMIT)


@router.get("/apps")
async def apps(_: User, response: Response, refresh: bool = False) -> dict:
    response.headers["Cache-Control"] = "no-store"
    try:
        return await artcraft.overview(force=refresh)
    except artcraft.ArtcraftError as exc:
        raise _http(exc) from exc


@router.post("/remote-check", status_code=202)
async def remote_check(_: User) -> dict:
    try:
        return artcraft.start_remote_check(manual=True).public()
    except artcraft.ArtcraftError as exc:
        raise _http(exc) from exc


@router.post("/smoke", status_code=202)
async def smoke(body: SmokeIn, request: Request, _: User, db: Db) -> dict:
    try:
        job = await artcraft.start_smoke(body.apps, body.mode, body.confirm)
    except artcraft.ArtcraftError as exc:
        raise _http(exc) from exc
    record(db, action="artcraft.smoke", target_type="artcraft", detail={"mode": body.mode, "apps": job.apps},
           ip=client_ip(request))
    await db.commit()
    return job.public()


@router.post("/apps/{app}/gui", status_code=202)
async def gui(app: str, body: GuiIn, request: Request, _: User, db: Db) -> dict:
    try:
        job = await artcraft.start_gui(app, body.action, body.confirm)
    except artcraft.ArtcraftError as exc:
        raise _http(exc) from exc
    record(db, action=f"artcraft.gui.{body.action}", target_type="artcraft_app", detail={"app": app},
           ip=client_ip(request))
    await db.commit()
    return job.public()


@router.get("/jobs")
async def jobs(_: User) -> dict:
    return {"items": [j.public() for j in reversed(list(artcraft.JOBS.values()))]}


@router.get("/jobs/{job_id}")
async def job(job_id: str, _: User, response: Response) -> dict:
    response.headers["Cache-Control"] = "no-store"
    try:
        return artcraft.get_job(job_id).public()
    except artcraft.ArtcraftError as exc:
        raise _http(exc) from exc


@router.get("/bugs")
async def bugs(_: User) -> dict:
    try:
        return artcraft.list_bugs()
    except artcraft.ArtcraftError as exc:
        raise _http(exc) from exc


@router.put("/bugs/{bug_id}")
async def put_bug(bug_id: str, body: BugIn, _: User) -> dict:
    try:
        return artcraft.set_bug(bug_id, body.status, body.note)
    except artcraft.ArtcraftError as exc:
        raise _http(exc) from exc


@router.get("/agent-config")
async def agent_config(_: User) -> dict:
    try:
        return await artcraft.agent_config()
    except artcraft.ArtcraftError as exc:
        raise _http(exc) from exc
