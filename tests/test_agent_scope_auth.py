from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest

from ttakkari.db import sessionmaker
from ttakkari.models import ChatSession, Workspace
from ttakkari.orchestrator.runtime import issue_token


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
