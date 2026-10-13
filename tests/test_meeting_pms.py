"""회의록의 프로젝트 선택은 PMS 프로젝트를 정본으로 삼는다."""
import uuid

from test_meetings import _agent_headers, _create

from ttakkari.db import sessionmaker
from ttakkari.meetings import pipeline
from ttakkari.models import (
    CtxMemory,
    Meeting,
    MeetingItem,
    MeetingItemKind,
    MeetingStatus,
)


async def test_pms_project_create_read_filter_patch_and_scope(client, auth, make_dir):
    personal = (await client.post("/api/pms/projects", json={"name": "PMS 개인"}, headers=auth)).json()
    work = (await client.post("/api/pms/projects", json={"name": "PMS 회사", "scope": "work"}, headers=auth)).json()
    m = await _create(client, auth, title="PMS 연결 회의", project_id=personal["id"], transcript="첫 회의")
    assert m["project"] == {"id": personal["id"], "name": personal["name"]}
    rows = (await client.get("/api/meetings", params={"project_id": personal["id"]}, headers=auth)).json()["meetings"]
    assert [r["id"] for r in rows] == [m["id"]]
    assert (await client.patch(f'/api/meetings/{m["id"]}', json={"project_id": work["id"]}, headers=auth)).status_code == 400
    ph = await _agent_headers(client, auth, make_dir)
    assert (await client.get("/api/agent/meetings", params={"project": "PMS 개인"}, headers=ph)).json()["meetings"][0]["id"] == m["id"]
    async with sessionmaker()() as db:
        saved = await db.get(Meeting, uuid.UUID(m["id"]))
        assert str(saved.pms_project_id) == personal["id"]
        assert saved.entity_id is None
    cleared = (await client.patch(f'/api/meetings/{m["id"]}', json={"project_id": None}, headers=auth)).json()
    assert cleared["project"] is None
    patched = (await client.patch(f'/api/meetings/{m["id"]}', json={"project_id": personal["id"]}, headers=auth)).json()
    assert patched["project"]["id"] == personal["id"]
    assert (await client.delete(f'/api/pms/projects/{personal["id"]}', headers=auth)).status_code == 204
    assert (await client.get(f'/api/meetings/{m["id"]}', headers=auth)).json()["project"] is None


async def test_pms_previous_actions_and_project_search(client, auth, make_dir):
    p = (await client.post("/api/pms/projects", json={"name": "PMS 실행"}, headers=auth)).json()
    first = await _create(client, auth, title="첫 회의", project_id=p["id"], transcript="배포 준비")
    second = await _create(client, auth, title="후속 회의", project_id=p["id"], transcript="배포 점검")
    async with sessionmaker()() as db:
        prev = await db.get(Meeting, uuid.UUID(first["id"]))
        prev.status = MeetingStatus.analyzed
        db.add(MeetingItem(meeting_id=prev.id, kind=MeetingItemKind.action, position=0, text="배포 준비", status="open"))
        db.add(CtxMemory(kind="decision", scope="personal", content="기존 확정 결정", entity_ids=[], source="meeting", source_ref=f"meeting:{prev.id}#decision"))
        await db.commit()
        refs = await pipeline.gather_context(db, await db.get(Meeting, uuid.UUID(second["id"])))
        assert any(r["type"] == "memory" and r["text"] == "기존 확정 결정" for r in refs.values())
        assert any(r["type"] == "action" and r["text"] == "배포 준비" for r in refs.values())
    ph = await _agent_headers(client, auth, make_dir)
    items = (await client.get("/api/agent/meetings/search", params={"project": p["name"], "q": "배포"}, headers=ph)).json()["items"]
    assert items[0]["meeting"]["project"] == p["name"]
