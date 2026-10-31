"""Session-authenticated recovery uses the existing mission pipeline and preserves active runs."""
import uuid

import pytest
from authz03_canary import ensure_authz03
from sqlalchemy import select

from ttakkari.collector import missions
from ttakkari.collector_models import (
    CollectionMission,
    CollectionMissionJob,
    CollectorJob,
)
from ttakkari.db import sessionmaker
from ttakkari.orchestrator.runtime import issue_token

BASE = "/api/agent/collection/missions"
pytestmark = pytest.mark.security


@pytest.fixture
async def seeded(make_dir):
    async with sessionmaker()() as db:
        env = await ensure_authz03(db, make_dir)
        mission = await missions.create(db, goal="디자인 시스템 UI/UX 지식", max_pages=10)
        linked = list(await db.scalars(select(CollectorJob).join(
            CollectionMissionJob, CollectionMissionJob.job_id == CollectorJob.id
        ).where(CollectionMissionJob.mission_id == mission.id)))
        for job in linked:
            job.status = "failed"
            job.attempts = 3
            job.last_error = {"code": "attempts_exhausted", "type": "FetchError", "message": "password=secret-value"}
        mission.status = "partial"
        await db.commit()
        return env, str(mission.id)


def headers(sid):
    return {"Authorization": f"Bearer {issue_token(sid)}"}


async def test_status_returns_failure_type_without_payload_or_credentials(client, seeded):
    env, mid = seeded
    response = await client.get(f"{BASE}/{mid}", headers=headers(env.personal_session_id))
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["id"] == mid
    assert body["jobs"][0]["error"]["type"] == "FetchError"
    assert body["jobs"][0]["error"]["code"] == "attempts_exhausted"
    assert "secret-value" not in response.text
    assert "plan" not in body and "goal" not in body
    assert body["counts"]["acquired"] == 0


async def test_retry_starts_one_new_run_and_duplicate_request_cannot_cancel_it(client, seeded):
    env, mid = seeded
    hdr = headers(env.personal_session_id)
    response = await client.post(f"{BASE}/{mid}/retry", headers=hdr, json={"expected_run_seq": 1})
    assert response.status_code == 202
    assert response.json()["run_seq"] == 2
    assert (await client.post(f"{BASE}/{mid}/retry", headers=hdr, json={"expected_run_seq": 1})).status_code == 409
    async with sessionmaker()() as db:
        row = await db.get(CollectionMission, uuid.UUID(mid))
        assert row.current_run_seq == 2
        jobs = list(await db.scalars(select(CollectorJob).join(
            CollectionMissionJob, CollectionMissionJob.job_id == CollectorJob.id
        ).where(CollectionMissionJob.mission_id == row.id, CollectionMissionJob.run_seq == 2)))
        assert jobs and all(job.status == "queued" and not job.cancel_requested for job in jobs)


async def test_retry_refuses_active_or_user_paused_mission(client, seeded):
    env, mid = seeded
    hdr = headers(env.personal_session_id)
    async with sessionmaker()() as db:
        job = await db.scalar(select(CollectorJob).join(
            CollectionMissionJob, CollectionMissionJob.job_id == CollectorJob.id
        ).where(CollectionMissionJob.mission_id == uuid.UUID(mid)))
        job.status = "running"
        await db.commit()
    assert (await client.post(f"{BASE}/{mid}/retry", headers=hdr, json={"expected_run_seq": 1})).status_code == 409
    async with sessionmaker()() as db:
        job = await db.get(CollectorJob, job.id)
        assert job.status == "running" and not job.cancel_requested
        job.status = "failed"
        row = await db.get(CollectionMission, uuid.UUID(mid))
        row.status = "paused"
        await db.commit()
    assert (await client.post(f"{BASE}/{mid}/retry", headers=hdr, json={"expected_run_seq": 1})).status_code == 409


async def test_agent_recovery_is_authenticated_and_owner_bound(client, seeded):
    env, mid = seeded
    assert (await client.get(f"{BASE}/{mid}")).status_code in (401, 403)
    assert (await client.post(f"{BASE}/{mid}/retry", json={"expected_run_seq": 1})).status_code in (401, 403)
    async with sessionmaker()() as db:
        row = await db.get(CollectionMission, uuid.UUID(mid))
        row.tenant_id = "someone-else"
        await db.commit()
    hdr = headers(env.personal_session_id)
    assert (await client.get(f"{BASE}/{mid}", headers=hdr)).status_code == 404
    assert (await client.post(f"{BASE}/{mid}/retry", headers=hdr, json={"expected_run_seq": 1})).status_code == 404


async def test_retry_rejects_arbitrary_parameters(client, seeded):
    env, mid = seeded
    hdr = headers(env.personal_session_id)
    for body in ({}, {"expected_run_seq": 0}, {"expected_run_seq": 1, "skip_policy": True}):
        assert (await client.post(f"{BASE}/{mid}/retry", headers=hdr, json=body)).status_code == 422


async def test_owner_work_session_can_inspect_and_retry_existing_mission(client, seeded):
    env, mid = seeded
    hdr = headers(env.work_session_id)
    assert (await client.get(f"{BASE}/{mid}", headers=hdr)).status_code == 200
    assert (await client.post(f"{BASE}/{mid}/retry", headers=hdr, json={"expected_run_seq": 1})).status_code == 202
