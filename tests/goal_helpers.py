"""목표 계약 시험 공용 도구. 실제 경로(queue_run·manager._finish·work API)를 부르기 위한 얇은 헬퍼만 둔다."""
from __future__ import annotations

from uuid import UUID

from sqlalchemy import select

from ttakkari.api.sessions import queue_run
from ttakkari.db import sessionmaker
from ttakkari.models import (
    AppSetting,
    ChatSession,
    Run,
    RunOrigin,
    RunStatus,
    SessionKind,
)
from ttakkari.orchestrator import work
from ttakkari.orchestrator.runtime import issue_token
from ttakkari.runs.manager import manager


async def lead(client, auth, make_dir, kind="orchestrator"):
    root = make_dir()
    ws = (await client.post("/api/workspaces", headers=auth,
                            json={"name": root.name, "root_path": str(root)})).json()
    sess = (await client.post("/api/sessions", headers=auth,
                              json={"workspace_id": ws["id"], "kind": kind})).json()
    return sess, {"Authorization": f"Bearer {issue_token(UUID(sess['id']))}"}


def contract(goal="목표", reqs=None, status="active", **extra):
    reqs = reqs or [{"id": "R1", "text": "첫 요구", "status": "pending"}, {"id": "R2", "text": "둘째 요구", "status": "pending"}]
    value = {"goal": goal, "strategy": "s", "requirements": reqs, "next_action": "진행", "status": status}
    value.update(extra)
    return value


async def put(client, h, state, revision=None, **extra):
    """checkpoint. revision 을 안 주면 현재 값을 읽어 쓴다."""
    if revision is None:
        revision = (await get(client, h))["revision"]
    return await client.post("/api/orchestrator/work", headers=h,
                             json={"operation": "checkpoint", "revision": revision, "state": state, **extra})


async def get(client, h) -> dict:
    return (await client.post("/api/orchestrator/work", headers=h, json={"operation": "get"})).json()


async def raw(sid: UUID) -> dict:
    async with sessionmaker()() as db:
        row = await db.get(AppSetting, work.key(sid))
        return dict(row.value) if row else {}


async def request(sid: UUID, prompt: str, origin=RunOrigin.user) -> Run:
    """사용자 요청을 실제 접수 경로(queue_run)로 넣는다."""
    async with sessionmaker()() as db:
        s = await db.get(ChatSession, sid)
        run = await queue_run(db, s, prompt, origin=origin)
        await db.refresh(run)
        return run


async def new_run(sid: UUID, *, status=RunStatus.running, origin=RunOrigin.orchestrator, prompt="turn",
                  error=None, result_text=None, session_id: UUID | None = None, events=None) -> UUID:
    """이미 시작된 Run 을 하나 만든다. events 는 [(type, payload)]."""
    from ttakkari.runs import events as ev
    async with sessionmaker()() as db:
        s = await db.get(ChatSession, session_id or sid)
        run = Run(session_id=s.id, workspace_id=s.workspace_id, engine="claude", prompt=prompt, origin=origin,
                  status=status, error=error, result_text=result_text)
        db.add(run)
        await db.flush()
        for kind, payload in events or []:
            ev.append(db, run, kind, payload)
        await db.commit()
        return run.id


async def finish(run_id: UUID, status=RunStatus.succeeded, error: str | None = None, result_text: str | None = None) -> None:
    """운영과 같은 종료 경로(RunManager._finish → after_run·보고·완료 확인)."""
    async with sessionmaker()() as db:
        run = await db.get(Run, run_id)
        if result_text is not None:
            run.result_text = result_text
        await manager._finish(db, run, status, error=error)


async def queued(sid: UUID, origin: RunOrigin | None = None) -> list[Run]:
    async with sessionmaker()() as db:
        q = select(Run).where(Run.session_id == sid, Run.status == RunStatus.queued).order_by(Run.created_at, Run.id)
        if origin is not None:
            q = q.where(Run.origin == origin)
        return list(await db.scalars(q))


def continuations(runs: list[Run]) -> list[Run]:
    return [r for r in runs if isinstance((r.usage or {}).get("work_continuation"), dict)]


async def make_worker(sid: UUID, title="worker") -> UUID:
    async with sessionmaker()() as db:
        s = await db.get(ChatSession, sid)
        w = ChatSession(workspace_id=s.workspace_id, parent_id=sid, title=title, engine="claude", kind=SessionKind.worker)
        db.add(w)
        await db.commit()
        return w.id
