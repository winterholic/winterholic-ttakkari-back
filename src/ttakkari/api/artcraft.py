"""ArtCraft 관제 API. 디자인시스템과 별개 도메인이며 로직은 ttakkari.artcraft 에 있다."""
from __future__ import annotations

import uuid
from typing import Literal

from fastapi import APIRouter, HTTPException, Query, Request, Response
from pydantic import BaseModel, Field

from ttakkari import artcraft, artcraft_tasks
from ttakkari import artcraft_catalog as catalog
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
    status: Literal["candidate", "selected", "in_progress", "reported", "resolved", "deferred", "excluded"]
    note: str | None = Field(default=None, max_length=artcraft.NOTE_LIMIT)
    url: str | None = Field(default=None, max_length=artcraft.URL_LIMIT)


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
        return artcraft.set_bug(bug_id, body.status, body.note, body.url)
    except artcraft.ArtcraftError as exc:
        raise _http(exc) from exc


@router.get("/agent-config")
async def agent_config(_: User) -> dict:
    try:
        return await artcraft.agent_config()
    except artcraft.ArtcraftError as exc:
        raise _http(exc) from exc


# ---------------------------------------------------------------- 작업 플랫폼(ADR 0039)

class TaskIn(BaseModel):
    category: str = Field(min_length=1, max_length=20)
    prompt: str = Field(min_length=1, max_length=catalog.PROMPT_MAX)
    template_id: str | None = Field(default=None, max_length=40)
    input_artifact_ids: list[uuid.UUID] = Field(default_factory=list, max_length=catalog.FILES_MAX)
    apps: list[str] | None = Field(default=None, max_length=13)
    options: dict[str, str] = Field(default_factory=dict)
    speed: Literal["fast", "normal", "careful"] = "normal"


class RoundIn(BaseModel):
    prompt: str = Field(min_length=1, max_length=catalog.PROMPT_MAX)
    input_artifact_ids: list[uuid.UUID] = Field(default_factory=list, max_length=catalog.FILES_MAX)
    use_previous: bool = True
    options: dict[str, str] | None = None
    speed: Literal["fast", "normal", "careful"] = "normal"


class TaskPatch(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=200)
    archived: bool | None = None


@router.get("/catalog")
async def get_catalog(_: User, db: Db, response: Response) -> dict:
    """묶음·범주·템플릿·되는 일/안 되는 일. 앱 설치 여부와 작업용 워크스페이스를 함께 준다(느린 호출 없음)."""
    response.headers["Cache-Control"] = "no-store"
    try:
        known = artcraft.registry()
        available, reason = True, None
    except artcraft.ArtcraftError as exc:
        known, available, reason = {}, False, exc.message
    root = artcraft.root()
    installed = {app: (root / "bin" / f"{app}-cli").exists() for app in known}
    workspace_id, workspace_error = None, None
    try:
        workspace_id = str((await artcraft_tasks.ensure_workspace(db)).id)
    except artcraft.ArtcraftError as exc:
        workspace_error = exc.message
    return {
        "sections": [{"id": sid, "title": title, "categories": [c.id for c in catalog.CATEGORIES if c.section == sid]}
                     for sid, title in catalog.SECTIONS],
        "categories": [catalog.public(c) | {"apps": [
            a | {"installed": installed.get(a["app"], False)} for a in catalog.public(c)["apps"]]}
            for c in catalog.CATEGORIES],
        "available": available, "unavailable_reason": reason,
        "workspace_id": workspace_id, "workspace_error": workspace_error,
        "limits": {"files": catalog.FILES_MAX, "prompt": catalog.PROMPT_MAX},
    }


@router.post("/tasks", status_code=201)
async def create_task(body: TaskIn, request: Request, _: User, db: Db) -> dict:
    try:
        task, run = await artcraft_tasks.create_task(
            db, category_id=body.category, prompt=body.prompt, template_id=body.template_id,
            input_ids=body.input_artifact_ids, apps=body.apps, options=body.options, speed=body.speed,
            ip=client_ip(request))
    except artcraft.ArtcraftError as exc:
        raise _http(exc) from exc
    record(db, action="artcraft.task.create", target_type="artcraft_task", target_id=task.id,
           detail={"category": task.category, "apps": task.apps, "run_id": str(run.id)}, ip=client_ip(request))
    await db.commit()
    return await artcraft_tasks.task_detail(db, task)


@router.get("/tasks")
async def list_tasks(_: User, db: Db, category: str | None = Query(None, max_length=20),
                     state: Literal["active", "all"] = "all", session_id: uuid.UUID | None = None,
                     limit: int = Query(40, ge=1, le=100)) -> dict:
    return {"items": await artcraft_tasks.list_tasks(db, category_id=category, state=state, limit=limit,
                                                     session_id=session_id)}


@router.get("/tasks/{task_id}")
async def get_task(task_id: uuid.UUID, _: User, db: Db, response: Response) -> dict:
    response.headers["Cache-Control"] = "no-store"
    try:
        return await artcraft_tasks.task_detail(db, await artcraft_tasks.get_task(db, task_id))
    except artcraft.ArtcraftError as exc:
        raise _http(exc) from exc


@router.post("/tasks/{task_id}/runs", status_code=202)
async def add_round(task_id: uuid.UUID, body: RoundIn, request: Request, _: User, db: Db) -> dict:
    try:
        task = await artcraft_tasks.get_task(db, task_id)
        run = await artcraft_tasks.add_round(
            db, task, prompt=body.prompt, input_ids=body.input_artifact_ids, use_previous=body.use_previous,
            options=body.options, speed=body.speed, ip=client_ip(request))
    except artcraft.ArtcraftError as exc:
        raise _http(exc) from exc
    record(db, action="artcraft.task.round", target_type="artcraft_task", target_id=task.id,
           detail={"run_id": str(run.id), "use_previous": body.use_previous}, ip=client_ip(request))
    await db.commit()
    return await artcraft_tasks.task_detail(db, task)


@router.patch("/tasks/{task_id}")
async def patch_task(task_id: uuid.UUID, body: TaskPatch, _: User, db: Db) -> dict:
    try:
        task = await artcraft_tasks.get_task(db, task_id)
    except artcraft.ArtcraftError as exc:
        raise _http(exc) from exc
    data = body.model_dump(exclude_unset=True)
    if data.get("title"):
        task.title = data["title"].strip()[:200]
    if "archived" in data and data["archived"] is not None:
        from ttakkari.models import utcnow
        task.archived_at = utcnow() if data["archived"] else None
    await db.commit()
    return await artcraft_tasks.task_detail(db, task)


@router.get("/outputs")
async def outputs(_: User, db: Db, category: str | None = Query(None, max_length=20),
                  kind: str | None = Query(None, max_length=20), q: str | None = Query(None, max_length=100),
                  limit: int = Query(40, ge=1, le=100), offset: int = Query(0, ge=0, le=10_000)) -> dict:
    return await artcraft_tasks.outputs_gallery(db, category_id=category, kind=kind, q=q, limit=limit, offset=offset)
