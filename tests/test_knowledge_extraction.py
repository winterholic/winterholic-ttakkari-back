"""Failure-only retry and server-observed extraction execution provenance."""

import uuid

from test_knowledge import BASE, ingest

from ttakkari.db import sessionmaker


async def start(client, auth, revision):
    r = await client.post(
        BASE + "/extractions", headers=auth, json={"revision_id": revision["id"]}
    )
    assert r.status_code == 201, r.text
    return r.json()


async def batch(client, auth, job):
    r = await client.get(BASE + f"/extractions/{job['id']}/batch", headers=auth)
    assert r.status_code == 200, r.text
    return r.json()


async def test_failure_retry_is_bounded_idempotent_and_keeps_parent_history(
    client, auth
):
    revision = await ingest(
        client, auth, body="첫 번째 원문\n\n두 번째 원문\n\n세 번째 원문"
    )
    job = await start(client, auth, revision)
    units = (await batch(client, auth, job))["units"]
    results = [
        {"unit_id": u["id"], "outcome": o, "claims": [], "reason": reason}
        for u, o, reason in zip(
            units,
            ["no_claim", "error", "unsupported"],
            ["명시적 관계 없음", "model_output_invalid", "image_adapter_unavailable"],
            strict=True,
        )
    ]
    r = await client.post(
        BASE + f"/extractions/{job['id']}/batches",
        headers=auth,
        json={"expected_cursor": 0, "results": results},
    )
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "partial"
    parent = (await client.get(BASE + f"/extractions/{job['id']}", headers=auth)).json()
    assert parent["dispositions"][units[1]["id"]]["reason"] == "model_output_invalid"
    original = job
    for attempt in range(1, 4):
        retry = await client.post(
            BASE + f"/extractions/{job['id']}/retry", headers=auth, json={}
        )
        assert retry.status_code == 201, retry.text
        child = retry.json()
        again = (
            await client.post(
                BASE + f"/extractions/{job['id']}/retry", headers=auth, json={}
            )
        ).json()
        assert again["id"] == child["id"]
        assert child["retry_attempt"] == attempt
        assert child["root_job_id"] == original["id"]
        next_units = (await batch(client, auth, child))["units"]
        assert [u["id"] for u in next_units] == [units[1]["id"]]
        if attempt < 3:
            done = await client.post(
                BASE + f"/extractions/{child['id']}/batches",
                headers=auth,
                json={"expected_cursor": 0, "results": [results[1]]},
            )
            assert done.status_code == 200, done.text
        job = child
    # Finish the third retry with an explicit no-claim result, preserving the failed original.
    done = await client.post(
        BASE + f"/extractions/{job['id']}/batches",
        headers=auth,
        json={
            "expected_cursor": 0,
            "results": [
                {
                    "unit_id": units[1]["id"],
                    "outcome": "no_claim",
                    "claims": [],
                    "reason": "원문 재대조",
                }
            ],
        },
    )
    assert done.status_code == 200 and done.json()["status"] == "succeeded"
    assert (
        await client.get(BASE + f"/extractions/{original['id']}", headers=auth)
    ).json()["counts"]["errors"] == 1
    assert (
        await client.post(
            BASE + f"/extractions/{job['id']}/retry", headers=auth, json={}
        )
    ).status_code == 409


async def test_reported_errors_need_reason_and_do_not_advance_on_validation_failure(
    client, auth
):
    job = await start(client, auth, await ingest(client, auth))
    u = (await batch(client, auth, job))["units"][0]
    r = await client.post(
        BASE + f"/extractions/{job['id']}/batches",
        headers=auth,
        json={
            "expected_cursor": 0,
            "results": [{"unit_id": u["id"], "outcome": "error", "claims": []}],
        },
    )
    assert r.status_code == 422
    assert (await batch(client, auth, job))["cursor"] == 0


async def test_agent_run_identity_is_observed_by_server_not_supplied_in_payload(
    client, auth, make_dir
):
    from ttakkari.models import Run, utcnow
    from ttakkari.orchestrator.runtime import issue_token

    workspace = (
        await client.post(
            "/api/workspaces",
            headers=auth,
            json={"name": "trace", "root_path": str(make_dir())},
        )
    ).json()
    session = (
        await client.post(
            "/api/sessions", headers=auth, json={"workspace_id": workspace["id"]}
        )
    ).json()
    rid = uuid.uuid4()
    async with sessionmaker()() as db:
        db.add(
            Run(
                id=rid,
                session_id=uuid.UUID(session["id"]),
                workspace_id=uuid.UUID(workspace["id"]),
                prompt="fixture",
                status="running",
                engine="codex",
                model="gpt-6.1-sol",
                effort="high",
                started_at=utcnow(),
            )
        )
        await db.commit()
    token = {"Authorization": "Bearer " + issue_token(session["id"])}
    revision = await ingest(client, auth)
    r = await client.post(
        "/api/agent/knowledge/extractions",
        headers=token,
        json={"revision_id": revision["id"]},
    )
    assert r.status_code == 201, r.text
    job = r.json()
    u = (
        await client.get(
            "/api/agent/knowledge/extractions/" + job["id"] + "/batch", headers=token
        )
    ).json()["units"][0]
    result = {
        "unit_id": u["id"],
        "outcome": "no_claim",
        "claims": [],
        "reason": "관계 없음",
    }
    submitted = await client.post(
        "/api/agent/knowledge/extractions/" + job["id"] + "/batches",
        headers=token,
        json={"expected_cursor": 0, "results": [result]},
    )
    assert submitted.status_code == 200, submitted.text
    details = await client.get(
        "/api/agent/knowledge/extractions/" + job["id"], headers=token
    )
    assert details.status_code == 200, details.text
    event = details.json()["batches"][0]
    assert event["execution"]["run_id"] == str(rid)
    assert event["execution"]["session_id"] == session["id"]
    assert event["execution"]["model"] == "gpt-6.1-sol"
    assert event["execution"]["cost_scope"] == "entire_run_not_extraction"
    assert len(event["prompt_sha256"]) == 64
    # Identical replay must not duplicate receipt history.
    await client.post(
        "/api/agent/knowledge/extractions/" + job["id"] + "/batches",
        headers=token,
        json={"expected_cursor": 0, "results": [result]},
    )
    assert (
        len(
            (
                await client.get(
                    "/api/agent/knowledge/extractions/" + job["id"], headers=token
                )
            ).json()["batches"]
        )
        == 1
    )


async def test_permission_failure_is_not_retried_unchanged(client, auth):
    job = await start(client, auth, await ingest(client, auth))
    u = (await batch(client, auth, job))["units"][0]
    r = await client.post(
        BASE + f"/extractions/{job['id']}/batches",
        headers=auth,
        json={
            "expected_cursor": 0,
            "results": [
                {
                    "unit_id": u["id"],
                    "outcome": "error",
                    "claims": [],
                    "reason": "permission_denied: operation not permitted",
                }
            ],
        },
    )
    assert r.status_code == 200, r.text
    retry = await client.post(
        BASE + f"/extractions/{job['id']}/retry", headers=auth, json={}
    )
    assert retry.status_code == 409
    assert "no_retryable_units" in retry.text


async def test_fourth_retry_is_rejected_even_when_all_attempts_keep_failing(
    client, auth
):
    job = await start(client, auth, await ingest(client, auth))
    u = (await batch(client, auth, job))["units"][0]
    error = {
        "unit_id": u["id"],
        "outcome": "error",
        "claims": [],
        "reason": "transient_timeout",
    }
    for attempt in range(4):
        done = await client.post(
            BASE + f"/extractions/{job['id']}/batches",
            headers=auth,
            json={"expected_cursor": 0, "results": [error]},
        )
        assert done.status_code == 200, done.text
        retry = await client.post(
            BASE + f"/extractions/{job['id']}/retry", headers=auth, json={}
        )
        if attempt == 3:
            assert retry.status_code == 409 and "retry_budget_exceeded" in retry.text
        else:
            assert retry.status_code == 201, retry.text
            job = retry.json()


async def test_retry_does_not_silently_accept_forged_execution_fields(client, auth):
    job = await start(client, auth, await ingest(client, auth))
    u = (await batch(client, auth, job))["units"][0]
    await client.post(
        BASE + f"/extractions/{job['id']}/batches",
        headers=auth,
        json={
            "expected_cursor": 0,
            "results": [
                {
                    "unit_id": u["id"],
                    "outcome": "error",
                    "claims": [],
                    "reason": "transient_timeout",
                }
            ],
        },
    )
    result = await client.post(
        BASE + f"/extractions/{job['id']}/retry",
        headers=auth,
        json={"model": "fake", "run_id": str(uuid.uuid4())},
    )
    assert result.status_code == 422
