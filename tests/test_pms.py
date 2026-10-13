"""ADR 0012: 프로젝트 관리 PMS · WBS. 작업 트리의 번호·집계·이동과 대시보드를 API 로 확인한다."""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from ttakkari.db import sessionmaker
from ttakkari.models import PmsMilestone, PmsNote, PmsTask

P = "/api/pms"


async def _project(client, auth, **body) -> dict:
    body.setdefault("name", "웹 리뉴얼")
    r = await client.post(f"{P}/projects", json=body, headers=auth)
    assert r.status_code == 201, r.text
    return r.json()


async def _task(client, auth, pid, title, **kw) -> dict:
    r = await client.post(f"{P}/projects/{pid}/tasks", json={"title": title, **kw}, headers=auth)
    assert r.status_code == 201, r.text
    return r.json()["task"]


async def _tasks(client, auth, pid) -> list[dict]:
    return (await client.get(f"{P}/projects/{pid}/tasks", headers=auth)).json()["tasks"]


def _by(tasks, title) -> dict:
    return next(t for t in tasks if t["title"] == title)


async def test_requires_auth(client):
    assert (await client.get(f"{P}/projects")).status_code == 401
    assert (await client.post(f"{P}/projects", json={"name": "x"})).status_code == 401


async def test_project_crud_and_scope_filter(client, auth):
    a = await _project(client, auth, name="개인 프로젝트", status="active", start_date="2026-10-01", end_date="2026-12-31", members=["민수", " 민수 ", "지연"])
    b = await _project(client, auth, name="회사 프로젝트", scope="work")
    assert a["members"] == ["민수", "지연"] and a["progress"] == 0 and a["counts"]["total"] == 0

    assert [p["name"] for p in (await client.get(f"{P}/projects?scope=work", headers=auth)).json()["projects"]] == ["회사 프로젝트"]
    assert len((await client.get(f"{P}/projects", headers=auth)).json()["projects"]) == 2
    assert [p["id"] for p in (await client.get(f"{P}/projects?status=active", headers=auth)).json()["projects"]] == [a["id"]]
    assert [p["id"] for p in (await client.get(f"{P}/projects?q=회사", headers=auth)).json()["projects"]] == [b["id"]]

    r = await client.patch(f"{P}/projects/{a['id']}", json={"name": "새 이름", "status": "done", "end_date": None}, headers=auth)
    assert r.status_code == 200
    assert r.json()["name"] == "새 이름" and r.json()["status"] == "done" and r.json()["end_date"] is None

    bad = await client.patch(f"{P}/projects/{a['id']}", json={"end_date": "2026-09-01"}, headers=auth)
    assert bad.status_code == 422 and bad.json()["code"] == "bad_dates"

    assert (await client.delete(f"{P}/projects/{b['id']}", headers=auth)).status_code == 204
    assert (await client.get(f"{P}/projects/{b['id']}", headers=auth)).status_code == 404


async def test_create_with_phases_numbers_tasks(client, auth):
    p = await _project(client, auth, phases=["기획", "설계", "구현"])
    ts = await _tasks(client, auth, p["id"])
    assert [(t["wbs"], t["title"]) for t in ts] == [("1", "기획"), ("2", "설계"), ("3", "구현")]


async def test_wbs_numbers_follow_tree_order(client, auth):
    p = (await _project(client, auth))["id"]
    a = await _task(client, auth, p, "설계")
    b = await _task(client, auth, p, "구현")
    a1 = await _task(client, auth, p, "화면 설계", parent_id=a["id"])
    await _task(client, auth, p, "API 설계", parent_id=a["id"])
    await _task(client, auth, p, "세부 화면", parent_id=a1["id"])
    # 형제 끼워 넣기: 설계와 구현 사이
    await _task(client, auth, p, "검토", after_id=a["id"])
    ts = await _tasks(client, auth, p)
    assert [(t["wbs"], t["title"], t["depth"]) for t in ts] == [
        ("1", "설계", 0), ("1.1", "화면 설계", 1), ("1.1.1", "세부 화면", 2), ("1.2", "API 설계", 1), ("2", "검토", 0), ("3", "구현", 0)]
    assert _by(ts, "설계")["has_children"] and not _by(ts, "구현")["has_children"]
    assert b["id"] == _by(ts, "구현")["id"]


async def test_leaf_status_progress_sync(client, auth):
    p = (await _project(client, auth))["id"]
    t = await _task(client, auth, p, "작업")
    r = await client.patch(f"{P}/tasks/{t['id']}", json={"progress": 40}, headers=auth)
    assert r.json()["task"]["status"] == "doing"
    r = await client.patch(f"{P}/tasks/{t['id']}", json={"status": "done"}, headers=auth)
    assert (r.json()["task"]["status"], r.json()["task"]["progress"]) == ("done", 100)
    assert r.json()["task"]["done_at"]
    r = await client.patch(f"{P}/tasks/{t['id']}", json={"status": "doing"}, headers=auth)
    assert (r.json()["task"]["status"], r.json()["task"]["progress"], r.json()["task"]["done_at"]) == ("doing", 90, None)
    r = await client.patch(f"{P}/tasks/{t['id']}", json={"progress": 100}, headers=auth)
    assert r.json()["task"]["status"] == "done"
    r = await client.patch(f"{P}/tasks/{t['id']}", json={"status": "todo"}, headers=auth)
    assert (r.json()["task"]["status"], r.json()["task"]["progress"]) == ("todo", 0)


async def test_parent_rollup_progress_status_dates(client, auth):
    p = (await _project(client, auth))["id"]
    ph = await _task(client, auth, p, "구현")
    a = await _task(client, auth, p, "A", parent_id=ph["id"], start_date="2026-10-01", end_date="2026-10-03", status="done")
    b = await _task(client, auth, p, "B", parent_id=ph["id"], start_date="2026-10-04", end_date="2026-10-13")
    ts = await _tasks(client, auth, p)
    ph_row = _by(ts, "구현")
    # 가중치는 기간(일): A 3일 100%, B 10일 0% → 23%
    assert ph_row["progress"] == 23 and ph_row["status"] == "doing"
    assert (ph_row["start_date"], ph_row["end_date"], ph_row["dates_rollup"]) == ("2026-10-01", "2026-10-13", True)

    await client.patch(f"{P}/tasks/{b['id']}", json={"status": "done"}, headers=auth)
    row = _by(await _tasks(client, auth, p), "구현")
    assert (row["progress"], row["status"]) == (100, "done")

    # 상위 작업의 상태·진행률·(집계된) 기간은 직접 못 고친다
    for body in ({"status": "done"}, {"progress": 10}, {"end_date": "2026-11-01"}):
        r = await client.patch(f"{P}/tasks/{ph['id']}", json=body, headers=auth)
        assert r.status_code == 422 and r.json()["code"] == "rollup", body
    assert a["id"]


async def test_parent_without_dated_children_keeps_own_dates(client, auth):
    p = (await _project(client, auth))["id"]
    ph = await _task(client, auth, p, "단계", start_date="2026-10-01", end_date="2026-10-31")
    await _task(client, auth, p, "자식", parent_id=ph["id"])
    row = _by(await _tasks(client, auth, p), "단계")
    assert (row["start_date"], row["end_date"], row["dates_rollup"]) == ("2026-10-01", "2026-10-31", False)
    r = await client.patch(f"{P}/tasks/{ph['id']}", json={"end_date": "2026-11-15"}, headers=auth)
    assert r.status_code == 200 and r.json()["task"]["end_date"] == "2026-11-15"


async def test_move_indent_outdent_reorder(client, auth):
    p = (await _project(client, auth))["id"]
    a = await _task(client, auth, p, "A")
    b = await _task(client, auth, p, "B")
    c = await _task(client, auth, p, "C")
    # B 를 A 아래로 들여쓰기
    r = await client.post(f"{P}/tasks/{b['id']}/move", json={"parent_id": a["id"]}, headers=auth)
    assert r.status_code == 200
    assert [(t["wbs"], t["title"]) for t in r.json()["tasks"]] == [("1", "A"), ("1.1", "B"), ("2", "C")]
    # C 를 맨 앞으로
    r = await client.post(f"{P}/tasks/{c['id']}/move", json={"parent_id": None, "index": 0}, headers=auth)
    assert [(t["wbs"], t["title"]) for t in r.json()["tasks"]] == [("1", "C"), ("2", "A"), ("2.1", "B")]
    # B 내어쓰기: A 바로 뒤
    r = await client.post(f"{P}/tasks/{b['id']}/move", json={"parent_id": None, "index": 2}, headers=auth)
    assert [(t["wbs"], t["title"]) for t in r.json()["tasks"]] == [("1", "C"), ("2", "A"), ("3", "B")]
    # 자기 자손 아래로는 못 옮긴다
    await client.post(f"{P}/tasks/{b['id']}/move", json={"parent_id": a["id"]}, headers=auth)
    r = await client.post(f"{P}/tasks/{a['id']}/move", json={"parent_id": b["id"]}, headers=auth)
    assert r.status_code == 422 and r.json()["code"] == "bad_move"


async def test_move_last_child_out_keeps_parent_values(client, auth):
    p = (await _project(client, auth))["id"]
    ph = await _task(client, auth, p, "단계")
    kid = await _task(client, auth, p, "자식", parent_id=ph["id"], status="done", start_date="2026-10-01", end_date="2026-10-05")
    assert _by(await _tasks(client, auth, p), "단계")["status"] == "done"
    r = await client.post(f"{P}/tasks/{kid['id']}/move", json={"parent_id": None}, headers=auth)
    row = _by(r.json()["tasks"], "단계")
    # 자식을 잃고 말단이 되어도 방금 보이던 값이 그대로 남는다(0% 로 튀지 않음)
    assert (row["status"], row["progress"], row["has_children"]) == ("done", 100, False)
    assert (row["start_date"], row["end_date"]) == ("2026-10-01", "2026-10-05")


async def test_delete_subtree_and_cleanup(client, auth):
    p = (await _project(client, auth))["id"]
    a = await _task(client, auth, p, "A")
    a1 = await _task(client, auth, p, "A1", parent_id=a["id"])
    b = await _task(client, auth, p, "B", predecessors=[a1["id"]])
    c = await _task(client, auth, p, "C")
    note = (await client.post(f"{P}/projects/{p}/notes", json={"title": "이슈", "task_id": a1["id"]}, headers=auth)).json()
    r = await client.delete(f"{P}/tasks/{a['id']}", headers=auth)
    assert r.status_code == 200 and r.json()["removed"] == 2
    ts = r.json()["tasks"]
    assert [(t["wbs"], t["title"]) for t in ts] == [("1", "B"), ("2", "C")]
    assert _by(ts, "B")["predecessors"] == []
    async with sessionmaker()() as db:
        assert await db.scalar(select(PmsTask).where(PmsTask.id == uuid.UUID(a1["id"]))) is None
        n = await db.get(PmsNote, uuid.UUID(note["id"]))
        assert n is not None and n.task_id is None
    assert b["id"] and c["id"]


async def test_predecessors_validation(client, auth):
    p = (await _project(client, auth))["id"]
    a = await _task(client, auth, p, "A")
    b = await _task(client, auth, p, "B", predecessors=[a["id"]])
    assert b["predecessors"] == [a["id"]]
    r = await client.patch(f"{P}/tasks/{a['id']}", json={"predecessors": [b["id"]]}, headers=auth)
    assert r.status_code == 422 and r.json()["code"] == "predecessor_cycle"
    r = await client.patch(f"{P}/tasks/{a['id']}", json={"predecessors": [a["id"]]}, headers=auth)
    assert r.status_code == 422 and r.json()["code"] == "bad_predecessor"
    other = (await _project(client, auth, name="다른"))["id"]
    x = await _task(client, auth, other, "X")
    r = await client.patch(f"{P}/tasks/{a['id']}", json={"predecessors": [x["id"]]}, headers=auth)
    assert r.status_code == 422 and r.json()["code"] == "bad_predecessor"


async def test_depth_limit_and_bad_references(client, auth):
    p = (await _project(client, auth))["id"]
    parent = None
    for i in range(8):
        parent = (await _task(client, auth, p, f"L{i}", parent_id=parent))["id"]
    r = await client.post(f"{P}/projects/{p}/tasks", json={"title": "너무 깊음", "parent_id": parent}, headers=auth)
    assert r.status_code == 422 and r.json()["code"] == "too_deep"
    r = await client.post(f"{P}/projects/{p}/tasks", json={"title": "x", "parent_id": str(uuid.uuid4())}, headers=auth)
    assert r.status_code == 404
    r = await client.post(f"{P}/projects/{p}/tasks", json={"title": "x", "start_date": "2026-10-10", "end_date": "2026-10-01"}, headers=auth)
    assert r.status_code == 422 and r.json()["code"] == "bad_dates"
    assert (await client.patch(f"{P}/tasks/{uuid.uuid4()}", json={"title": "x"}, headers=auth)).status_code == 404


async def test_assignee_can_be_cleared(client, auth):
    p = (await _project(client, auth))["id"]
    t = await _task(client, auth, p, "작업", assignee="  민수 ")
    assert t["assignee"] == "민수"
    r = await client.patch(f"{P}/tasks/{t['id']}", json={"assignee": None}, headers=auth)
    assert r.json()["task"]["assignee"] is None
    r = await client.patch(f"{P}/tasks/{t['id']}", json={"title": "이름만"}, headers=auth)
    assert r.json()["task"]["assignee"] is None and r.json()["task"]["title"] == "이름만"


async def test_dashboard(client, auth):
    p = (await _project(client, auth, start_date="2026-10-01", end_date="2026-10-31", members=["민수"]))["id"]
    ph = await _task(client, auth, p, "구현")
    await _task(client, auth, p, "지연됨", parent_id=ph["id"], assignee="민수", start_date="2026-10-01", end_date="2026-10-05", progress=30)
    await _task(client, auth, p, "곧 마감", parent_id=ph["id"], assignee="민수", start_date="2026-10-10", end_date="2026-10-17", status="doing")
    await _task(client, auth, p, "멀었음", parent_id=ph["id"], assignee="지연", start_date="2026-10-20", end_date="2026-10-30")
    await _task(client, auth, p, "끝남", parent_id=ph["id"], start_date="2026-10-01", end_date="2026-10-02", status="done")
    await client.post(f"{P}/projects/{p}/milestones", json={"title": "베타", "due_date": "2026-10-20"}, headers=auth)
    await client.post(f"{P}/projects/{p}/milestones", json={"title": "킥오프", "due_date": "2026-10-01", "done": True}, headers=auth)
    await client.post(f"{P}/projects/{p}/notes", json={"title": "서버 비용", "kind": "risk", "severity": "high"}, headers=auth)
    await client.post(f"{P}/projects/{p}/notes", json={"title": "버튼 깨짐", "kind": "issue", "severity": "low"}, headers=auth)

    d = (await client.get(f"{P}/projects/{p}/dashboard?today=2026-10-13", headers=auth)).json()
    assert d["counts"] == {"total": 4, "todo": 1, "doing": 2, "review": 0, "done": 1, "overdue": 1, "due_soon": 1, "no_dates": 0}
    assert [t["title"] for t in d["overdue"]] == ["지연됨"] and d["overdue"][0]["days"] == -8
    assert [t["title"] for t in d["due_soon"]] == ["곧 마감"]
    assert d["planned_progress"] == 40 and d["days_left"] == 18
    assert [m["title"] for m in d["milestones"]] == ["킥오프", "베타"]
    assert d["milestones"][1]["days"] == 7 and not d["milestones"][1]["overdue"]
    assert d["notes"]["open"] == 2 and d["notes"]["risks"] == 1 and d["notes"]["high"] == 1
    assert d["notes"]["top"][0]["title"] == "서버 비용"
    by = {r["name"]: r for r in d["by_assignee"]}
    assert by["민수"]["total"] == 2 and by["민수"]["overdue"] == 1 and by["지연"]["todo"] == 1 and by[""]["done"] == 1
    assert d["by_assignee"][-1]["name"] == ""  # 미배정은 맨 끝
    assert d["phases"][0]["title"] == "구현" and d["progress"] == d["phases"][0]["progress"]

    listed = (await client.get(f"{P}/projects", headers=auth)).json()["projects"][0]
    assert listed["progress"] == d["progress"] and listed["counts"]["total"] == 4 and listed["open_notes"] == 2
    assert listed["next_milestone"]["title"] == "베타"


async def test_milestones_and_notes_crud(client, auth):
    p = (await _project(client, auth))["id"]
    t = await _task(client, auth, p, "작업")
    m = (await client.post(f"{P}/projects/{p}/milestones", json={"title": "알파", "due_date": "2026-11-01"}, headers=auth)).json()
    r = await client.patch(f"{P}/milestones/{m['id']}", json={"done": True, "title": "알파 출시"}, headers=auth)
    assert r.json()["done"] and r.json()["title"] == "알파 출시"
    assert len((await client.get(f"{P}/projects/{p}/milestones", headers=auth)).json()["milestones"]) == 1
    assert (await client.delete(f"{P}/milestones/{m['id']}", headers=auth)).status_code == 204

    n = (await client.post(f"{P}/projects/{p}/notes", json={"title": "로그인 실패", "task_id": t["id"], "owner": "민수"}, headers=auth)).json()
    assert n["status"] == "open" and n["task_id"] == t["id"] and n["kind"] == "issue"
    r = await client.patch(f"{P}/notes/{n['id']}", json={"status": "resolved"}, headers=auth)
    assert r.json()["status"] == "resolved" and r.json()["resolved_at"]
    r = await client.patch(f"{P}/notes/{n['id']}", json={"status": "open", "task_id": None}, headers=auth)
    assert r.json()["resolved_at"] is None and r.json()["task_id"] is None
    r = await client.post(f"{P}/projects/{p}/notes", json={"title": "x", "task_id": str(uuid.uuid4())}, headers=auth)
    assert r.status_code == 422 and r.json()["code"] == "bad_task"
    assert [x["id"] for x in (await client.get(f"{P}/projects/{p}/notes?status=resolved", headers=auth)).json()["notes"]] == []
    assert (await client.delete(f"{P}/notes/{n['id']}", headers=auth)).status_code == 204


async def test_delete_project_cascades(client, auth):
    p = (await _project(client, auth, phases=["a", "b"]))["id"]
    await client.post(f"{P}/projects/{p}/milestones", json={"title": "m", "due_date": "2026-11-01"}, headers=auth)
    await client.post(f"{P}/projects/{p}/notes", json={"title": "n"}, headers=auth)
    assert (await client.delete(f"{P}/projects/{p}", headers=auth)).status_code == 204
    async with sessionmaker()() as db:
        for model in (PmsTask, PmsMilestone, PmsNote):
            assert (await db.scalars(select(model))).first() is None


@pytest.mark.parametrize("bad", [{"name": ""}, {"name": "x", "scope": "team"}, {"name": "x", "status": "weird"}])
async def test_project_validation(client, auth, bad):
    r = await client.post(f"{P}/projects", json=bad, headers=auth)
    assert r.status_code == 422
