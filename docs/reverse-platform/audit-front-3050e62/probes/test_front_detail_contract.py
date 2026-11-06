"""Independent frontend-consumer probes against the pinned backend ASGI API."""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest
from sqlalchemy import select

pytest_plugins = ["conftest"]

# Reuse the unchanged core audit's fixture helpers and temp-PostgreSQL harness.
from test_core_audit import _advance_implementation, _finish_verifier, _start  # noqa: E402
from ttakkari.db import sessionmaker
from ttakkari.models import ReverseJob


@pytest.mark.parametrize(
    ("comparisons", "failed_required", "expected_status", "expected_count"),
    [(True, True, "failed", 1), (False, False, "failed", 0)],
)
async def test_detail_api_failure_and_zero_comparison_are_consumer_consistent(
    client, auth, monkeypatch, comparisons, failed_required, expected_status, expected_count
):
    job_id = await _start(client, auth, monkeypatch)
    async with sessionmaker()() as db:
        job = await db.get(ReverseJob, uuid.UUID(job_id))
        implementation, verifier_run = await _advance_implementation(db, job)
        await _finish_verifier(db, job, verifier_run, comparisons=comparisons,
                               failed_required=failed_required)
        verifier_session_id = job.verifier_session_id
        implementation_session_id = job.session_id
        expected_sources = ["src/main.py"]
        expected_log = "probe.log"
        expected_output_path = job.output_path
        await db.commit()

    response = await client.get(f"/api/reverse/{job_id}", headers=auth)
    assert response.status_code == 200, response.text
    detail = response.json()
    latest = detail["latest"]

    assert detail["result"]["latest"] == latest
    assert detail["result"]["history"] == detail["history"]
    assert detail["result_state"]["source"]["files"] == expected_sources
    assert latest["source"]["files"] == expected_sources
    assert latest["execution"]["checks"][0]["log"] == expected_log
    assert detail["output_path"] == expected_output_path
    assert detail["session_id"] == str(implementation_session_id)
    assert detail["verifier_session_id"] == str(verifier_session_id)
    assert detail["session_id"] != detail["verifier_session_id"]
    assert latest["independent_verification"]["status"] == expected_status
    assert latest["independent_verification"]["run_id"] == str(verifier_run.id)
    assert latest["verification_run_id"] == str(verifier_run.id)
    assert latest["verification_run_id"] != latest["run_id"]
    assert latest["source"]["files"] == expected_sources
    assert latest["execution"]["checks"][0]["log"] == expected_log
    assert detail["result_state"]["independent_verification"]["status"] == expected_status
    # result_state.direct_comparison mirrors the implementation report; verifier
    # evidence has its own fields and must not overwrite that user-facing comparison.
    assert detail["result_state"]["direct_comparison"] == latest["direct_comparison"]
    assert latest["verification_direct_comparison"]["count"] == expected_count
    assert latest["verification_direct_comparison"]["status"] == ("match" if expected_count else "unverified")
    if expected_count == 0:
        assert latest["verification_comparisons"] == []

    # Reconstruction.tsx joins each report-relative path to output_path before calling
    # /api/artifacts/resolve. Feed those exact API values through that resolver and
    # fetch content, which exercises the backend side of the frontend consumer path.
    for rel_path, expected_body in ((expected_sources[0], "print(4)\n"),
                                    (expected_log, "probe output\n")):
        resolved = await client.post(
            "/api/artifacts/resolve", headers=auth,
            json={"path": f"{expected_output_path}/{rel_path}",
                  "workspace_id": detail["workspace_id"], "session_id": detail["session_id"]},
        )
        assert resolved.status_code == 200, resolved.text
        artifact = resolved.json()
        assert artifact["filename"] == Path(rel_path).name
        content = await client.get(f"/api/artifacts/{artifact['id']}/content?variant=original", headers=auth)
        assert content.status_code == 200, content.text
        assert content.text == expected_body


async def test_pending_retry_api_has_empty_latest_and_keeps_history_consistent(
    client, auth, monkeypatch
):
    job_id = await _start(client, auth, monkeypatch)
    async with sessionmaker()() as db:
        job = await db.get(ReverseJob, uuid.UUID(job_id))
        implementation_session_id = job.session_id
        _, verifier_run = await _advance_implementation(db, job)
        # A successful job is intentionally not retryable. Produce a verifier failure,
        # persist it, then retry the allowed partial state to observe pending detail.
        await _finish_verifier(db, job, verifier_run, failed_required=True)
        assert job.status == "partial"
        await db.commit()

    retry = await client.post(f"/api/reverse/{job_id}/retry", headers=auth)
    assert retry.status_code == 202, retry.text
    detail_response = await client.get(f"/api/reverse/{job_id}", headers=auth)
    assert detail_response.status_code == 200, detail_response.text
    detail = detail_response.json()
    assert detail["result"]["latest"] is None
    assert detail["latest"] is None
    assert detail["result"]["history"] == detail["history"]
    assert detail["result_state"]["source"]["files"] == []
    assert detail["result_state"]["direct_comparison"]["status"] == "unverified"
    assert detail["result_state"]["direct_comparison"]["count"] == 0
    assert detail["result_state"]["independent_verification"]["status"] == "pending"
    assert detail["verifier_session_id"] is None
    assert detail["session_id"] == str(implementation_session_id)
