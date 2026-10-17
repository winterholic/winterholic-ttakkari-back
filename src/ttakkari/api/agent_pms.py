"""세션 토큰으로 PMS 전체를 관리한다. 사용자 API의 검증·트리 연산을 재사용한다."""
from __future__ import annotations

import json
import uuid
from datetime import date
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from ttakkari.api import pms
from ttakkari.api.agent import _session
from ttakkari.api.deps import Db
from ttakkari.audit import record
from ttakkari.orchestrator.runtime import OrchestratorSession as AgentSession
from ttakkari.pms import PmsOperation
from ttakkari.pms import service as svc

router = APIRouter(prefix="/api/agent/pms", tags=["agent"])


class PmsIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    operation: PmsOperation
    project_id: uuid.UUID | None = None
    item_id: uuid.UUID | None = None
    data: dict[str, Any] = Field(default_factory=dict)


class ProjectQuery(BaseModel):
    scope: pms.Scope = "all"
    status: pms.ProjectStatus | None = None
    q: str | None = Field(default=None, max_length=200)


class NoteQuery(BaseModel):
    status: Literal["open", "resolved", "all"] = "all"
    kind: pms.NoteKind | None = None


class TaskQuery(BaseModel):
    """list_tasks 선택 필터와 페이지. 모두 생략하면 compact 50개."""
    parent_id: uuid.UUID | None = None  # 직계 자식만. 비우면 필터 없음(최상위만은 depth=0)
    depth: int | None = Field(default=None, ge=0, lt=svc.MAX_DEPTH)
    max_depth: int | None = Field(default=None, ge=0, lt=svc.MAX_DEPTH)
    status: pms.TaskStatus | None = None
    q: str | None = Field(default=None, max_length=200)
    compact: bool = True
    limit: int | None = Field(default=None, ge=1, le=svc.COMPACT_LIMIT_MAX)  # 기본 compact 50 / 전체 25
    offset: int = Field(default=0, ge=0)


class DashboardQuery(BaseModel):
    today: date | None = None


class EmptyData(BaseModel):
    pass


def _data(model: type[BaseModel], value: dict[str, Any]) -> Any:
    unknown = set(value) - model.model_fields.keys()
    if unknown:
        raise HTTPException(422, {"code": "bad_fields", "message": f"알 수 없는 PMS 필드: {', '.join(sorted(unknown))}"})
    try:
        return model.model_validate(value)
    except ValidationError as exc:
        raise HTTPException(422, json.loads(exc.json())) from exc


def _required(value: uuid.UUID | None, name: str) -> uuid.UUID:
    if value is None:
        raise HTTPException(422, {"code": "missing_id", "message": f"{name}가 필요합니다."})
    return value


@router.post("")
async def manage(body: PmsIn, sid: AgentSession, db: Db, request: Request) -> Any:
    session = await _session(db, sid)
    if session.archived_at is not None:
        raise HTTPException(403, "보관된 세션입니다.")
    work_only = session.pool == "work"
    op, data = body.operation, body.data

    if op == "list_projects":
        query = _data(ProjectQuery, data)
        return await pms.list_projects(_="agent", db=db, scope="work" if work_only else query.scope,
                                       status_=query.status, q=query.q)

    if op == "create_project":
        model = _data(pms.ProjectIn, {"scope": "work" if work_only else "personal", **data})
        if work_only and model.scope != "work":
            raise HTTPException(403, "회사 세션에서는 회사 프로젝트만 관리할 수 있습니다.")
        return await pms.create_project(body=model, _="agent", db=db, request=request)

    # 모든 기존 항목의 접근은 그 소유 프로젝트를 통해 검사한다. 작업/마일스톤/이슈 ID로도 우회할 수 없다.
    item_id = body.item_id
    if op in ("update_task", "move_task", "delete_task"):
        item_id = _required(item_id, "item_id")
        project_id = (await pms._task(db, item_id)).project_id
    elif op in ("update_milestone", "delete_milestone"):
        item_id = _required(item_id, "item_id")
        project_id = (await pms._milestone(db, item_id)).project_id
    elif op in ("update_note", "delete_note"):
        item_id = _required(item_id, "item_id")
        project_id = (await pms._note(db, item_id)).project_id
    else:
        project_id = _required(body.project_id, "project_id")
    project = await pms._project(db, project_id)
    if work_only and project.scope != "work":
        raise pms._nf("프로젝트")

    common = {"_": "agent", "db": db}
    if op == "get_project":
        _data(EmptyData, data)
        return await pms.get_project(project_id, **common)
    if op == "dashboard":
        return await pms.project_dashboard(project_id, **common, today=_data(DashboardQuery, data).today)
    if op == "list_tasks":
        query = _data(TaskQuery, data)
        limit = query.limit or (svc.DEFAULT_LIMIT if query.compact else svc.FULL_LIMIT_MAX)
        if not query.compact and limit > svc.FULL_LIMIT_MAX:
            raise HTTPException(422, {"code": "limit_too_large", "message":
                                      f"compact=false 에서는 limit 을 {svc.FULL_LIMIT_MAX} 이하로 쓰세요. parent_id·q 로 좁히거나 offset 으로 나눠 읽으세요."})
        return await svc.tasks_page(db, project_id, parent_id=query.parent_id, depth=query.depth, max_depth=query.max_depth,
                                    status_=query.status, q=query.q, compact=query.compact, limit=limit,
                                    offset=query.offset)
    if op == "list_milestones":
        _data(EmptyData, data)
        return await pms.list_milestones(project_id, **common)
    if op == "list_notes":
        query = _data(NoteQuery, data)
        return await pms.list_notes(project_id, **common, status_=query.status, kind=query.kind)

    # UUID·필드·선행관계·집계 규칙 검증과 실제 변경은 사용자 화면과 동일한 함수가 처리한다.
    actions = {
        "update_project": (pms.patch_project, pms.ProjectPatch, project_id),
        "create_task": (pms.create_task, pms.TaskIn, project_id),
        "update_task": (pms.patch_task, pms.TaskPatch, item_id),
        "move_task": (pms.move_task, pms.MoveIn, item_id),
        "create_milestone": (pms.create_milestone, pms.MilestoneIn, project_id),
        "update_milestone": (pms.patch_milestone, pms.MilestonePatch, item_id),
        "create_note": (pms.create_note, pms.NoteIn, project_id),
        "update_note": (pms.patch_note, pms.NotePatch, item_id),
    }
    deletes = {"delete_project": pms.delete_project, "delete_task": pms.delete_task,
               "delete_milestone": pms.delete_milestone, "delete_note": pms.delete_note}
    if op in actions:
        fn, schema, ident = actions[op]
        model = _data(schema, data)
        if op == "update_project" and work_only and model.scope == "personal":
            raise HTTPException(403, "회사 프로젝트를 개인 범위로 옮길 수 없습니다.")
        record(db, actor="agent", action=f"pms.agent.{op}", target_type="pms_project", target_id=project_id,
               detail={"session_id": str(sid), "item_id": str(item_id) if item_id else None, "fields": list(data)})
        return await fn(ident, body=model, **common)
    _data(EmptyData, data)
    if op == "delete_project":
        await pms.delete_project(project_id, **common, request=request)
        return {"deleted": True}
    record(db, actor="agent", action=f"pms.agent.{op}", target_type="pms_project", target_id=project_id,
           detail={"session_id": str(sid), "item_id": str(item_id)})
    result = await deletes[op](item_id, **common)
    return result if result is not None else {"deleted": True}
