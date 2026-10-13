"""프로젝트 관리 PMS·WBS 공통 로직(ADR 0012). 사용자 API 가 쓰고, 나중에 에이전트 API 도 같은 함수를 쓴다.

작업은 한 표(pms_tasks)에 트리로 둔다. 번호(1.1.2)와 상위 작업의 진행률·상태·기간은 저장하지 않고
읽을 때 계산한다. 그래서 순서를 바꾸거나 들여써도 번호가 어긋날 일이 없다.
"""

from __future__ import annotations

import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ttakkari.models import PmsMilestone, PmsNote, PmsProject, PmsTask, utcnow

KST = ZoneInfo("Asia/Seoul")
MAX_DEPTH = 8  # 단계 → 작업 → 하위 작업 … 이 이상은 WBS 로 읽히지 않는다
MAX_TASKS = 2000
DUE_SOON_DAYS = 7
SEVERITY_RANK = {"high": 0, "medium": 1, "low": 2}


def bad(message: str, code: str = "validation", http: int = status.HTTP_422_UNPROCESSABLE_CONTENT) -> HTTPException:
    return HTTPException(http, {"code": code, "message": message})


def today_kst() -> date:
    return datetime.now(KST).date()


# ---- 트리 구성

@dataclass
class Node:
    task: PmsTask
    children: list[Node] = field(default_factory=list)
    wbs: str = ""
    depth: int = 0
    # 집계된 값(말단은 자기 값)
    status: str = "todo"
    progress: int = 0
    start: date | None = None
    end: date | None = None
    dates_rollup: bool = False

    @property
    def is_leaf(self) -> bool:
        return not self.children

    def weight(self) -> int:
        """진행률 가중치: 기간(일). 날짜가 없으면 1일로 본다."""
        if self.start and self.end and self.end >= self.start:
            return (self.end - self.start).days + 1
        return 1


def _order_key(t: PmsTask) -> tuple[int, datetime]:
    return (t.position, t.created_at)


def build(tasks: list[PmsTask]) -> tuple[list[Node], dict[uuid.UUID, Node]]:
    """트리 순서(깊이 우선)로 편 노드 목록과 id 표. 부모가 없는 id 를 가리키는 작업은 최상위로 본다."""
    nodes = {t.id: Node(t) for t in tasks}
    by_parent: dict[uuid.UUID | None, list[Node]] = defaultdict(list)
    for n in nodes.values():
        pid = n.task.parent_id
        by_parent[pid if pid in nodes else None].append(n)
    for lst in by_parent.values():
        lst.sort(key=lambda n: _order_key(n.task))
    ordered: list[Node] = []

    def walk(parent: uuid.UUID | None, prefix: str, depth: int) -> None:
        for i, n in enumerate(by_parent.get(parent, []), start=1):
            n.wbs = f"{prefix}{i}"
            n.depth = depth
            n.children = by_parent.get(n.task.id, [])
            ordered.append(n)
            walk(n.task.id, f"{n.wbs}.", depth + 1)

    walk(None, "", 0)
    for n in reversed(ordered):  # 자식이 부모보다 뒤에 있으니 거꾸로 돌면 자식이 먼저 집계된다
        _rollup(n)
    return ordered, nodes


def _rollup(n: Node) -> None:
    t = n.task
    if n.is_leaf:
        n.status, n.progress, n.start, n.end, n.dates_rollup = t.status, t.progress, t.start_date, t.end_date, False
        return
    kids = n.children
    total = sum(k.weight() for k in kids)
    n.progress = round(sum(k.progress * k.weight() for k in kids) / total) if total else 0
    states = {k.status for k in kids}
    if states == {"done"}:
        n.status = "done"
    elif states <= {"review", "done"}:
        n.status = "review"
    elif states == {"todo"}:
        n.status = "todo"
    else:
        n.status = "doing"
    starts = [k.start for k in kids if k.start]
    ends = [k.end for k in kids if k.end]
    if starts or ends:
        n.start, n.end, n.dates_rollup = min(starts or ends), max(ends or starts), True
    else:
        n.start, n.end, n.dates_rollup = t.start_date, t.end_date, False


def is_overdue(n: Node, today: date) -> bool:
    return n.end is not None and n.end < today and n.status != "done"


def task_out(n: Node, today: date) -> dict[str, Any]:
    t = n.task
    return {
        "id": str(t.id), "project_id": str(t.project_id), "parent_id": str(t.parent_id) if t.parent_id else None,
        "position": t.position, "depth": n.depth, "wbs": n.wbs, "has_children": not n.is_leaf,
        "title": t.title, "description": t.description, "priority": t.priority, "assignee": t.assignee,
        "status": n.status, "progress": n.progress,
        "start_date": n.start.isoformat() if n.start else None, "end_date": n.end.isoformat() if n.end else None,
        "dates_rollup": n.dates_rollup, "overdue": is_overdue(n, today),
        "predecessors": [str(p) for p in t.predecessors],
        "created_at": t.created_at.isoformat(), "updated_at": t.updated_at.isoformat(),
        "done_at": t.done_at.isoformat() if t.done_at else None,
    }


async def load_tasks(db: AsyncSession, project_id: uuid.UUID) -> list[PmsTask]:
    return list(await db.scalars(select(PmsTask).where(PmsTask.project_id == project_id)))


async def tree(db: AsyncSession, project_id: uuid.UUID) -> tuple[list[Node], dict[uuid.UUID, Node]]:
    return build(await load_tasks(db, project_id))


async def tasks_out(db: AsyncSession, project_id: uuid.UUID, today: date | None = None) -> list[dict[str, Any]]:
    today = today or today_kst()
    ordered, _ = await tree(db, project_id)
    return [task_out(n, today) for n in ordered]


# ---- 상태·진행률 동기화(말단 작업)

def set_status(t: PmsTask, new: str) -> None:
    t.status = new
    if new == "done":
        t.progress = 100
        t.done_at = t.done_at or utcnow()
        return
    t.done_at = None
    if new == "todo":
        t.progress = 0
    elif t.progress >= 100:
        t.progress = 90  # 끝난 작업을 다시 열었는데 100% 로 남아 있으면 화면이 거짓말이 된다


def set_progress(t: PmsTask, p: int) -> None:
    t.progress = max(0, min(100, p))
    if t.progress == 100:
        set_status(t, "done")
    elif t.status == "done":
        t.status, t.done_at = "doing", None
    elif t.progress > 0 and t.status == "todo":
        t.status = "doing"


# ---- 구조 변경

def depth_of(nodes: dict[uuid.UUID, Node], task_id: uuid.UUID | None) -> int:
    """task_id 아래에 새로 놓을 자식의 깊이(0 부터)."""
    if task_id is None:
        return 0
    return nodes[task_id].depth + 1


def subtree_height(n: Node) -> int:
    return 0 if n.is_leaf else 1 + max(subtree_height(c) for c in n.children)


def descendants(n: Node) -> list[Node]:
    out: list[Node] = []
    stack = list(n.children)
    while stack:
        x = stack.pop()
        out.append(x)
        stack.extend(x.children)
    return out


def siblings(tasks: list[PmsTask], parent_id: uuid.UUID | None, exclude: uuid.UUID | None = None) -> list[PmsTask]:
    return sorted((t for t in tasks if t.parent_id == parent_id and t.id != exclude), key=_order_key)


def resequence(lst: list[PmsTask]) -> None:
    for i, t in enumerate(lst):
        if t.position != i:
            t.position = i


def snapshot_parents(nodes: dict[uuid.UUID, Node], ids: list[uuid.UUID | None]) -> dict[uuid.UUID, Node]:
    return {i: nodes[i] for i in ids if i is not None and i in nodes}


def settle_leaves(before: dict[uuid.UUID, Node], tasks: list[PmsTask]) -> None:
    """자식을 모두 잃은 상위 작업은 다시 말단이 된다. 마지막으로 보이던 집계 값을 자기 값으로 넘겨 화면이 갑자기 바뀌지 않게 한다."""
    parents = {t.parent_id for t in tasks if t.parent_id}
    for pid, n in before.items():
        if pid in parents:
            continue
        t = n.task
        t.status, t.progress = n.status, n.progress
        if n.dates_rollup:
            t.start_date, t.end_date = n.start, n.end


def check_dates(start: date | None, end: date | None) -> None:
    if start and end and end < start:
        raise bad("종료일이 시작일보다 빠를 수 없어요.", "bad_dates")


def clean_predecessors(task: PmsTask, ids: list[uuid.UUID], tasks: list[PmsTask]) -> list[str]:
    """선행 작업은 같은 프로젝트의 다른 작업이어야 하고, 순환(A→B→A)을 만들면 안 된다."""
    known = {t.id: t for t in tasks}
    out: list[uuid.UUID] = []
    for i in ids:
        if i == task.id:
            raise bad("자기 자신을 선행 작업으로 둘 수 없어요.", "bad_predecessor")
        if i not in known:
            raise bad("선행 작업은 같은 프로젝트의 작업이어야 해요.", "bad_predecessor")
        if i not in out:
            out.append(i)
    graph = {t.id: [uuid.UUID(str(p)) for p in t.predecessors] for t in tasks}
    graph[task.id] = out
    # task 에서 선행 쪽으로 거슬러 올라가 task 로 돌아오면 순환이다.
    seen: set[uuid.UUID] = set()
    stack = list(out)
    while stack:
        cur = stack.pop()
        if cur == task.id:
            raise bad("선후행이 서로 맞물려 순환해요.", "predecessor_cycle")
        if cur in seen:
            continue
        seen.add(cur)
        stack.extend(graph.get(cur, []))
    return [str(i) for i in out]


def drop_predecessor_refs(tasks: list[PmsTask], gone: set[uuid.UUID]) -> None:
    sgone = {str(g) for g in gone}
    for t in tasks:
        if t.id in gone:
            continue
        if any(str(p) in sgone for p in t.predecessors):
            t.predecessors = [p for p in t.predecessors if str(p) not in sgone]


# ---- 요약·대시보드

def _brief(n: Node, today: date) -> dict[str, Any]:
    return {"id": str(n.task.id), "wbs": n.wbs, "title": n.task.title, "assignee": n.task.assignee, "status": n.status,
            "progress": n.progress, "end_date": n.end.isoformat() if n.end else None,
            "days": (n.end - today).days if n.end else None, "priority": n.task.priority}


def overall(ordered: list[Node]) -> int:
    roots = [n for n in ordered if n.depth == 0]
    total = sum(r.weight() for r in roots)
    return round(sum(r.progress * r.weight() for r in roots) / total) if total else 0


def planned_progress(p: PmsProject, today: date) -> int | None:
    """기간 안에서 오늘까지 흘러간 비율. 기간이 없으면 비교할 기준이 없다."""
    if not (p.start_date and p.end_date) or p.end_date <= p.start_date:
        return None
    span = (p.end_date - p.start_date).days
    return max(0, min(100, round((today - p.start_date).days / span * 100)))


def counts_of(leaves: list[Node], today: date) -> dict[str, int]:
    c = {"total": len(leaves), "todo": 0, "doing": 0, "review": 0, "done": 0, "overdue": 0, "due_soon": 0, "no_dates": 0}
    for n in leaves:
        c[n.status] += 1
        if n.status == "done":
            continue
        if is_overdue(n, today):
            c["overdue"] += 1
        elif n.end and n.end <= today + timedelta(days=DUE_SOON_DAYS):
            c["due_soon"] += 1
        if n.end is None:
            c["no_dates"] += 1
    return c


def milestone_out(m: PmsMilestone, today: date) -> dict[str, Any]:
    return {"id": str(m.id), "project_id": str(m.project_id), "title": m.title, "due_date": m.due_date.isoformat(),
            "done": m.done, "days": (m.due_date - today).days, "overdue": (not m.done) and m.due_date < today}


def note_out(n: PmsNote) -> dict[str, Any]:
    return {"id": str(n.id), "project_id": str(n.project_id), "task_id": str(n.task_id) if n.task_id else None,
            "kind": n.kind, "title": n.title, "body": n.body, "severity": n.severity, "status": n.status, "owner": n.owner,
            "created_at": n.created_at.isoformat(), "updated_at": n.updated_at.isoformat(),
            "resolved_at": n.resolved_at.isoformat() if n.resolved_at else None}


def project_base(p: PmsProject) -> dict[str, Any]:
    return {"id": str(p.id), "name": p.name, "description": p.description, "scope": p.scope, "status": p.status,
            "start_date": p.start_date.isoformat() if p.start_date else None,
            "end_date": p.end_date.isoformat() if p.end_date else None,
            "members": list(p.members), "created_at": p.created_at.isoformat(), "updated_at": p.updated_at.isoformat()}


async def projects_out(db: AsyncSession, projects: list[PmsProject], today: date | None = None) -> list[dict[str, Any]]:
    """목록용 요약: 진행률·작업 수·지연·다음 마일스톤·열린 이슈. 프로젝트마다 쿼리하지 않고 한 번에 읽는다."""
    today = today or today_kst()
    if not projects:
        return []
    ids = [p.id for p in projects]
    tasks_by: dict[uuid.UUID, list[PmsTask]] = defaultdict(list)
    for t in await db.scalars(select(PmsTask).where(PmsTask.project_id.in_(ids))):
        tasks_by[t.project_id].append(t)
    miles_by: dict[uuid.UUID, list[PmsMilestone]] = defaultdict(list)
    for m in await db.scalars(select(PmsMilestone).where(PmsMilestone.project_id.in_(ids), PmsMilestone.done.is_(False))):
        miles_by[m.project_id].append(m)
    open_notes: dict[uuid.UUID, int] = defaultdict(int)
    for pid in await db.scalars(select(PmsNote.project_id).where(PmsNote.project_id.in_(ids), PmsNote.status == "open")):
        open_notes[pid] += 1
    out: list[dict[str, Any]] = []
    for p in projects:
        ordered, _ = build(tasks_by.get(p.id, []))
        c = counts_of([n for n in ordered if n.is_leaf], today)
        nxt = min(miles_by.get(p.id, []), key=lambda m: m.due_date, default=None)
        out.append({**project_base(p), "progress": overall(ordered), "counts": c,
                    "next_milestone": milestone_out(nxt, today) if nxt else None, "open_notes": open_notes.get(p.id, 0)})
    return out


async def dashboard(db: AsyncSession, p: PmsProject, today: date | None = None) -> dict[str, Any]:
    today = today or today_kst()
    ordered, _ = await tree(db, p.id)
    leaves = [n for n in ordered if n.is_leaf]
    open_leaves = [n for n in leaves if n.status != "done"]
    overdue = sorted((n for n in open_leaves if is_overdue(n, today)), key=lambda n: (n.end or today, n.wbs))
    soon_edge = today + timedelta(days=DUE_SOON_DAYS)
    due_soon = sorted((n for n in open_leaves if n.end and today <= n.end <= soon_edge), key=lambda n: (n.end or today, n.wbs))

    people: dict[str, dict[str, Any]] = {}
    for n in leaves:
        name = n.task.assignee or ""
        row = people.setdefault(name, {"name": name, "total": 0, "todo": 0, "doing": 0, "review": 0, "done": 0, "overdue": 0})
        row["total"] += 1
        row[n.status] += 1
        if is_overdue(n, today):
            row["overdue"] += 1
    by_assignee = sorted(people.values(), key=lambda r: (r["name"] == "", -r["total"], r["name"]))

    miles = list(await db.scalars(select(PmsMilestone).where(PmsMilestone.project_id == p.id).order_by(PmsMilestone.due_date)))
    notes = list(await db.scalars(select(PmsNote).where(PmsNote.project_id == p.id, PmsNote.status == "open")))
    notes.sort(key=lambda n: (SEVERITY_RANK.get(n.severity, 1), -n.created_at.timestamp()))
    phases = [{"id": str(n.task.id), "wbs": n.wbs, "title": n.task.title, "progress": n.progress, "status": n.status,
               "start_date": n.start.isoformat() if n.start else None, "end_date": n.end.isoformat() if n.end else None}
              for n in ordered if n.depth == 0]
    planned = planned_progress(p, today)
    return {
        "project": project_base(p), "today": today.isoformat(), "progress": overall(ordered), "planned_progress": planned,
        "days_left": (p.end_date - today).days if p.end_date else None, "counts": counts_of(leaves, today),
        "overdue": [_brief(n, today) for n in overdue[:10]], "due_soon": [_brief(n, today) for n in due_soon[:10]],
        "by_assignee": by_assignee, "phases": phases, "milestones": [milestone_out(m, today) for m in miles],
        "notes": {"open": len(notes), "issues": sum(n.kind == "issue" for n in notes), "risks": sum(n.kind == "risk" for n in notes),
                  "high": sum(n.severity == "high" for n in notes), "top": [note_out(n) for n in notes[:5]]},
    }
