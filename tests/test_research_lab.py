"""Actual PostgreSQL/HTTP research lifecycle; no LLM result is treated as measured."""

import uuid


async def study(client, auth):
    r = await client.post(
        "/api/research/studies",
        headers=auth,
        json={
            "title": "RAG research",
            "goal": "Improve retrieval",
            "environment": {"backend": "metal"},
            "criteria": ["recall@5", "latency_ms"],
        },
    )
    assert r.status_code == 201, r.text
    return r.json()["id"]


async def test_auth_required(client):
    assert (await client.get("/api/research/studies")).status_code == 401


async def test_methods_are_reusable_across_same_scope_research(client, auth):
    a, b = await study(client, auth), await study(client, auth)
    m = (
        await client.post(
            f"/api/research/studies/{a}/nodes",
            headers=auth,
            json={
                "kind": "method",
                "title": "Reusable novel method",
                "conditions": {"backend": ["metal"]},
                "details": {"cost_seconds": 10},
            },
        )
    ).json()
    choice = await client.post(
        f"/api/research/studies/{b}/select", headers=auth, json={}
    )
    assert choice.status_code == 200 and choice.json()["candidates"][0]["id"] == m["id"]
    exp = await client.post(
        f"/api/research/studies/{b}/experiments",
        headers=auth,
        json={
            "title": "Reuse from earlier study",
            "hypothesis": "Same conditions may help",
            "rationale": "Selected earlier method",
            "method_ids": [m["id"]],
        },
    )
    assert exp.status_code == 201, exp.text


async def test_source_claim_contradiction_and_resume(client, auth):
    sid = await study(client, auth)
    source = await client.post(
        f"/api/research/studies/{sid}/sources",
        headers=auth,
        json={
            "source_key": "paper:example:v1",
            "title": "Empirical example",
            "locator": "https://example.org/paper",
            "body": "Ordering improved recall in our experiment. Cause remains unknown.",
        },
    )
    assert source.status_code == 201, source.text
    unit = source.json()["units"][0]
    claim = await client.post(
        f"/api/research/studies/{sid}/nodes",
        headers=auth,
        json={
            "kind": "claim",
            "title": "Ordering helps",
            "summary": "Author reported an improvement",
            "conditions": {"dataset": ["example"]},
            "unit_id": unit["id"],
            "start": 0,
            "quote": "Ordering improved recall in our experiment.",
        },
    )
    assert claim.status_code == 201, claim.text
    assert claim.json()["epistemic_status"] == "source_reported"
    h = await client.post(
        f"/api/research/studies/{sid}/nodes",
        headers=auth,
        json={
            "kind": "hypothesis",
            "title": "Unknown mechanism",
            "summary": "No established causal explanation",
        },
    )
    assert h.status_code == 201
    edge = await client.post(
        f"/api/research/studies/{sid}/relations",
        headers=auth,
        json={
            "from_id": claim.json()["id"],
            "to_id": h.json()["id"],
            "predicate": "challenges_under_conditions",
            "rationale": "Separate reported effect from interpretation",
            "conditions": {"dataset": "example"},
        },
    )
    assert edge.status_code == 201, edge.text
    patch = await client.patch(
        f"/api/research/studies/{sid}",
        headers=auth,
        json={
            "expected_version": 1,
            "phase": "paused",
            "next_actions": ["Inspect cause without inventing it"],
        },
    )
    assert patch.status_code == 200, patch.text
    resume = (
        await client.get(f"/api/research/studies/{sid}/resume", headers=auth)
    ).json()
    assert resume["study"]["phase"] == "paused"
    assert resume["study"]["next_actions"] == ["Inspect cause without inventing it"]
    assert len(resume["nodes"]) == 2 and len(resume["relations"]) == 1
    assert resume["timeline"]


async def test_reject_fabricated_quote_and_measured_status(client, auth):
    sid = await study(client, auth)
    for payload in [
        {"kind": "claim", "title": "Unsupported"},
        {"kind": "observation", "title": "Fake result"},
        {"kind": "method", "title": "Fake", "epistemic_status": "locally_measured"},
    ]:
        r = await client.post(
            f"/api/research/studies/{sid}/nodes", headers=auth, json=payload
        )
        assert r.status_code == 422, r.text


async def test_retrieval_execution_persists_raw_measurements_and_fresh_resume(
    client, auth
):
    sid = await study(client, auth)
    corpus = [
        {"id": "a", "text": "qwen metal inference"},
        {"id": "b", "text": "speech whisper transcription"},
    ]
    r = await client.post(
        f"/api/research/studies/{sid}/experiments",
        headers=auth,
        json={
            "title": "Retrieval baseline",
            "hypothesis": "BM25 can improve rank",
            "rationale": "Measure before adopting",
            "adapter": "retrieval_eval",
            "config": {
                "corpus": corpus,
                "queries": [{"query": "qwen", "relevant": ["a"]}],
                "k": 1,
            },
            "environment": {"dataset": "fixture", "dataset_revision": "v1"},
            "budget": {"seconds": 10, "memory_mb": 64},
        },
    )
    assert r.status_code == 201, r.text
    eid = r.json()["id"]
    r = await client.post(
        f"/api/research/experiments/{eid}/execute", headers=auth, json={}
    )
    assert r.status_code == 200, r.text
    assert r.json()["state"] == "completed"
    assert r.json()["receipt"]["origin"] == "server_executed"
    assert r.json()["receipt"]["result"]["rows"][0]["bm25"]["recall"] == 1
    # A new HTTP request restores persisted measurements rather than process memory.
    restored = (
        await client.get(f"/api/research/studies/{sid}/resume", headers=auth)
    ).json()
    assert restored["experiments"][0]["receipt"]["input_sha256"]
    r = await client.post(
        f"/api/research/experiments/{eid}/execute", headers=auth, json={}
    )
    assert r.status_code == 409  # replay cannot silently rerun a completed experiment


async def test_optimistic_write_and_cross_study_reference(client, auth):
    a, b = await study(client, auth), await study(client, auth)
    n = (
        await client.post(
            f"/api/research/studies/{a}/nodes",
            headers=auth,
            json={"kind": "novel-extension", "title": "Future type"},
        )
    ).json()
    r = await client.post(
        f"/api/research/studies/{b}/relations",
        headers=auth,
        json={
            "from_id": n["id"],
            "to_id": str(uuid.uuid4()),
            "predicate": "related",
            "rationale": "bad",
        },
    )
    assert r.status_code in (404, 422)
    r = await client.patch(
        f"/api/research/studies/{a}",
        headers=auth,
        json={"expected_version": 10, "phase": "complete"},
    )
    assert r.status_code == 409


async def test_external_results_remain_reported_and_pause_does_not_forge_execution(
    client, auth
):
    sid = await study(client, auth)
    r = await client.post(
        f"/api/research/studies/{sid}/experiments",
        headers=auth,
        json={
            "title": "Not run",
            "hypothesis": "Possibly faster",
            "rationale": "Need other hardware",
            "adapter": "external",
            "budget": {"seconds": 10, "memory_mb": 64},
        },
    )
    eid = r.json()["id"]
    r = await client.post(
        f"/api/research/experiments/{eid}/report",
        headers=auth,
        json={
            "result": {"speed": 100},
            "interpretation": "Agent says fast",
            "next_actions": ["Independently verify"],
        },
    )
    assert r.status_code == 200, r.text
    assert r.json()["receipt"]["origin"] == "external_report"
    assert r.json()["state"] == "needs_verification"


async def test_fabricated_evidence_span_is_rejected(client, auth):
    sid = await study(client, auth)
    source = (
        await client.post(
            f"/api/research/studies/{sid}/sources",
            headers=auth,
            json={
                "source_key": "x",
                "title": "x",
                "locator": "https://example.org",
                "body": "A reported effect.",
            },
        )
    ).json()
    r = await client.post(
        f"/api/research/studies/{sid}/nodes",
        headers=auth,
        json={
            "kind": "claim",
            "title": "made up",
            "unit_id": source["units"][0]["id"],
            "start": 0,
            "quote": "A universal fact.",
        },
    )
    assert r.status_code == 422


async def test_node_history_is_immutable_in_database(client, auth):
    import pytest
    from sqlalchemy import text
    from sqlalchemy.exc import DBAPIError

    from ttakkari.db import sessionmaker

    sid = await study(client, auth)
    n = (
        await client.post(
            f"/api/research/studies/{sid}/nodes",
            headers=auth,
            json={"kind": "hypothesis", "title": "Original"},
        )
    ).json()
    async with sessionmaker()() as db:
        with pytest.raises(DBAPIError, match="immutable"):
            await db.execute(
                text("UPDATE research_nodes SET title=:title WHERE id=:id"),
                {"title": "Rewritten", "id": uuid.UUID(n["id"])},
            )
        await db.rollback()


async def test_bad_conditions_and_bad_benchmark_fail_explicitly(client, auth):
    sid = await study(client, auth)
    await client.post(
        f"/api/research/studies/{sid}/nodes",
        headers=auth,
        json={
            "kind": "method",
            "title": "Unknown numeric limits",
            "conditions": {"backend": {"min": "cuda"}},
        },
    )
    r = await client.post(
        f"/api/research/studies/{sid}/select",
        headers=auth,
        json={"environment": {"backend": "metal"}},
    )
    assert r.status_code == 200, r.text
    assert r.json()["candidates"][0]["eligibility"] == "needs_review"
    r = await client.post(
        f"/api/research/studies/{sid}/experiments",
        headers=auth,
        json={
            "title": "Bad qrels",
            "hypothesis": "x",
            "rationale": "x",
            "adapter": "retrieval_eval",
            "config": {
                "corpus": [{"id": "a", "text": "a"}],
                "queries": [{"query": "x", "relevant": ["missing"]}],
            },
        },
    )
    r = await client.post(
        f"/api/research/experiments/{r.json()['id']}/execute", headers=auth, json={}
    )
    assert r.status_code == 200 and r.json()["state"] == "failed"
    assert r.json()["receipt"]["result"] is None


async def test_agent_scope_and_new_session_resume(client, auth, make_dir):
    from ttakkari.orchestrator.runtime import issue_token

    async def agent(pool):
        w = (
            await client.post(
                "/api/workspaces",
                headers=auth,
                json={"name": "research", "root_path": str(make_dir())},
            )
        ).json()
        s = (
            await client.post(
                "/api/sessions",
                headers=auth,
                json={"workspace_id": w["id"], "pool": pool},
            )
        ).json()
        return {"Authorization": "Bearer " + issue_token(uuid.UUID(s["id"]))}

    first, second, work = (
        await agent("personal"),
        await agent("personal"),
        await agent("work"),
    )
    r = await client.post(
        "/api/agent/research",
        headers=first,
        json={
            "operation": "create",
            "data": {"title": "Resume test", "goal": "Restore across sessions"},
        },
    )
    assert r.status_code == 200, r.text
    sid = r.json()["id"]
    r = await client.post(
        "/api/agent/research",
        headers=second,
        json={"operation": "resume", "study_id": sid},
    )
    assert (
        r.status_code == 200 and r.json()["study"]["goal"] == "Restore across sessions"
    )
    assert (
        await client.post(
            "/api/agent/research",
            headers=work,
            json={"operation": "resume", "study_id": sid},
        )
    ).status_code == 200
    for body in [
        {
            "operation": "update",
            "study_id": sid,
            "data": {"expected_version": 1, "unknown": True},
        },
        {"operation": "execute"},
        {"operation": "node", "study_id": sid, "data": {}},
    ]:
        assert (
            await client.post("/api/agent/research", headers=second, json=body)
        ).status_code == 422


async def test_exact_source_whitespace_is_preserved(client, auth):
    from ttakkari.config import get_settings
    from ttakkari.kb_store import object_relpath

    sid = await study(client, auth)
    body = "  leading and trailing source whitespace\n\n"
    r = await client.post(
        f"/api/research/studies/{sid}/sources",
        headers=auth,
        json={
            "title": "Exact",
            "source_key": "exact-space",
            "locator": "https://example.org",
            "body": body,
        },
    )
    assert r.status_code == 201, r.text
    digest = r.json()["revision"]["sha256"]
    assert (
        get_settings().kb_originals_dir / object_relpath(digest)
    ).read_bytes() == body.encode("utf-8")
    assert r.json()["units"][0]["body"] == body.strip()
