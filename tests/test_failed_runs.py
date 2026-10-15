from __future__ import annotations

from datetime import timedelta

from ttakkari.db import sessionmaker
from ttakkari.models import ChatSession, Run, RunStatus, utcnow


async def test_failed_runs_are_newest_first_paginated_and_filterable(client, auth, new_session):
    ws, session = await new_session()
    now = utcnow()
    async with sessionmaker()() as db:
        chat = await db.get(ChatSession, session["id"])
        chat.title = "실패 조회 기획"
        rows = [
            Run(session_id=chat.id, workspace_id=chat.workspace_id, prompt=f"실패 지시 {i}",
                engine="claude", model="opus", profile="claude-main", status=RunStatus.failed,
                error=f"실패 이유 {i}", created_at=now + timedelta(seconds=i),
                finished_at=now + timedelta(seconds=i))
            for i in (1, 2, 3)
        ]
        rows.append(Run(session_id=chat.id, workspace_id=chat.workspace_id, prompt="성공 지시",
                        engine="claude", model="opus", status=RunStatus.succeeded,
                        created_at=now + timedelta(seconds=4), finished_at=now + timedelta(seconds=4)))
        db.add_all(rows)
        await db.commit()

    page1 = await client.get("/api/runs/failed", params={"limit": 2}, headers=auth)
    assert page1.status_code == 200, page1.text
    body = page1.json()
    assert [item["prompt"] for item in body["items"]] == ["실패 지시 3", "실패 지시 2"]
    assert all(item["status"] == "failed" for item in body["items"])
    assert body["items"][0]["session_title"] == "실패 조회 기획"
    assert body["items"][0]["workspace_name"] == ws["name"]
    assert body["items"][0]["profile"] == "claude-main"
    assert body["next_before"]

    page2 = await client.get("/api/runs/failed", params={"limit": 2, "before": body["next_before"],
                                                           "before_id": body["next_before_id"]}, headers=auth)
    assert [item["prompt"] for item in page2.json()["items"]] == ["실패 지시 1"]
    assert page2.json()["next_before"] is None

    since = (now + timedelta(seconds=2)).isoformat()
    filtered = await client.get("/api/runs/failed", params={"since": since}, headers=auth)
    assert [item["prompt"] for item in filtered.json()["items"]] == ["실패 지시 3", "실패 지시 2"]
