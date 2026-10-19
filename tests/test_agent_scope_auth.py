from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest

from ttakkari.db import sessionmaker
from ttakkari.models import (
    Artifact,
    ChatSession,
    CtxEntity,
    CtxMemory,
    CtxRelation,
    Workspace,
)
from ttakkari.orchestrator.runtime import issue_token
from ttakkari.security.auth import issue_access_token


@pytest.fixture
async def setup_sessions(make_dir):
    root = make_dir()
    async with sessionmaker()() as db:
        ws = Workspace(id=uuid.uuid4(), name=root.name, root_path=str(root))
        db.add(ws)
        await db.flush()

        personal_session = ChatSession(
            id=uuid.uuid4(),
            workspace_id=ws.id,
            title="Personal Session",
            engine="codex",
            pool="personal",
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
        )
        work_session = ChatSession(
            id=uuid.uuid4(),
            workspace_id=ws.id,
            title="Work Session",
            engine="codex",
            pool="work",
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
        )
        db.add_all([personal_session, work_session])
        await db.commit()
    return personal_session, work_session


@pytest.mark.asyncio
async def test_agent_sessions_list_isolation(client, setup_sessions):
    personal_s, work_s = setup_sessions

    # 1. Personal session listing: sees both
    p_token = issue_token(personal_s.id)
    resp = await client.get("/api/agent/sessions", headers={"Authorization": f"Bearer {p_token}"})
    assert resp.status_code == 200
    titles = [s["title"] for s in resp.json()["sessions"]]
    assert "Personal Session" in titles
    assert "Work Session" in titles

    # 2. Work session listing: sees only work sessions
    w_token = issue_token(work_s.id)
    resp = await client.get("/api/agent/sessions", headers={"Authorization": f"Bearer {w_token}"})
    assert resp.status_code == 200
    w_titles = [s["title"] for s in resp.json()["sessions"]]
    assert "Work Session" in w_titles
    assert "Personal Session" not in w_titles


@pytest.mark.asyncio
async def test_agent_endpoint_rejects_missing_garbage_and_wrong_audience_tokens(client, setup_sessions):
    personal_session, _ = setup_sessions
    user_token, _ = issue_access_token()
    invalid_headers = [
        {},
        {"Authorization": "Bearer not.a.signed.token"},
        {"Authorization": f"Bearer {user_token}"},
    ]
    for headers in invalid_headers:
        response = await client.get("/api/agent/sessions", headers=headers)
        assert response.status_code == 401

    valid = await client.get(
        "/api/agent/sessions",
        headers={"Authorization": f"Bearer {issue_token(personal_session.id)}"},
    )
    assert valid.status_code == 200


@pytest.mark.asyncio
async def test_agent_read_session_scope_enforcement(client, setup_sessions):
    personal_s, work_s = setup_sessions
    w_token = issue_token(work_s.id)
    p_token = issue_token(personal_s.id)

    # 1. Work session trying to read personal session -> 403 Forbidden
    resp = await client.get(f"/api/agent/sessions/{personal_s.id}", headers={"Authorization": f"Bearer {w_token}"})
    assert resp.status_code == 403
    assert "회사 세션" in resp.json()["message"]

    # 2. Work session reading work session -> 200 OK
    resp = await client.get(f"/api/agent/sessions/{work_s.id}", headers={"Authorization": f"Bearer {w_token}"})
    assert resp.status_code == 200
    assert resp.json()["title"] == "Work Session"

    # 3. Personal session reading personal session -> 200 OK
    resp = await client.get(f"/api/agent/sessions/{personal_s.id}", headers={"Authorization": f"Bearer {p_token}"})
    assert resp.status_code == 200
    assert resp.json()["title"] == "Personal Session"


@pytest.mark.asyncio
async def test_work_agent_cannot_forget_personal_memory(client, setup_sessions):
    _, work_s = setup_sessions
    personal_memory = CtxMemory(content="private synthetic memory", scope="personal", source="agent")
    work_memory = CtxMemory(content="work synthetic memory", scope="work", source="agent")
    async with sessionmaker()() as db:
        db.add_all([personal_memory, work_memory])
        await db.commit()

    headers = {"Authorization": f"Bearer {issue_token(work_s.id)}"}
    forbidden = await client.post("/api/agent/forget", headers=headers, json={"id": str(personal_memory.id)})
    assert forbidden.status_code == 404
    allowed = await client.post("/api/agent/forget", headers=headers, json={"id": str(work_memory.id)})
    assert allowed.status_code == 200
    assert allowed.json() == {"forgotten": True}
    async with sessionmaker()() as db:
        stored_personal = await db.get(CtxMemory, personal_memory.id)
        stored_work = await db.get(CtxMemory, work_memory.id)
        assert stored_personal is not None and stored_personal.archived_at is None
        assert stored_work is not None and stored_work.archived_at is not None


@pytest.mark.asyncio
async def test_work_entity_detail_does_not_include_personal_relations(client, setup_sessions):
    _, work_s = setup_sessions
    subject = CtxEntity(type="topic", name="work-subject", scope="work", aliases=[], attributes={})
    private_target = CtxEntity(type="topic", name="personal-target", scope="personal", aliases=[], attributes={})
    async with sessionmaker()() as db:
        db.add_all([subject, private_target])
        await db.flush()
        db.add(CtxRelation(subject_id=subject.id, predicate="related_to", object_id=private_target.id,
                           note="synthetic cross-scope relation", source="agent"))
        await db.commit()

    headers = {"Authorization": f"Bearer {issue_token(work_s.id)}"}
    response = await client.post("/api/agent/entities", headers=headers, json={"name": "work-subject"})
    assert response.status_code == 200
    assert response.json()["relations"] == []


@pytest.mark.asyncio
async def test_agent_stocks_scope_enforcement(client, setup_sessions):
    personal_s, work_s = setup_sessions
    w_token = issue_token(work_s.id)
    p_token = issue_token(personal_s.id)

    # Work session calling agent stocks -> 403 Forbidden
    resp = await client.get("/api/agent/stocks/tossinvest/status", headers={"Authorization": f"Bearer {w_token}"})
    assert resp.status_code == 403
    assert "개인 세션" in resp.json()["message"]

    # Personal session calling agent stocks -> 200 OK
    resp = await client.get("/api/agent/stocks/tossinvest/status", headers={"Authorization": f"Bearer {p_token}"})
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_agent_work_artifacts_are_workspace_and_session_scoped(client, setup_sessions, make_dir):
    personal_s, work_s = setup_sessions
    other_root = make_dir()
    async with sessionmaker()() as db:
        other_ws = Workspace(id=uuid.uuid4(), name="other", root_path=str(other_root))
        db.add(other_ws)
        await db.flush()

        def artifact(name: str, workspace_id: uuid.UUID, session_id: uuid.UUID | None,
                     source: str = "agent_output") -> Artifact:
            return Artifact(filename=name, mime_type="text/plain", kind="text", source=source,
                            workspace_id=workspace_id, session_id=session_id, original_path=f"/{name}",
                            rel_path=name, storage_mode="copy", stored_path=f"/{name}", size_bytes=1,
                            sha256="0" * 64, retention_until=None)

        personal_artifact = artifact("personal-private.txt", personal_s.workspace_id, personal_s.id)
        work_artifact = artifact("work-allowed.txt", work_s.workspace_id, work_s.id)
        db.add_all([
            personal_artifact,
            work_artifact,
            artifact("shared-workspace.txt", work_s.workspace_id, None, "user_registered"),
            artifact("unowned-orphan.txt", work_s.workspace_id, None),
            artifact("other-workspace.txt", other_ws.id, None),
        ])
        await db.commit()

    work_headers = {"Authorization": f"Bearer {issue_token(work_s.id)}"}
    personal_headers = {"Authorization": f"Bearer {issue_token(personal_s.id)}"}
    work_resp = await client.get("/api/agent/artifacts", headers=work_headers)
    assert work_resp.status_code == 200
    work_names = {item["filename"] for item in work_resp.json()["artifacts"]}
    assert work_names == {"work-allowed.txt", "shared-workspace.txt"}
    injected_filter = await client.get(
        "/api/agent/artifacts", headers=work_headers, params={"q": "%' OR 1=1 --"}
    )
    assert injected_filter.status_code == 200
    assert injected_filter.json()["artifacts"] == []
    forbidden_move = await client.patch(
        f"/api/agent/artifacts/{personal_artifact.id}",
        headers=work_headers,
        json={"area": "files"},
    )
    assert forbidden_move.status_code == 404
    allowed_move = await client.patch(
        f"/api/agent/artifacts/{work_artifact.id}",
        headers=work_headers,
        json={"area": "files"},
    )
    assert allowed_move.status_code == 200

    # Personal sessions preserve the existing cross-workspace library listing behavior.
    personal_resp = await client.get("/api/agent/artifacts", headers=personal_headers)
    assert personal_resp.status_code == 200
    personal_names = {item["filename"] for item in personal_resp.json()["artifacts"]}
    assert personal_names == {"personal-private.txt", "work-allowed.txt", "shared-workspace.txt",
                              "unowned-orphan.txt", "other-workspace.txt"}
