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
    """C2: 개인 자료는 회사 계정 엔진(work 세션)에 보이지 않는다. 소유자가 engine-access 로 열었을 때만 읽힌다."""
    body = {"title": "개인 문서", "text": "개인 전용 오로라 검색문서"}
    made = (await client.post("/api/assistant/kb/documents", headers=auth, json=body)).json()
    doc_id = made["document_id"]

    # 회사 세션(work)은 개인 문서를 쿼리로 scope 를 주장해도 읽지 못한다(404 및 빈 검색).
    work = await client.get("/api/agent/kb/search?q=오로라&scope=personal", headers=headers["work"])
    assert work.status_code == 200, work.text
    assert work.json()["items"] == []
    assert (await client.get(f"/api/agent/kb/documents/{doc_id}", headers=headers["work"])).status_code == 404
    work_observations = (await client.get("/api/agent/kb/observations?q=개인", headers=headers["work"])).json()
    assert work_observations["counts"]["candidates"] == 0
    assert work_observations["items"] == []
    assert work_observations["scope"]["reader_pool"] == "work"

    # 개인 세션(personal)은 전체 도서관을 읽는다.
    personal = await client.get("/api/agent/kb/search?q=오로라", headers=headers["personal"])
    assert len(personal.json()["items"]) == 1
    summary = await client.get("/api/agent/kb/summary", headers=headers["personal"])
    assert summary.json()["counts"]["documents"] == 1
    observations = (await client.get("/api/agent/kb/observations?q=개인", headers=headers["personal"])).json()
    assert observations["counts"] == {"candidates": 1, "visible": 1, "excluded": {"context": 0, "covered": 0}}
    assert observations["items"][0]["document_id"] == doc_id
    assert observations["items"][0]["revisions"][0]["id"] == made["revision_id"]
    assert observations["items"][0]["revisions"][0]["passages"][0]["id"]
    assert "개인 전용 오로라 검색문서" not in str(observations)
    pid = personal.json()["items"][0]["passage_id"]
    assert (await client.get(f"/api/agent/kb/documents/{doc_id}", headers=headers["personal"])).status_code == 200
    assert (await client.get(f"/api/agent/kb/passages/{pid}/evidence", headers=headers["personal"])).status_code == 200
    assert (await client.get(f"/api/agent/kb/passages/{pid}/evidence", headers=headers["work"])).status_code == 404

    # 소유자가 회사 계정 엔진 접근을 열면 work 세션도 읽을 수 있다.
    grant = await client.put(f"/api/assistant/kb/documents/{doc_id}/engine-access", headers=auth,
                             json={"company_engine": True})
    assert grant.status_code == 200 and grant.json()["company_engine"] is True

    work_allowed = await client.get("/api/agent/kb/search?q=오로라", headers=headers["work"])
    assert len(work_allowed.json()["items"]) == 1
    assert (await client.get(f"/api/agent/kb/documents/{doc_id}", headers=headers["work"])).status_code == 200
    assert (await client.get(f"/api/agent/kb/passages/{pid}/evidence", headers=headers["work"])).status_code == 200


async def test_agent_kb_has_no_unauthenticated_access(client):
    for path in ("search?q=test", "summary", "observations", "documents", f"documents/{uuid.uuid4()}",
                 f"passages/{uuid.uuid4()}/evidence"):
        assert (await client.get("/api/agent/kb/" + path)).status_code == 401


async def test_agent_kb_mcp_read_options_select_historical_revision(client, auth, headers):
    first = (await client.post("/api/assistant/kb/documents", headers=auth, json={
        "title": "개정 옵션", "source_key": "kb-options", "text": "아주오래된표현만있는본문",
    })).json()
    revised = (await client.post("/api/assistant/kb/documents", headers=auth, json={
        "title": "개정 옵션", "source_key": "kb-options", "text": "현재판에는새표현만있다",
    })).json()
    old_detail = await client.get(f"/api/agent/kb/documents/{first['document_id']}", headers=headers["personal"],
                                  params={"revision_id": first["revision_id"], "passage_limit": 1,
                                          "passage_offset": 0})
    assert old_detail.status_code == 200
    assert old_detail.json()["selected_revision_id"] == first["revision_id"]
    historical = await client.get("/api/agent/kb/search", headers=headers["personal"], params={
        "q": "아주오래된표현", "document_id": first["document_id"], "current_only": "false", "offset": 0,
    })
    assert [item["revision"]["id"] for item in historical.json()["items"]] == [first["revision_id"]]
    current = await client.get("/api/agent/kb/search", headers=headers["personal"], params={
        "q": "아주오래된표현", "document_id": revised["document_id"], "current_only": "true",
    })
    assert current.json()["items"] == []

pytestmark = pytest.mark.security
