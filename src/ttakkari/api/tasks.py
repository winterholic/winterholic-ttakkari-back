"""작업 트리(ADR 0014): 모드마다 하나뿐인 접수 대화, 지금 도는 일의 트리, 지난 작업 서고와 검색.

트리의 뿌리는 접수가 넘긴 기획 세션이다. 접수 자신은 뿌리가 아니라 그 위의 대화창이다.
부모가 없는 옛 세션(일반 대화·옛 메인 오케스트레이터·이전 접수)도 서고에서는 뿌리로 본다.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from typing import Literal

from fastapi import APIRouter, Query
from sqlalchemy import Select, func, or_, select, text
from sqlalchemy.dialects.postgresql import distinct_on
from sqlalchemy.orm import aliased

from ttakkari.api.deps import Db, get_session
from ttakkari.api.sessions import _session_out
from ttakkari.api.workspaces import ensure_default_workspace
from ttakkari.audit import record
from ttakkari.models import ChatSession, Run, RunStatus, SessionKind, utcnow
from ttakkari.schemas import (
    DeskIn,
    SessionOut,
    TaskForest,
    TaskHit,
    TaskLibrary,
    TaskNode,
    TaskRoot,
    TaskTree,
)
from ttakkari.security.auth import User

router = APIRouter(prefix="/api", tags=["tasks"])
ACTIVE = [RunStatus.queued, RunStatus.running]
Pool = Literal["personal", "work"]
DESK_TITLE = {"personal": "따까리", "work": "따까리 · 회사"}
# pg advisory lock 키. 두 기기가 처음 열 때 접수를 둘 만들지 않게 한다.
DESK_LOCK = 7_014_000
RESULT_CHARS = 200
SNIPPET_BEFORE, SNIPPET_AFTER = 40, 140
SEARCH_SCAN = 400


@router.post("/desk", response_model=SessionOut)
async def ensure_desk(body: DeskIn, _: User, db: Db) -> SessionOut:
    """모드(개인·회사)마다 접수 대화는 하나다. 가장 최근 것을 돌려주고, 없으면 기본 워크스페이스에 만든다."""
    # 기본 워크스페이스 확보가 안에서 commit 하므로 잠그기 전에 끝낸다(commit 하면 xact 잠금이 풀린다).
    ws = await ensure_default_workspace(db)
    await db.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": DESK_LOCK + (body.pool == "work")})
    desk = await db.scalar(
        select(ChatSession)
        .where(ChatSession.kind == SessionKind.desk, ChatSession.pool == body.pool, ChatSession.archived_at.is_(None))
        .order_by(ChatSession.updated_at.desc()).limit(1))
    if desk is None:
        desk = ChatSession(workspace_id=ws.id, title=DESK_TITLE[body.pool], engine="auto", kind=SessionKind.desk,
                           pool=body.pool)
        db.add(desk)
        await db.flush()
        record(db, action="session.create", target_type="session", target_id=desk.id, detail={"kind": "desk", "pool": body.pool})
    await db.commit()
    return await _session_out(db, desk)


@router.get("/tasks/active", response_model=TaskForest)
async def active_tasks(_: User, db: Db, recent_hours: float = Query(6, ge=0, le=168)) -> TaskForest:
    """지금 도는 일과 최근에 끝난 일의 트리. 실행 중인 뿌리가 먼저, 그다음 최근에 움직인 순서다."""
    since = utcnow() - timedelta(hours=recent_hours)
    running = select(Run.session_id).where(Run.status.in_(ACTIVE))
    hot = list(await db.scalars(
        select(ChatSession.id).where(
            ChatSession.kind != SessionKind.desk, ChatSession.archived_at.is_(None),
            or_(ChatSession.updated_at >= since, ChatSession.id.in_(running)))))
    if not hot:
        return TaskForest(roots=[], nodes=[])
    up = await _with_ancestors(db, hot)
    roots = {_root_of(up, i) for i in hot}
    roots = {r for r in roots if up[r].kind != SessionKind.desk and up[r].archived_at is None}
    nodes = await _nodes(db, await _subtrees(db, list(roots)))
    by_root = _group_by_root(nodes, roots)
    order = sorted(roots, key=lambda r: (not any(n.status in ACTIVE for n in by_root[r]),
                                         -max(n.updated_at for n in by_root[r]).timestamp()))
    return TaskForest(roots=order, nodes=nodes)


@router.get("/tasks/library", response_model=TaskLibrary)
async def task_library(_: User, db: Db, pool: Pool | None = None, before: datetime | None = None,
                       limit: int = Query(30, ge=1, le=100)) -> TaskLibrary:
    """지난 작업 서고. 뿌리만 최근 순으로, 하위 세션 수·실행 중·실패 수를 붙인다. 펼치기는 /tasks/{id}/tree."""
    parent = aliased(ChatSession)
    q = (select(ChatSession).outerjoin(parent, ChatSession.parent_id == parent.id)
         .where(or_(ChatSession.parent_id.is_(None), parent.kind == SessionKind.desk)))
    if pool:
        q = q.where(ChatSession.pool == pool)
    if before:
        q = q.where(ChatSession.updated_at < before)
    page = list(await db.scalars(q.order_by(ChatSession.updated_at.desc()).limit(limit + 1)))
    more = len(page) > limit
    page = page[:limit]
    if not page:
        return TaskLibrary(roots=[])
    ids = [s.id for s in page if s.kind != SessionKind.desk]
    stats: dict[uuid.UUID, TaskRoot] = {}
    if ids:
        nodes = await _nodes(db, await _subtrees(db, ids))
        for root, members in _group_by_root(nodes, set(ids)).items():
            stats[root] = TaskRoot(
                **next(n for n in members if n.id == root).model_dump(),
                descendants=len(members) - 1,
                active=sum(n.status in ACTIVE for n in members),
                failed=sum(n.status == RunStatus.failed for n in members))
    desks = await _nodes(db, [s for s in page if s.kind == SessionKind.desk])
    for n in desks:
        stats[n.id] = TaskRoot(**n.model_dump())
    return TaskLibrary(roots=[stats[s.id] for s in page], next_before=page[-1].updated_at if more else None)


@router.get("/tasks/search", response_model=list[TaskHit])
async def search_tasks(_: User, db: Db, q: str = Query(min_length=1, max_length=200), pool: Pool | None = None,
                       limit: int = Query(40, ge=1, le=100)) -> list[TaskHit]:
    """세션 제목, 지시, 결과에서 찾는다. 세션마다 한 번만, 제목 > 지시 > 결과 순으로 보여 줄 근거를 고른다."""
    needle = q.strip()
    pattern = "%" + needle.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
    scope = select(ChatSession.id)
    if pool:
        scope = scope.where(ChatSession.pool == pool)
    found: dict[uuid.UUID, tuple[str, str]] = {}
    for sid, title in await db.execute(
            select(ChatSession.id, ChatSession.title)
            .where(ChatSession.id.in_(scope), ChatSession.title.ilike(pattern, escape="\\"))
            .order_by(ChatSession.updated_at.desc()).limit(SEARCH_SCAN)):
        found[sid] = ("title", title)
    for field, col in (("prompt", Run.prompt), ("result", Run.result_text)):
        rows = await db.execute(
            select(Run.session_id, col).where(Run.session_id.in_(scope), col.ilike(pattern, escape="\\"))
            .order_by(Run.created_at.desc()).limit(SEARCH_SCAN))
        for sid, body in rows:
            found.setdefault(sid, (field, body))
    if not found:
        return []
    sessions = list(await db.scalars(
        select(ChatSession).where(ChatSession.id.in_(found)).order_by(ChatSession.updated_at.desc()).limit(limit)))
    up = await _with_ancestors(db, [s.id for s in sessions])
    nodes = {n.id: n for n in await _nodes(db, list(up.values()))}
    hits = []
    for s in sessions:
        chain = _chain(up, s.id)
        field, body = found[s.id]
        hits.append(TaskHit(node=nodes[s.id], root_id=chain[0], path=[nodes[i] for i in chain], field=field,  # type: ignore[arg-type]
                            snippet=_snippet(body, needle)))
    return hits


@router.get("/tasks/{session_id}/tree", response_model=TaskTree)
async def task_tree(session_id: uuid.UUID, _: User, db: Db) -> TaskTree:
    """세션이 속한 뿌리의 트리 전체와, 뿌리부터 이 세션까지의 경로(빵 부스러기)."""
    await get_session(db, session_id)
    up = await _with_ancestors(db, [session_id])
    chain = _chain(up, session_id)
    root = chain[0]
    nodes = await _nodes(db, await _subtrees(db, [root]))
    by_id = {n.id: n for n in nodes}
    return TaskTree(root_id=root, path=[by_id[i] for i in chain if i in by_id], nodes=nodes)


# ---- 내부

def _root_of(up: dict[uuid.UUID, ChatSession], sid: uuid.UUID) -> uuid.UUID:
    """접수 바로 아래(기획 세션)나 부모 없는 세션이 뿌리다."""
    cur = up[sid]
    while cur.parent_id is not None and cur.parent_id in up and up[cur.parent_id].kind != SessionKind.desk:
        cur = up[cur.parent_id]
    return cur.id


def _chain(up: dict[uuid.UUID, ChatSession], sid: uuid.UUID) -> list[uuid.UUID]:
    root = _root_of(up, sid)
    chain = [sid]
    while chain[-1] != root:
        chain.append(up[chain[-1]].parent_id)  # type: ignore[arg-type]
    return chain[::-1]


async def _with_ancestors(db: Db, ids: list[uuid.UUID]) -> dict[uuid.UUID, ChatSession]:
    """ids 와 그 조상 전부."""
    up = select(ChatSession.id, ChatSession.parent_id).where(ChatSession.id.in_(ids)).cte("up", recursive=True)
    up = up.union(select(ChatSession.id, ChatSession.parent_id).join(up, ChatSession.id == up.c.parent_id))
    rows = await db.scalars(select(ChatSession).where(ChatSession.id.in_(select(up.c.id))))
    return {s.id: s for s in rows}


async def _subtrees(db: Db, roots: list[uuid.UUID]) -> list[ChatSession]:
    """뿌리들과 그 아래 전부. 접수 아래로는 내려가지 않는다(접수의 자식은 각자 뿌리다)."""
    if not roots:
        return []
    down = (select(ChatSession.id, ChatSession.kind).where(ChatSession.id.in_(roots))
            .cte("down", recursive=True))
    down = down.union_all(
        select(ChatSession.id, ChatSession.kind).join(down, ChatSession.parent_id == down.c.id)
        .where(down.c.kind != SessionKind.desk))
    return list(await db.scalars(select(ChatSession).where(ChatSession.id.in_(select(down.c.id)))
                                 .order_by(ChatSession.created_at)))


def _latest(q: Select, ids: list[uuid.UUID]) -> Select:
    return q.where(Run.session_id.in_(ids)).ext(distinct_on(Run.session_id)).order_by(Run.session_id, Run.created_at.desc())


async def _nodes(db: Db, sessions: list[ChatSession]) -> list[TaskNode]:
    """세션마다 마지막 Run 의 상태·프로필과 마지막으로 끝난 Run 의 결과 앞부분을 붙인다(쿼리 두 번)."""
    ids = [s.id for s in sessions]
    if not ids:
        return []
    last = {r.session_id: r for r in await db.execute(_latest(select(Run.session_id, Run.status, Run.profile, Run.model), ids))}
    done = {r.session_id: r for r in await db.execute(_latest(
        select(Run.session_id, func.left(func.coalesce(Run.result_text, Run.error, ""), RESULT_CHARS * 2).label("text"))
        .where(Run.status.not_in(ACTIVE)), ids))}
    out = []
    for s in sessions:
        lr, dr = last.get(s.id), done.get(s.id)
        result = " ".join(dr.text.split())[:RESULT_CHARS] if dr is not None and dr.text else None
        out.append(TaskNode(
            id=s.id, parent_id=s.parent_id, kind=s.kind, title=s.title, pool=s.pool, created_at=s.created_at,
            updated_at=s.updated_at, archived_at=s.archived_at, status=lr.status if lr else None,
            profile=lr.profile if lr else None, model=(lr.model if lr else None) or s.model, last_result=result))
    return out


def _group_by_root(nodes: list[TaskNode], roots: set[uuid.UUID]) -> dict[uuid.UUID, list[TaskNode]]:
    parent = {n.id: n.parent_id for n in nodes}
    groups: dict[uuid.UUID, list[TaskNode]] = {r: [] for r in roots}
    for n in nodes:
        cur = n.id
        while cur not in roots:
            cur = parent[cur]  # type: ignore[assignment]
        groups[cur].append(n)
    return groups


def _snippet(body: str, needle: str) -> str:
    flat = " ".join(body.split())
    i = flat.lower().find(needle.lower())
    if i < 0:
        return flat[:SNIPPET_BEFORE + SNIPPET_AFTER]
    start = max(0, i - SNIPPET_BEFORE)
    end = min(len(flat), i + len(needle) + SNIPPET_AFTER)
    return ("…" if start else "") + flat[start:end] + ("…" if end < len(flat) else "")

