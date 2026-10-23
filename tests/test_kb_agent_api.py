import uuid

import pytest

from ttakkari.db import sessionmaker
from ttakkari.models import ChatSession, Workspace
from ttakkari.orchestrator.runtime import issue_token


@pytest.fixture
async def headers(make_dir):
    root = make_dir()
    async with sessionmaker()() as db:
        workspace = Workspace(name="KB agent", root_path=str(root))
        db.add(workspace)
        await db.flush()
        result = {}
        for scope in ("personal", "work"):
            row = ChatSession(workspace_id=workspace.id, title=scope, engine="codex", pool=scope)
            db.add(row)
            await db.flush()
            result[scope] = {"Authorization": "Bearer " + issue_token(row.id)}
        await db.commit()
    return result


async def test_agent_kb_search_scope_comes_from_session(client, auth, headers):
    body = {"title": "회사 문서", "text": "회사 전용 오로라 검색문서", "scope": "work"}
    made = (await client.post("/api/assistant/kb/documents", headers=auth, json=body)).json()
    personal = await client.get("/api/agent/kb/search?q=오로라&scope=work", headers=headers["personal"])
    assert personal.status_code == 200, personal.text
    assert personal.json()["items"] == []
    work = await client.get("/api/agent/kb/search?q=오로라", headers=headers["work"])
    assert len(work.json()["items"]) == 1
    doc = await client.get("/api/agent/kb/documents/" + made["document_id"], headers=headers["personal"])
    assert doc.status_code == 404
    pid = work.json()["items"][0]["passage_id"]
    assert (await client.get(f"/api/agent/kb/passages/{pid}/evidence", headers=headers["personal"])).status_code == 404
    assert (await client.get(f"/api/agent/kb/passages/{pid}/evidence", headers=headers["work"])).status_code == 200


async def test_agent_kb_has_no_unauthenticated_access(client):
    for path in ("search?q=test", "documents", f"documents/{uuid.uuid4()}", f"passages/{uuid.uuid4()}/evidence"):
        assert (await client.get("/api/agent/kb/" + path)).status_code == 401

pytestmark = pytest.mark.security
