"""PMS는 사용자를 대신해 에이전트가 관리한다. 기존 작업 트리 규칙을 그대로 적용한다."""
from __future__ import annotations

import json
import uuid

from ttakkari.orchestrator.runtime import issue_token


async def agent(client, auth, make_dir, *, pool="personal"):
    ws = (await client.post("/api/workspaces", json={"name": "pms", "root_path": str(make_dir())}, headers=auth)).json()
    session = (await client.post("/api/sessions", json={"workspace_id": ws["id"], "pool": pool}, headers=auth)).json()
    return {"Authorization": f"Bearer {issue_token(uuid.UUID(session['id']))}"}


async def call(client, headers, operation, *, project_id=None, item_id=None, data=None):
    r = await client.post("/api/agent/pms", headers=headers,
                          json={"operation": operation, "project_id": project_id, "item_id": item_id, "data": data or {}})
    assert r.status_code == 200, r.text
    return r.json()


async def test_agent_manages_projects_tree_dependencies_milestones_and_notes(client, auth, make_dir):
    h = await agent(client, auth, make_dir)
    p = await call(client, h, "create_project", data={"name": "에이전트 확장 기획", "phases": ["조사"]})
    pid = p["id"]
    assert (await call(client, h, "get_project", project_id=pid))["name"] == p["name"]
    assert any(x["id"] == pid for x in (await call(client, h, "list_projects"))["projects"])
    assert (await call(client, h, "update_project", project_id=pid, data={"status": "active"}))["status"] == "active"
    root = (await call(client, h, "list_tasks", project_id=pid))["tasks"][0]
    a = (await call(client, h, "create_task", project_id=pid, data={"title": "API 조사", "parent_id": root["id"]}))["task"]
    b = (await call(client, h, "create_task", project_id=pid,
                    data={"title": "PoC", "parent_id": root["id"], "predecessors": [a["id"]]}))["task"]
    assert b["depth"] == 1 and b["predecessors"] == [a["id"]]
    assert (await call(client, h, "update_task", item_id=a["id"], data={"status": "done"}))["task"]["progress"] == 100
    moved = await call(client, h, "move_task", item_id=b["id"], data={"parent_id": None, "index": 0})
    assert next(t for t in moved["tasks"] if t["id"] == b["id"])["depth"] == 0
    m = await call(client, h, "create_milestone", project_id=pid, data={"title": "검토", "due_date": "2026-12-01"})
    assert (await call(client, h, "update_milestone", item_id=m["id"], data={"done": True}))["done"]
    assert len((await call(client, h, "list_milestones", project_id=pid))["milestones"]) == 1
    n = await call(client, h, "create_note", project_id=pid, data={"title": "불확실성", "kind": "risk", "task_id": b["id"]})
    assert (await call(client, h, "update_note", item_id=n["id"], data={"status": "resolved"}))["status"] == "resolved"
    assert len((await call(client, h, "list_notes", project_id=pid))["notes"]) == 1
    assert isinstance(await call(client, h, "dashboard", project_id=pid), dict)
    for op, ident in [("delete_note", n["id"]), ("delete_milestone", m["id"]), ("delete_task", a["id"])]:
        await call(client, h, op, item_id=ident)
    await call(client, h, "delete_project", project_id=pid)
    assert not (await call(client, h, "list_projects"))["projects"]


async def test_agent_pms_keeps_existing_validation_and_rejects_unsupported_operations(client, auth, make_dir):
    h = await agent(client, auth, make_dir)
    p = await call(client, h, "create_project", data={"name": "x"})
    a = (await call(client, h, "create_task", project_id=p["id"], data={"title": "a"}))["task"]
    b = (await call(client, h, "create_task", project_id=p["id"], data={"title": "b", "predecessors": [a["id"]]}))["task"]
    r = await client.post("/api/agent/pms", headers=h, json={"operation": "update_task", "item_id": a["id"], "data": {"predecessors": [b["id"]]}})
    assert r.status_code == 422
    for body in [{"operation": "create_task", "project_id": p["id"], "data": {"title": "bad", "parent_id": str(uuid.uuid4())}},
                 {"operation": "create_project", "data": {"name": "bad", "unknown_option": True}},
                 {"operation": "create_task", "data": {"title": "missing project"}},
                 {"operation": "run_shell", "data": {} }]:
        assert (await client.post("/api/agent/pms", headers=h, json=body)).status_code in (404, 422)
    assert len((await call(client, h, "list_tasks", project_id=p["id"]))["tasks"]) == 2


async def test_agent_pms_work_scope_and_token_boundaries(client, auth, make_dir):
    personal = (await client.post("/api/pms/projects", headers=auth, json={"name": "개인", "scope": "personal"})).json()
    h = await agent(client, auth, make_dir, pool="work")
    work = await call(client, h, "create_project", data={"name": "회사", "scope": "work"})
    assert [p["id"] for p in (await call(client, h, "list_projects"))["projects"]] == [work["id"]]
    for op in ("get_project", "update_project", "delete_project", "list_tasks", "create_task"):
        r = await client.post("/api/agent/pms", headers=h, json={"operation": op, "project_id": personal["id"], "data": {"title": "x"}})
        assert r.status_code == 404, (op, r.text)
    assert (await client.post("/api/agent/pms", headers=h, json={"operation": "update_project", "project_id": work["id"], "data": {"scope": "personal"}})).status_code == 403
    for headers in ({}, auth):
        assert (await client.post("/api/agent/pms", headers=headers, json={"operation": "list_projects"})).status_code == 401
    for kind, create in (("task", {"title": "개인 작업"}),
                         ("milestone", {"title": "개인 마일스톤", "due_date": "2026-12-01"}),
                         ("note", {"title": "개인 이슈"})):
        segment = {"task": "tasks", "milestone": "milestones", "note": "notes"}[kind]
        saved = (await client.post(f"/api/pms/projects/{personal['id']}/{segment}", headers=auth, json=create)).json()
        ident = saved["task"]["id"] if kind == "task" else saved["id"]
        for action in ("update", "delete"):
            r = await client.post("/api/agent/pms", headers=h,
                                  json={"operation": f"{action}_{kind}", "item_id": ident, "data": {}})
            assert r.status_code == 404


def test_codex_pms_management_is_preapproved_without_mislabeling_writes(make_dir):
    from ttakkari.agents.base import LaunchSpec
    from ttakkari.agents.codex import CodexAdapter
    from ttakkari.orchestrator.tool_policy import READ_ONLY_TOOLS

    root = make_dir()
    spec = LaunchSpec(cwd=root, prompt="PMS 관리", engine_session_id=None, model=None, effort=None,
                      outbox=root, write_roots=[root], system_note="",
                      mcp_servers={"ttakkari": {"command": "python", "args": [], "env": {}}})
    argv = CodexAdapter().build_command(spec, {})
    for name in ("manage_pms", "create_pms_task", "list_pms_projects"):
        assert f'mcp_servers.ttakkari.tools.{name}.approval_mode="approve"' in argv
    assert "manage_pms" not in READ_ONLY_TOOLS and "create_pms_task" not in READ_ONLY_TOOLS


async def _tree(client, h, pid):
    """1 / 1.1(1.1.1, 1.1.2) / 1.2 , 2 / 2.1 , 3 — 하위가 있는 작업과 말단이 섞인 8개 작업."""
    async def add(title, parent=None, **data):
        d = {"title": title, "parent_id": parent, **data}
        return (await call(client, h, "create_task", project_id=pid, data=d))["task"]["id"]

    a = await add("설계")
    a1 = await add("API 설계", a, description="긴 본문 " * 50)
    a11 = await add("스키마", a1, status="done")
    a12 = await add("엔드포인트", a1, status="doing", progress=40)
    a2 = await add("DB 설계", a)
    b = await add("구현")
    b1 = await add("API 구현", b)
    c = await add("배포")
    return {"a": a, "a1": a1, "a11": a11, "a12": a12, "a2": a2, "b": b, "b1": b1, "c": c}


async def _list(client, h, pid, **data):
    return await call(client, h, "list_tasks", project_id=pid, data=data)


async def test_list_tasks_defaults_to_compact_first_page_with_paging_info(client, auth, make_dir):
    h = await agent(client, auth, make_dir)
    pid = (await call(client, h, "create_project", data={"name": "큰 프로젝트"}))["id"]
    ids = await _tree(client, h, pid)
    r = await _list(client, h, pid)
    assert r["compact"] is True and r["total"] == r["project_total"] == r["returned"] == 8
    assert r["has_more"] is False and r["next_offset"] is None and r["offset"] == 0 and r["limit"] == 50
    first = r["tasks"][0]
    assert set(first) == {"id", "wbs", "title", "status", "progress", "parent_id", "child_count"}
    assert first == {"id": ids["a"], "wbs": "1", "title": "설계", "status": "doing", "progress": first["progress"],
                     "parent_id": None, "child_count": 2}
    assert [t["wbs"] for t in r["tasks"]] == ["1", "1.1", "1.1.1", "1.1.2", "1.2", "2", "2.1", "3"]
    by = {t["id"]: t for t in r["tasks"]}
    assert by[ids["a1"]]["child_count"] == 2 and by[ids["a1"]]["parent_id"] == ids["a"]
    assert by[ids["c"]]["child_count"] == 0
    assert "description" not in first


async def test_list_tasks_full_mode_is_explicit_and_bounded(client, auth, make_dir):
    h = await agent(client, auth, make_dir)
    pid = (await call(client, h, "create_project", data={"name": "전체"}))["id"]
    ids = await _tree(client, h, pid)
    r = await _list(client, h, pid, compact=False, parent_id=ids["a"])
    assert r["compact"] is False and r["limit"] == 25
    node = next(t for t in r["tasks"] if t["id"] == ids["a1"])
    assert node["description"].startswith("긴 본문") and {"priority", "assignee", "predecessors", "start_date"} <= set(node)
    full_all = (await client.get(f"/api/pms/projects/{pid}/tasks", headers=auth)).json()["tasks"]
    again = (await _list(client, h, pid, compact=False))["tasks"]
    assert [t["id"] for t in again] == [t["id"] for t in full_all]
    r = await client.post("/api/agent/pms", headers=h, json={"operation": "list_tasks", "project_id": pid,
                                                              "data": {"compact": False, "limit": 26}})
    assert r.status_code == 422 and r.json()["code"] == "limit_too_large"
    assert (await _list(client, h, pid, compact=False, limit=25))["limit"] == 25


async def test_list_tasks_filters_and_combinations_keep_wbs_and_rollup(client, auth, make_dir):
    h = await agent(client, auth, make_dir)
    pid = (await call(client, h, "create_project", data={"name": "필터"}))["id"]
    ids = await _tree(client, h, pid)

    def wbs(r):
        return [t["wbs"] for t in r["tasks"]]

    assert wbs(await _list(client, h, pid, parent_id=ids["a"])) == ["1.1", "1.2"]  # 손자는 제외
    assert wbs(await _list(client, h, pid, parent_id=ids["a1"])) == ["1.1.1", "1.1.2"]
    assert (await _list(client, h, pid, parent_id=ids["c"]))["tasks"] == []
    assert wbs(await _list(client, h, pid, depth=0)) == ["1", "2", "3"]
    assert wbs(await _list(client, h, pid, depth=2)) == ["1.1.1", "1.1.2"]
    assert wbs(await _list(client, h, pid, max_depth=1)) == ["1", "1.1", "1.2", "2", "2.1", "3"]
    assert wbs(await _list(client, h, pid, depth=1, max_depth=0)) == []
    assert wbs(await _list(client, h, pid, status="done")) == ["1.1.1"]
    assert wbs(await _list(client, h, pid, status="doing")) == ["1", "1.1", "1.1.2"]  # 집계 상태 기준
    assert wbs(await _list(client, h, pid, q="api")) == ["1.1", "2.1"]  # 대소문자 무시
    assert wbs(await _list(client, h, pid, q=" 설계 ")) == ["1", "1.1", "1.2"]
    assert wbs(await _list(client, h, pid, q="없는제목")) == []
    # 조합은 AND, total 은 필터 후 개수, project_total 은 전체
    r = await _list(client, h, pid, parent_id=ids["a"], status="doing", q="api")
    assert wbs(r) == ["1.1"] and r["total"] == 1 and r["project_total"] == 8
    assert wbs(await _list(client, h, pid, max_depth=1, status="todo", q="구현")) == ["2", "2.1"]
    # 필터가 번호·집계를 바꾸지 않는다
    sub = (await _list(client, h, pid, parent_id=ids["a1"]))["tasks"][1]
    assert sub["wbs"] == "1.1.2" and sub["progress"] == 40
    full = {t["id"]: t for t in (await _list(client, h, pid))["tasks"]}
    assert full[ids["a1"]]["progress"] == 70  # 말단 100 과 40 의 평균


async def test_list_tasks_pagination_boundaries(client, auth, make_dir):
    h = await agent(client, auth, make_dir)
    pid = (await call(client, h, "create_project", data={"name": "페이지"}))["id"]
    await _tree(client, h, pid)
    seen, offset, pages = [], 0, 0
    while True:
        r = await _list(client, h, pid, limit=3, offset=offset)
        pages += 1
        seen += [t["wbs"] for t in r["tasks"]]
        assert r["returned"] == len(r["tasks"]) and r["total"] == 8
        if not r["has_more"]:
            assert r["next_offset"] is None
            break
        assert r["next_offset"] == offset + 3
        offset = r["next_offset"]
    assert pages == 3 and seen == ["1", "1.1", "1.1.1", "1.1.2", "1.2", "2", "2.1", "3"]
    exact = await _list(client, h, pid, limit=8)  # 정확히 맞는 경계: 다음 쪽 없음
    assert exact["returned"] == 8 and exact["has_more"] is False and exact["next_offset"] is None
    assert (await _list(client, h, pid, limit=7))["next_offset"] == 7
    past = await _list(client, h, pid, offset=8)
    assert past["tasks"] == [] and past["has_more"] is False and past["total"] == 8
    assert (await _list(client, h, pid, offset=999))["tasks"] == []
    filtered = await _list(client, h, pid, max_depth=1, limit=4)
    assert filtered["total"] == 6 and filtered["has_more"] and filtered["next_offset"] == 4
    assert [t["wbs"] for t in (await _list(client, h, pid, max_depth=1, limit=4, offset=4))["tasks"]] == ["2.1", "3"]
    for bad in ({"limit": 0}, {"limit": 201}, {"offset": -1}, {"depth": -1}, {"depth": 8}, {"status": "nope"},
                {"compact": "maybe"}, {"unknown": 1}, {"parent_id": "not-a-uuid"}, {"q": "x" * 201}):
        r = await client.post("/api/agent/pms", headers=h, json={"operation": "list_tasks", "project_id": pid, "data": bad})
        assert r.status_code == 422, bad


async def test_list_tasks_default_page_caps_large_projects_and_stays_small(client, auth, make_dir):
    h = await agent(client, auth, make_dir)
    pid = (await call(client, h, "create_project", data={"name": "대형"}))["id"]
    for i in range(60):
        await call(client, h, "create_task", project_id=pid, data={"title": f"작업 {i:02d} " + "가" * 20, "description": "본문 " * 500})
    r = await call(client, h, "list_tasks", project_id=pid)
    assert r["returned"] == 50 and r["total"] == 60 and r["has_more"] and r["next_offset"] == 50
    assert len(json.dumps(r, ensure_ascii=False)) < 15_000
    last = await _list(client, h, pid, offset=r["next_offset"])
    assert last["returned"] == 10 and not last["has_more"]


async def test_list_tasks_filters_respect_project_and_work_scope(client, auth, make_dir):
    personal = (await client.post("/api/pms/projects", headers=auth, json={"name": "개인", "scope": "personal"})).json()
    p_task = (await client.post(f"/api/pms/projects/{personal['id']}/tasks", headers=auth, json={"title": "개인 작업"})).json()["task"]
    h = await agent(client, auth, make_dir, pool="work")
    work = await call(client, h, "create_project", data={"name": "회사", "scope": "work"})
    w_task = (await call(client, h, "create_task", project_id=work["id"], data={"title": "회사 작업"}))["task"]
    # work 세션: 개인 프로젝트는 필터·페이지 옵션과 무관하게 404
    for data in ({}, {"q": "개인"}, {"parent_id": p_task["id"]}, {"compact": False}, {"limit": 1, "offset": 0}):
        r = await client.post("/api/agent/pms", headers=h, json={"operation": "list_tasks", "project_id": personal["id"], "data": data})
        assert r.status_code == 404, data
    # 개인 프로젝트의 작업 id 를 회사 프로젝트의 parent_id 로 써도 내용이 새지 않는다
    r = await client.post("/api/agent/pms", headers=h, json={"operation": "list_tasks", "project_id": work["id"],
                                                              "data": {"parent_id": p_task["id"]}})
    assert r.status_code == 404 and "개인 작업" not in r.text
    ok = await _list(client, h, work["id"], q="작업")
    assert [t["id"] for t in ok["tasks"]] == [w_task["id"]]
    # personal 세션은 work 프로젝트도 읽을 수 있다(기존 정책)
    hp = await agent(client, auth, make_dir)
    assert (await _list(client, hp, work["id"]))["total"] == 1
    assert (await _list(client, hp, personal["id"], q="개인"))["total"] == 1
