"""프로젝트 관리 PMS · WBS API(ADR 0012). 칸반의 카드와 WBS 의 줄은 같은 pms_tasks 행이다.

작업을 바꾸는 호출은 번호·집계가 다시 계산된 전체 작업 목록(`tasks`)을 함께 돌려준다.
화면이 한 번의 왕복으로 트리를 맞추고, 에이전트(MCP)가 붙어도 같은 모양을 읽는다.
"""

from __future__ import annotations

import uuid
from datetime import date
from typing import Annotated, Any, Literal

from fastapi import APIRouter, HTTPException, Query, Request, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ttakkari.api.deps import Db
from ttakkari.audit import record
from ttakkari.models import PmsMilestone, PmsNote, PmsProject, PmsTask, utcnow
from ttakkari.pms import service as svc
from ttakkari.security.auth import User, client_ip

router = APIRouter(prefix="/api/pms", tags=["pms"])
Scope = Literal["all", "personal", "work"]
ProjectStatus = Literal["planning", "active", "on_hold", "done"]
TaskStatus = Literal["todo", "doing", "review", "done"]
Priority = Literal["low", "normal", "high", "urgent"]
NoteKind = Literal["issue", "risk", "note"]
Severity = Literal["low", "medium", "high"]
Name = Annotated[str, Field(max_length=100)]


def clean_names(names: list[str]) -> list[str]:
    out: list[str] = []
    for n in names:
        n = n.strip()
        if n and n not in out:
            out.append(n)
    return out[:50]


class ProjectIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    description: str = Field(default="", max_length=10_000)
    scope: Literal["personal", "work"] = "personal"
    status: ProjectStatus = "planning"
    start_date: date | None = None
    end_date: date | None = None
    members: list[Name] = Field(default_factory=list, max_length=50)
    # 새 프로젝트에 미리 넣을 최상위 단계 이름(WBS 1, 2, 3 …). 비워 두면 빈 프로젝트.
    phases: list[Annotated[str, Field(min_length=1, max_length=300)]] = Field(default_factory=list, max_length=20)


class ProjectPatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=10_000)
    scope: Literal["personal", "work"] | None = None
    status: ProjectStatus | None = None
    start_date: date | None = None
    end_date: date | None = None
    members: list[Name] | None = Field(default=None, max_length=50)


class TaskIn(BaseModel):
    title: str = Field(min_length=1, max_length=300)
    parent_id: uuid.UUID | None = None
    # 이 작업 바로 아래(같은 부모의 다음 형제)에 넣는다. 주면 parent_id 는 무시한다.
    after_id: uuid.UUID | None = None
    description: str = Field(default="", max_length=20_000)
    priority: Priority = "normal"
    assignee: Name | None = None
    start_date: date | None = None
    end_date: date | None = None
    status: TaskStatus | None = None
    progress: int | None = Field(default=None, ge=0, le=100)
    predecessors: list[uuid.UUID] = Field(default_factory=list, max_length=30)


class TaskPatch(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=300)
    description: str | None = Field(default=None, max_length=20_000)
    priority: Priority | None = None
    # 보내지 않으면 그대로, null 이면 비운다(model_fields_set 로 구분).
    assignee: Name | None = None
    start_date: date | None = None
    end_date: date | None = None
    status: TaskStatus | None = None
    progress: int | None = Field(default=None, ge=0, le=100)
    predecessors: list[uuid.UUID] | None = Field(default=None, max_length=30)


class MoveIn(BaseModel):
    parent_id: uuid.UUID | None = None
    # 새 부모 아래에서 몇 번째에 둘지(0 부터, 이 작업을 뺀 목록 기준). 없으면 맨 끝.
    index: int | None = Field(default=None, ge=0)


class MilestoneIn(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    due_date: date
    done: bool = False


class MilestonePatch(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=200)
    due_date: date | None = None
    done: bool | None = None


class NoteIn(BaseModel):
    kind: NoteKind = "issue"
    title: str = Field(min_length=1, max_length=300)
    body: str = Field(default="", max_length=20_000)
    severity: Severity = "medium"
    owner: Name | None = None
    task_id: uuid.UUID | None = None


class NotePatch(BaseModel):
    kind: NoteKind | None = None
    title: str | None = Field(default=None, min_length=1, max_length=300)
    body: str | None = Field(default=None, max_length=20_000)
    severity: Severity | None = None
    owner: Name | None = None
    task_id: uuid.UUID | None = None
    status: Literal["open", "resolved"] | None = None


def _nf(what: str) -> HTTPException:
    return HTTPException(status.HTTP_404_NOT_FOUND, {"code": "not_found", "message": f"{what}이(가) 없어요."})


async def _project(db: AsyncSession, pid: uuid.UUID) -> PmsProject:
    p = await db.get(PmsProject, pid)
    if p is None:
        raise _nf("프로젝트")
    return p


async def _task(db: AsyncSession, tid: uuid.UUID) -> PmsTask:
    t = await db.get(PmsTask, tid)
    if t is None:
        raise _nf("작업")
    return t


def _opt(v: str | None) -> str | None:
    v = (v or "").strip()
    return v or None


async def _one(db: AsyncSession, p: PmsProject) -> dict[str, Any]:
    return (await svc.projects_out(db, [p]))[0]


# ---- 프로젝트

@router.get("/projects")
async def list_projects(_: User, db: Db, scope: Scope = "all", status_: Annotated[ProjectStatus | None, Query(alias="status")] = None,
                        q: Annotated[str | None, Query(max_length=200)] = None) -> dict[str, Any]:
    stmt = select(PmsProject).order_by(PmsProject.created_at.desc())
    if scope != "all":
        stmt = stmt.where(PmsProject.scope == scope)
    if status_:
        stmt = stmt.where(PmsProject.status == status_)
    if q:
        stmt = stmt.where(PmsProject.name.ilike(f"%{q.strip()}%"))
    return {"projects": await svc.projects_out(db, list(await db.scalars(stmt)))}


@router.post("/projects", status_code=status.HTTP_201_CREATED)
async def create_project(body: ProjectIn, _: User, db: Db, request: Request) -> dict[str, Any]:
    svc.check_dates(body.start_date, body.end_date)
    p = PmsProject(id=uuid.uuid4(), name=body.name.strip(), description=body.description, scope=body.scope, status=body.status,
                   start_date=body.start_date, end_date=body.end_date, members=clean_names(body.members))
    db.add(p)
    for i, title in enumerate(body.phases):
        db.add(PmsTask(project_id=p.id, position=i, title=title.strip()))
    record(db, action="pms.project.create", target_type="pms_project", target_id=p.id, ip=client_ip(request),
           detail={"scope": p.scope, "phases": len(body.phases)})
    await db.commit()
    return await _one(db, p)


@router.get("/projects/{pid}")
async def get_project(pid: uuid.UUID, _: User, db: Db) -> dict[str, Any]:
    return await _one(db, await _project(db, pid))


@router.patch("/projects/{pid}")
async def patch_project(pid: uuid.UUID, body: ProjectPatch, _: User, db: Db) -> dict[str, Any]:
    p = await _project(db, pid)
    data = body.model_dump(exclude_unset=True)
    svc.check_dates(data.get("start_date", p.start_date),
                    data.get("end_date", p.end_date))
    for k in ("description", "scope", "status"):
        if data.get(k) is not None:
            setattr(p, k, data[k])
    for k in ("start_date", "end_date"):
        if k in data:
            setattr(p, k, data[k])
    if data.get("name"):
        p.name = data["name"].strip()
    if data.get("members") is not None:
        p.members = clean_names(data["members"])
    await db.commit()
    return await _one(db, p)


@router.delete("/projects/{pid}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_project(pid: uuid.UUID, _: User, db: Db, request: Request) -> None:
    """프로젝트와 그 작업·마일스톤·이슈를 모두 지운다."""
    p = await _project(db, pid)
    record(db, action="pms.project.delete", target_type="pms_project", target_id=p.id, ip=client_ip(request),
           detail={"name": p.name})
    await db.delete(p)
    await db.commit()


@router.get("/projects/{pid}/dashboard")
async def project_dashboard(pid: uuid.UUID, _: User, db: Db, today: date | None = None) -> dict[str, Any]:
    """today 는 테스트·미리보기용. 비우면 한국 시간 오늘."""
    return await svc.dashboard(db, await _project(db, pid), today)


# ---- 작업(WBS · 칸반)

@router.get("/projects/{pid}/tasks")
async def list_tasks(pid: uuid.UUID, _: User, db: Db) -> dict[str, Any]:
    await _project(db, pid)
    return {"tasks": await svc.tasks_out(db, pid)}


@router.post("/projects/{pid}/tasks", status_code=status.HTTP_201_CREATED)
async def create_task(pid: uuid.UUID, body: TaskIn, _: User, db: Db) -> dict[str, Any]:
    await _project(db, pid)
    tasks = await svc.load_tasks(db, pid)
    if len(tasks) >= svc.MAX_TASKS:
        raise svc.bad(f"작업은 프로젝트당 {svc.MAX_TASKS}개까지예요.", "too_many")
    by_id = {t.id: t for t in tasks}
    parent_id = body.parent_id
    if body.after_id:
        sib = by_id.get(body.after_id)
        if sib is None:
            raise _nf("기준 작업")
        parent_id = sib.parent_id
    elif parent_id and parent_id not in by_id:
        raise _nf("상위 작업")
    _, nodes = svc.build(tasks)
    if parent_id and nodes[parent_id].depth + 1 >= svc.MAX_DEPTH:
        raise svc.bad(f"작업은 {svc.MAX_DEPTH}단계까지만 나눌 수 있어요.", "too_deep")
    svc.check_dates(body.start_date, body.end_date)
    t = PmsTask(project_id=pid, parent_id=parent_id, title=body.title.strip(), description=body.description,
                priority=body.priority, assignee=_opt(body.assignee), start_date=body.start_date, end_date=body.end_date,
                status="todo", progress=0)
    t.id = uuid.uuid4()
    t.predecessors = svc.clean_predecessors(t, body.predecessors, tasks)
    if body.status:
        svc.set_status(t, body.status)
    if body.progress is not None:
        svc.set_progress(t, body.progress)
    sibs = svc.siblings(tasks, parent_id)
    idx = next((i + 1 for i, s in enumerate(sibs) if s.id == body.after_id), len(sibs))
    db.add(t)
    sibs.insert(idx, t)
    svc.resequence(sibs)
    await db.flush()
    await db.commit()
    return await _task_response(db, t)


async def _task_response(db: AsyncSession, t: PmsTask) -> dict[str, Any]:
    today = svc.today_kst()
    ordered, nodes = await svc.tree(db, t.project_id)
    return {"task": svc.task_out(nodes[t.id], today), "tasks": [svc.task_out(n, today) for n in ordered]}


@router.patch("/tasks/{tid}")
async def patch_task(tid: uuid.UUID, body: TaskPatch, _: User, db: Db) -> dict[str, Any]:
    t = await _task(db, tid)
    tasks = await svc.load_tasks(db, t.project_id)
    _, nodes = svc.build(tasks)
    node = nodes[t.id]
    data = body.model_dump(exclude_unset=True)
    if not node.is_leaf and ("status" in data or "progress" in data):
        raise svc.bad("하위 작업이 있는 작업의 상태·진행률은 하위 작업에서 자동으로 계산돼요.", "rollup")
    if node.dates_rollup and ("start_date" in data or "end_date" in data):
        raise svc.bad("하위 작업이 있는 작업의 기간은 하위 작업에서 자동으로 계산돼요.", "rollup")
    svc.check_dates(data.get("start_date", t.start_date),
                    data.get("end_date", t.end_date))
    if data.get("title"):
        t.title = data["title"].strip()
    if data.get("description") is not None:
        t.description = data["description"]
    if data.get("priority"):
        t.priority = data["priority"]
    if "assignee" in data:
        t.assignee = _opt(data["assignee"])
    for k in ("start_date", "end_date"):
        if k in data:
            setattr(t, k, data[k])
    if data.get("predecessors") is not None:
        t.predecessors = svc.clean_predecessors(t, data["predecessors"], tasks)
    if data.get("status"):
        svc.set_status(t, data["status"])
    if data.get("progress") is not None:
        svc.set_progress(t, data["progress"])
    await db.commit()
    return await _task_response(db, t)


@router.post("/tasks/{tid}/move")
async def move_task(tid: uuid.UUID, body: MoveIn, _: User, db: Db) -> dict[str, Any]:
    """들여쓰기·내어쓰기·순서 바꾸기 모두 이 호출 하나다."""
    t = await _task(db, tid)
    tasks = await svc.load_tasks(db, t.project_id)
    _, nodes = svc.build(tasks)
    node = nodes[t.id]
    if body.parent_id is not None:
        if body.parent_id not in nodes:
            raise _nf("상위 작업")
        if body.parent_id == t.id or body.parent_id in {d.task.id for d in svc.descendants(node)}:
            raise svc.bad("작업을 자기 아래로 옮길 수 없어요.", "bad_move")
    base = 0 if body.parent_id is None else nodes[body.parent_id].depth + 1
    if base + svc.subtree_height(node) >= svc.MAX_DEPTH:
        raise svc.bad(f"작업은 {svc.MAX_DEPTH}단계까지만 나눌 수 있어요.", "too_deep")
    old_parent = t.parent_id
    before = svc.snapshot_parents(nodes, [old_parent])
    t.parent_id = body.parent_id
    new_sibs = svc.siblings(tasks, body.parent_id, exclude=t.id)
    new_sibs.insert(len(new_sibs) if body.index is None else min(body.index, len(new_sibs)), t)
    svc.resequence(new_sibs)
    if old_parent != body.parent_id:
        svc.resequence(svc.siblings(tasks, old_parent, exclude=t.id))
        svc.settle_leaves(before, tasks)
    await db.commit()
    return await _task_response(db, t)


@router.delete("/tasks/{tid}")
async def delete_task(tid: uuid.UUID, _: User, db: Db) -> dict[str, Any]:
    """하위 작업까지 함께 지운다. 이 작업들을 선행으로 걸어 둔 곳에서는 참조만 뺀다."""
    t = await _task(db, tid)
    pid = t.project_id
    tasks = await svc.load_tasks(db, pid)
    _, nodes = svc.build(tasks)
    gone = {t.id} | {d.task.id for d in svc.descendants(nodes[t.id])}
    before = svc.snapshot_parents(nodes, [t.parent_id])
    remaining = [x for x in tasks if x.id not in gone]
    svc.drop_predecessor_refs(remaining, gone)
    svc.resequence(svc.siblings(remaining, t.parent_id))
    svc.settle_leaves(before, remaining)
    await db.delete(t)
    await db.commit()
    return {"removed": len(gone), "tasks": await svc.tasks_out(db, pid)}


# ---- 마일스톤

@router.get("/projects/{pid}/milestones")
async def list_milestones(pid: uuid.UUID, _: User, db: Db) -> dict[str, Any]:
    await _project(db, pid)
    today = svc.today_kst()
    rows = await db.scalars(select(PmsMilestone).where(PmsMilestone.project_id == pid).order_by(PmsMilestone.due_date))
    return {"milestones": [svc.milestone_out(m, today) for m in rows]}


@router.post("/projects/{pid}/milestones", status_code=status.HTTP_201_CREATED)
async def create_milestone(pid: uuid.UUID, body: MilestoneIn, _: User, db: Db) -> dict[str, Any]:
    await _project(db, pid)
    m = PmsMilestone(project_id=pid, title=body.title.strip(), due_date=body.due_date, done=body.done)
    db.add(m)
    await db.commit()
    return svc.milestone_out(m, svc.today_kst())


async def _milestone(db: AsyncSession, mid: uuid.UUID) -> PmsMilestone:
    m = await db.get(PmsMilestone, mid)
    if m is None:
        raise _nf("마일스톤")
    return m


@router.patch("/milestones/{mid}")
async def patch_milestone(mid: uuid.UUID, body: MilestonePatch, _: User, db: Db) -> dict[str, Any]:
    m = await _milestone(db, mid)
    data = body.model_dump(exclude_unset=True)
    if data.get("title"):
        m.title = data["title"].strip()
    if data.get("due_date"):
        m.due_date = data["due_date"]
    if data.get("done") is not None:
        m.done = data["done"]
    await db.commit()
    return svc.milestone_out(m, svc.today_kst())


@router.delete("/milestones/{mid}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_milestone(mid: uuid.UUID, _: User, db: Db) -> None:
    await db.delete(await _milestone(db, mid))
    await db.commit()


# ---- 이슈·리스크·메모

@router.get("/projects/{pid}/notes")
async def list_notes(pid: uuid.UUID, _: User, db: Db, status_: Annotated[Literal["open", "resolved", "all"], Query(alias="status")] = "all",
                     kind: NoteKind | None = None) -> dict[str, Any]:
    await _project(db, pid)
    stmt = select(PmsNote).where(PmsNote.project_id == pid)
    if status_ != "all":
        stmt = stmt.where(PmsNote.status == status_)
    if kind:
        stmt = stmt.where(PmsNote.kind == kind)
    rows = list(await db.scalars(stmt))
    rows.sort(key=lambda n: (n.status != "open", svc.SEVERITY_RANK.get(n.severity, 1), -n.created_at.timestamp()))
    return {"notes": [svc.note_out(n) for n in rows]}


async def _check_task(db: AsyncSession, pid: uuid.UUID, task_id: uuid.UUID | None) -> None:
    if task_id is not None:
        t = await db.get(PmsTask, task_id)
        if t is None or t.project_id != pid:
            raise svc.bad("연결할 작업이 이 프로젝트에 없어요.", "bad_task")


@router.post("/projects/{pid}/notes", status_code=status.HTTP_201_CREATED)
async def create_note(pid: uuid.UUID, body: NoteIn, _: User, db: Db) -> dict[str, Any]:
    await _project(db, pid)
    await _check_task(db, pid, body.task_id)
    n = PmsNote(project_id=pid, task_id=body.task_id, kind=body.kind, title=body.title.strip(), body=body.body,
                severity=body.severity, owner=_opt(body.owner), status="open")
    db.add(n)
    await db.commit()
    return svc.note_out(n)


async def _note(db: AsyncSession, nid: uuid.UUID) -> PmsNote:
    n = await db.get(PmsNote, nid)
    if n is None:
        raise _nf("이슈")
    return n


@router.patch("/notes/{nid}")
async def patch_note(nid: uuid.UUID, body: NotePatch, _: User, db: Db) -> dict[str, Any]:
    n = await _note(db, nid)
    data = body.model_dump(exclude_unset=True)
    if "task_id" in data:
        await _check_task(db, n.project_id, data["task_id"])
        n.task_id = data["task_id"]
    if data.get("title"):
        n.title = data["title"].strip()
    for k in ("kind", "severity"):
        if data.get(k):
            setattr(n, k, data[k])
    if data.get("body") is not None:
        n.body = data["body"]
    if "owner" in data:
        n.owner = _opt(data["owner"])
    if data.get("status"):
        n.resolved_at = utcnow() if data["status"] == "resolved" and n.status != "resolved" else (
            n.resolved_at if data["status"] == "resolved" else None)
        n.status = data["status"]
    await db.commit()
    return svc.note_out(n)


@router.delete("/notes/{nid}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_note(nid: uuid.UUID, _: User, db: Db) -> None:
    await db.delete(await _note(db, nid))
    await db.commit()
