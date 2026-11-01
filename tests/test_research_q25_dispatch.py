"""The Qwen2.5 operation is fixed, work-scoped and owner-bound across its lifecycle."""

from __future__ import annotations

import pytest
from authz03_canary import ensure_authz03

from ttakkari.db import sessionmaker
from ttakkari.operations import jobs
from ttakkari.orchestrator.runtime import issue_token


def _hdr(session_id) -> dict[str, str]:
    return {"Authorization": f"Bearer {issue_token(session_id)}"}


async def test_personal_scope_cannot_start_fixed_work_research_operation(client, make_dir):
    async with sessionmaker()() as db:
        env = await ensure_authz03(db, make_dir)
    response = await client.post(
        "/api/agent/operations",
        json={"kind": "regna_q25_first6_cpu"},
        headers=_hdr(env.personal_session_id),
    )

    assert response.status_code == 403
    assert not any(row.get("kind") == "regna_q25_first6_cpu" for row in jobs.all_jobs())


async def test_fixed_research_operation_rejects_extra_paths(client, make_dir):
    async with sessionmaker()() as db:
        env = await ensure_authz03(db, make_dir)
    response = await client.post(
        "/api/agent/operations",
        json={"kind": "regna_q25_first6_cpu", "model_path": "/tmp/other.gguf"},
        headers=_hdr(env.work_session_id),
    )

    assert response.status_code == 422


async def test_work_session_starts_only_the_server_fixed_operation(client, make_dir, tmp_path, monkeypatch):
    async with sessionmaker()() as db:
        env = await ensure_authz03(db, make_dir)
    operations_root = tmp_path / "operations"
    (operations_root / "jobs").mkdir(parents=True)
    monkeypatch.setattr(jobs, "root", lambda: operations_root)

    def synthetic_start(job_id):
        return jobs.update(job_id, status="running", runner_pid=123, runner_started=1.0)

    monkeypatch.setattr(jobs, "spawn_heavy", synthetic_start)
    response = await client.post(
        "/api/agent/operations",
        json={"kind": "regna_q25_first6_cpu"},
        headers=_hdr(env.work_session_id),
    )

    assert response.status_code == 202, response.text
    assert response.json()["kind"] == "regna_q25_first6_cpu"
    assert response.json()["status"] == "running"
    assert "argv" not in response.json() and "path" not in response.json()


async def test_wrong_principal_cannot_read_or_cancel_another_work_job(client, make_dir, monkeypatch):
    async with sessionmaker()() as db:
        env = await ensure_authz03(db, make_dir)
    job = jobs.create("regna_q25_first6_cpu", str(env.work_session_id))
    monkeypatch.setattr(jobs, "get", lambda _job_id: job)
    monkeypatch.setattr(jobs, "reconciled", lambda _job_id: pytest.fail("unauthorized status must not reconcile owner job"))

    read = await client.get(f"/api/agent/operations/{job['id']}", headers=_hdr(env.personal_session_id))
    cancel = await client.post(f"/api/agent/operations/{job['id']}/cancel", headers=_hdr(env.personal_session_id))

    assert read.status_code in (403, 404)
    assert cancel.status_code in (403, 404)


async def test_work_operation_status_is_owner_bound(client, make_dir, monkeypatch):
    async with sessionmaker()() as db:
        env = await ensure_authz03(db, make_dir)
    job = jobs.create("regna_q25_first6_cpu", str(env.work_session_id))
    job["status"] = "running"
    monkeypatch.setattr(jobs, "reconciled", lambda _job_id: job)

    response = await client.get(f"/api/agent/operations/{job['id']}", headers=_hdr(env.work_session_id))

    assert response.status_code == 200
    assert response.json()["kind"] == "regna_q25_first6_cpu"
