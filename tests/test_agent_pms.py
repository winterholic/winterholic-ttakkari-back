"""PMS는 사용자를 대신해 에이전트가 관리한다. 기존 작업 트리 규칙을 그대로 적용한다."""
from __future__ import annotations

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
