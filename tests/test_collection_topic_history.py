"""Repeated intent is one topic; old acquisition ledgers remain intact."""
import asyncio
import uuid

import pytest
from sqlalchemy import func, select

from ttakkari.collector_models import CollectionMission, CollectionMissionJob
from ttakkari.db import sessionmaker

pytestmark = pytest.mark.security
BASE = "/api/assistant/collect/missions"


async def test_repeated_submission_reuses_legacy_mission_without_new_jobs(client, auth):
    body = {"goal": "하네스 환경과 피드백 조사", "context": "참고 가설"}
    old = (await client.post(BASE, headers=auth, json={**body, "request_key": "old-client"})).json()
    responses = await asyncio.gather(*[
        client.post(BASE, headers=auth, json={**body, "reuse_existing": True}) for _ in range(2)
    ])
    assert [r.status_code for r in responses] == [202, 202]
    assert {r.json()["id"] for r in responses} == {old["id"]}
    async with sessionmaker()() as db:
        assert await db.scalar(select(func.count()).select_from(CollectionMission)) == 1
        assert await db.scalar(select(func.count()).select_from(CollectionMissionJob)) == len(old["jobs"])


async def test_topic_key_survives_recovery_and_planner_changes_but_separates_context(client, auth):
    body = {"goal": "동일한 제목\n하네스 환경 조사"}
    first = (await client.post(BASE, headers=auth, json={**body, "request_key": "first"})).json()
    await client.post(f"{BASE}/{first['id']}/cancel", headers=auth)
    recovery = (await client.post(BASE, headers=auth, json={**body, "request_key": "repair"})).json()
    assert first["id"] != recovery["id"]
    assert first["topic_key"] == recovery["topic_key"]
    async with sessionmaker()() as db:
        row = await db.get(CollectionMission, uuid.UUID(first["id"]))
        row.plan = {**row.plan, "queries": ["old incorrect query"], "title": "Wrong old title"}
        await db.commit()
    current = await client.post(BASE, headers=auth, json={**body, "reuse_existing": True})
    assert current.json()["id"] == recovery["id"]
    other_context = await client.post(BASE, headers=auth, json={**body, "context": "별도 맥락", "reuse_existing": True})
    other_goal = await client.post(BASE, headers=auth, json={"goal": "동일한 제목\n로봇 제어 조사", "reuse_existing": True})
    assert len({r["topic_key"] for r in [first, other_context.json(), other_goal.json()]}) == 3
    listed = (await client.get(BASE + "?limit=2", headers=auth)).json()
    assert listed["has_more"] is True
    assert len(listed["items"]) == 2
    assert all(m["run_count"] == 1 for m in listed["items"])
    assert (await client.get(BASE + "?limit=2&offset=2", headers=auth)).json()["has_more"] is False


async def test_repeated_initial_submission_and_distinct_references(client, auth):
    body = {"goal": "하네스設計と環境", "reuse_existing": True}
    a, b = await asyncio.gather(*[client.post(BASE, headers=auth, json=body) for _ in range(2)])
    assert a.status_code == b.status_code == 202
    assert a.json()["id"] == b.json()["id"]
    for url in ["https://example.org/one", "https://example.org/two"]:
        r = await client.post(BASE, headers=auth, json={**body, "urls": [url]})
        assert r.status_code == 202, r.text
        assert r.json()["topic_key"] != a.json()["topic_key"]
    assert len((await client.get(BASE, headers=auth)).json()["items"]) == 3


async def test_topic_controls_require_owner_and_preview_does_not_write(client, auth):
    assert (await client.get(BASE)).status_code == 401
    assert (await client.post(BASE, json={"goal": "보호된 주제", "reuse_existing": True})).status_code == 401
    assert (await client.post(BASE + "/plan", headers=auth, json={"goal": "미리보기", "reuse_existing": True})).status_code == 200
    assert (await client.get(BASE, headers=auth)).json()["items"] == []
