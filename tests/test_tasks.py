"""ADR 0014: 하나뿐인 접수 대화, 지금 도는 일의 트리, 지난 작업 서고·검색."""
from __future__ import annotations

import asyncio
from datetime import timedelta

from ttakkari.db import sessionmaker
from ttakkari.models import ChatSession, Run, SessionKind, utcnow


async def _ws(client, auth) -> str:
    return (await client.get("/api/workspaces/default", headers=auth)).json()["id"]


async def _add(ws: str, kind: str, title: str, parent=None, *, pool="personal", age_h: float = 0,
               runs: list[tuple[str, str, str | None]] = ()) -> ChatSession:
    """세션 하나와 (status, prompt, result) Run 들을 DB 에 바로 넣는다. 에이전트를 돌리지 않는다."""
    at = utcnow() - timedelta(hours=age_h)
    async with sessionmaker()() as db:
        s = ChatSession(workspace_id=ws, title=title, engine="auto", kind=kind, pool=pool,
                        parent_id=parent.id if parent else None, created_at=at, updated_at=at)
        db.add(s)
        await db.flush()
        for i, (status, prompt, result) in enumerate(runs):
            db.add(Run(session_id=s.id, workspace_id=ws, prompt=prompt, engine="codex", status=status,
                       result_text=result, created_at=at + timedelta(seconds=i)))
        await db.commit()
        await db.refresh(s)
        return s


async def test_desk_is_one_per_pool_and_concurrent_opens_do_not_duplicate(client, auth):
    a, b = await asyncio.gather(*(client.post("/api/desk", json={"pool": "personal"}, headers=auth) for _ in range(2)))
    assert a.status_code == b.status_code == 200
    assert a.json()["id"] == b.json()["id"] and a.json()["kind"] == "desk" and a.json()["title"] == "따까리"
    work = (await client.post("/api/desk", json={"pool": "work"}, headers=auth)).json()
    assert work["id"] != a.json()["id"] and work["pool"] == "work"
    # 첫 메시지가 제목을 바꾸지 않는다("새 작업"일 때만 바뀐다)
    assert (await client.post("/api/desk", json={}, headers=auth)).json()["title"] == "따까리"


async def test_active_forest_keeps_running_and_recent_roots_only(client, auth):
    ws = await _ws(client, auth)
    desk = await _add(ws, SessionKind.desk, "따까리")
    live = await _add(ws, SessionKind.orchestrator, "보고서 정리", desk, age_h=30, runs=[("succeeded", "보고서", "나눴다")])
    w1 = await _add(ws, SessionKind.worker, "표 만들기", live, age_h=30, runs=[("running", "표", None)])
    w2 = await _add(ws, SessionKind.worker, "그림", w1, age_h=30, runs=[("failed", "그림", None)])
    recent = await _add(ws, SessionKind.orchestrator, "메일 답장", desk, age_h=1, runs=[("succeeded", "메일", "보냈다")])
    old = await _add(ws, SessionKind.orchestrator, "옛날 일", desk, age_h=48, runs=[("succeeded", "옛날", "끝")])

    r = await client.get("/api/tasks/active", headers=auth)
    assert r.status_code == 200, r.text
    body = r.json()
    # 깊은 곳에서 도는 작업이 뿌리를 끌어올리고, 실행 중인 뿌리가 먼저다. 오래된 일과 접수 자신은 없다.
    assert body["roots"] == [str(live.id), str(recent.id)]
    ids = {n["id"] for n in body["nodes"]}
    assert ids == {str(live.id), str(w1.id), str(w2.id), str(recent.id)}
    assert str(old.id) not in ids and str(desk.id) not in ids
    node = next(n for n in body["nodes"] if n["id"] == str(w1.id))
    assert node["status"] == "running" and node["parent_id"] == str(live.id)
    assert next(n for n in body["nodes"] if n["id"] == str(recent.id))["last_result"] == "보냈다"

    assert (await client.get("/api/tasks/active", params={"recent_hours": 0}, headers=auth)).json()["roots"] == [str(live.id)]


async def test_library_lists_roots_with_subtree_counts_and_pages(client, auth):
    ws = await _ws(client, auth)
    desk = await _add(ws, SessionKind.desk, "따까리", age_h=0.5)
    lead = await _add(ws, SessionKind.orchestrator, "자료 조사", desk, age_h=2, runs=[("succeeded", "조사", "됨")])
    await _add(ws, SessionKind.worker, "검색", lead, age_h=2, runs=[("failed", "검색", None)])
    await _add(ws, SessionKind.worker, "요약", lead, age_h=2, runs=[("running", "요약", None)])
    legacy = await _add(ws, SessionKind.chat, "옛 대화", age_h=5, runs=[("succeeded", "안녕", "네")])
    await _add(ws, SessionKind.orchestrator, "회사 일", desk, pool="work", age_h=3)

    body = (await client.get("/api/tasks/library", params={"pool": "personal"}, headers=auth)).json()
    assert [r["id"] for r in body["roots"]] == [str(desk.id), str(lead.id), str(legacy.id)]
    lead_row = body["roots"][1]
    assert (lead_row["descendants"], lead_row["active"], lead_row["failed"]) == (2, 1, 1)
    assert body["next_before"] is None

    page1 = (await client.get("/api/tasks/library", params={"limit": 2}, headers=auth)).json()
    assert len(page1["roots"]) == 2 and page1["next_before"]
    page2 = (await client.get("/api/tasks/library", params={"limit": 2, "before": page1["next_before"]}, headers=auth)).json()
    assert [r["title"] for r in page2["roots"]] == ["회사 일", "옛 대화"]


async def test_search_finds_title_prompt_and_result_with_path(client, auth):
    ws = await _ws(client, auth)
    desk = await _add(ws, SessionKind.desk, "따까리")
    lead = await _add(ws, SessionKind.orchestrator, "분기 보고서", desk)
    worker = await _add(ws, SessionKind.worker, "매출 표", lead,
                        runs=[("succeeded", "엑셀에서 매출을 뽑아", "3분기 매출은 12억 원으로 집계됐다")])

    hits = (await client.get("/api/tasks/search", params={"q": "12억"}, headers=auth)).json()
    assert len(hits) == 1
    h = hits[0]
    assert h["node"]["id"] == str(worker.id) and h["field"] == "result" and "12억" in h["snippet"]
    assert h["root_id"] == str(lead.id) and [p["title"] for p in h["path"]] == ["분기 보고서", "매출 표"]

    by_title = (await client.get("/api/tasks/search", params={"q": "보고서"}, headers=auth)).json()
    assert by_title[0]["node"]["id"] == str(lead.id) and by_title[0]["field"] == "title"
    # LIKE 특수문자는 글자 그대로 찾는다
    assert (await client.get("/api/tasks/search", params={"q": "%"}, headers=auth)).json() == []


async def test_tree_returns_whole_root_and_breadcrumb(client, auth):
    ws = await _ws(client, auth)
    desk = await _add(ws, SessionKind.desk, "따까리")
    lead = await _add(ws, SessionKind.orchestrator, "기획", desk)
    a = await _add(ws, SessionKind.worker, "가", lead)
    b = await _add(ws, SessionKind.worker, "나", a)
    await _add(ws, SessionKind.worker, "다", lead)

    t = (await client.get(f"/api/tasks/{b.id}/tree", headers=auth)).json()
    assert t["root_id"] == str(lead.id)
    assert [p["title"] for p in t["path"]] == ["기획", "가", "나"]
    assert sorted(n["title"] for n in t["nodes"]) == ["가", "기획", "나", "다"]
    # 접수의 트리는 접수 하나뿐이다(자식은 각자 뿌리)
    d = (await client.get(f"/api/tasks/{desk.id}/tree", headers=auth)).json()
    assert [n["id"] for n in d["nodes"]] == [str(desk.id)]


async def test_runs_page_backwards(client, auth):
    ws = await _ws(client, auth)
    s = await _add(ws, SessionKind.desk, "따까리", runs=[("succeeded", f"m{i}", None) for i in range(5)])
    last2 = (await client.get(f"/api/sessions/{s.id}/runs", params={"limit": 2}, headers=auth)).json()
    assert [r["prompt"] for r in last2] == ["m3", "m4"]
    older = (await client.get(f"/api/sessions/{s.id}/runs", params={"limit": 2, "before": last2[0]["created_at"]},
                              headers=auth)).json()
    assert [r["prompt"] for r in older] == ["m1", "m2"]
    assert len((await client.get(f"/api/sessions/{s.id}/runs", headers=auth)).json()) == 5
