"""Evidence, history, replay and scope contracts of the knowledge redesign."""

import uuid

from ttakkari.db import sessionmaker
from ttakkari.models import CtxSource

BASE = "/api/assistant/knowledge"


async def ingest(
    client,
    auth,
    *,
    key="manual:decision",
    scope="personal",
    body="따까리는 디자인 시스템을 사용한다.",
    **extra,
):
    r = await client.post(
        BASE + "/imports",
        headers=auth,
        json={"source_key": key, "scope": scope, "body": body, **extra},
    )
    assert r.status_code == 201, r.text
    return r.json()


async def first_unit(client, auth, revision):
    r = await client.get(
        BASE + "/units", params={"revision_id": revision["id"]}, headers=auth
    )
    assert r.status_code == 200, r.text
    return r.json()["items"][0]


async def assertion(client, auth, unit, **extra):
    text = unit["body"]
    r = await client.post(
        BASE + "/assertions",
        headers=auth,
        json={
            "unit_id": unit["id"],
            "start": 0,
            "end": len(text),
            "quote": text,
            "subject": "따까리",
            "predicate": "uses_design_system",
            "object": "디자인 시스템",
            **extra,
        },
    )
    assert r.status_code == 201, r.text
    return r.json()


async def review(client, auth, item, **extra):
    r = await client.post(
        BASE + f"/assertions/{item['id']}/reviews",
        headers=auth,
        json={
            "expected_version": item["version"],
            "state": "active",
            "review_level": "source_reviewed",
            "reason": "원문과 대조",
            **extra,
        },
    )
    assert r.status_code == 200, r.text
    return r.json()


async def test_revision_replay_and_history_preserve_both_exact_sources(client, auth):
    old = await ingest(client, auth)
    same = await ingest(client, auth)
    assert same["id"] == old["id"]
    new = await ingest(client, auth, body="따까리는 새 디자인 시스템을 검토한다.")
    assert new["id"] != old["id"]
    assert (await first_unit(client, auth, old))[
        "body"
    ] == "따까리는 디자인 시스템을 사용한다."
    assert (await first_unit(client, auth, new))[
        "body"
    ] == "따까리는 새 디자인 시스템을 검토한다."
    # Replaying an old revision must never resurrect it as the current one.
    await ingest(client, auth)
    current = (await client.get(BASE + "/revisions", headers=auth)).json()["items"]
    assert next(r for r in current if r["id"] == new["id"])["active"]
    assert not next(r for r in current if r["id"] == old["id"])["active"]


async def test_assertion_requires_exact_immutable_evidence_and_pending_does_not_serve_as_fact(
    client, auth
):
    rev = await ingest(client, auth)
    unit = await first_unit(client, auth, rev)
    item = await assertion(client, auth, unit)
    assert item["state"] == "candidate"
    assert item["review_level"] == "candidate"
    result = (
        await client.post(
            BASE + "/recall", headers=auth, json={"query": "따까리 디자인"}
        )
    ).json()
    assert result["assertions"] == []
    assert result["evidence"] and result["evidence"][0]["revision_id"] == rev["id"]
    bad = await client.post(
        BASE + "/assertions",
        headers=auth,
        json={
            "unit_id": unit["id"],
            "start": 0,
            "end": 3,
            "quote": "가짜",
            "subject": "따까리",
            "predicate": "uses",
            "object": "가짜",
        },
    )
    assert bad.status_code == 422


async def test_review_optimistic_version_and_append_only_history(client, auth):
    unit = await first_unit(client, auth, await ingest(client, auth))
    item = await assertion(client, auth, unit)
    accepted = await review(client, auth, item)
    assert accepted["version"] == 2
    stale = await client.post(
        BASE + f"/assertions/{item['id']}/reviews",
        headers=auth,
        json={"expected_version": 1, "state": "retracted", "reason": "stale"},
    )
    assert stale.status_code == 409
    d = (await client.get(BASE + f"/assertions/{item['id']}", headers=auth)).json()
    assert [e["version"] for e in d["history"]] == [1, 2]
    assert d["evidence"][0]["quote"] == unit["body"]


async def test_retraction_survives_reimport_and_extraction_replay(client, auth):
    unit = await first_unit(client, auth, await ingest(client, auth))
    item = await review(client, auth, await assertion(client, auth, unit))
    gone = await review(client, auth, item, state="retracted")
    replay = await assertion(client, auth, unit)
    assert replay["id"] == gone["id"] and replay["state"] == "retracted"
    result = (
        await client.post(BASE + "/recall", headers=auth, json={"query": "따까리"})
    ).json()
    assert result["assertions"] == []
    assert (await client.get(BASE + f"/assertions/{item['id']}", headers=auth)).json()[
        "history"
    ][-1]["state"] == "retracted"


async def test_historical_valid_time_is_distinct_from_recorded_time(client, auth):
    unit = await first_unit(client, auth, await ingest(client, auth))
    item = await assertion(
        client,
        auth,
        unit,
        valid_from="2025-01-01T00:00:00Z",
        valid_to="2026-01-01T00:00:00Z",
    )
    await review(client, auth, item)
    old = (
        await client.post(
            BASE + "/recall",
            headers=auth,
            json={"query": "따까리", "as_of": "2025-06-01T00:00:00Z"},
        )
    ).json()
    assert len(old["assertions"]) == 1
    current = (
        await client.post(BASE + "/recall", headers=auth, json={"query": "따까리"})
    ).json()
    assert current["assertions"] == []


async def test_scope_and_source_disable_apply_before_evidence_and_graph_retrieval(
    client, auth
):
    async with sessionmaker()() as db:
        source = CtxSource(path="/synthetic/knowledge.md", scope="work")
        db.add(source)
        await db.commit()
        sid = str(source.id)
    work = await ingest(client, auth, key="work:item", scope="work", source_id=sid)
    personal = await ingest(client, auth, key="personal:item")
    for r in (work, personal):
        await review(
            client,
            auth,
            await assertion(client, auth, await first_unit(client, auth, r)),
        )
    response = (
        await client.post(
            BASE + "/recall", headers=auth, json={"query": "따까리", "scope": "work"}
        )
    ).json()
    assert len(response["assertions"]) == 1
    assert all(x["scope"] == "work" for x in response["evidence"])
    async with sessionmaker()() as db:
        s = await db.get(CtxSource, uuid.UUID(sid))
        s.enabled = False
        await db.commit()
    disabled = (
        await client.post(
            BASE + "/recall", headers=auth, json={"query": "따까리", "scope": "work"}
        )
    ).json()
    assert disabled["assertions"] == [] and disabled["evidence"] == []


async def test_candidate_polarity_and_proposals_are_not_accepted_as_positive_facts(
    client, auth
):
    u = await first_unit(
        client,
        auth,
        await ingest(client, auth, body="따까리는 이 도구를 쓰지 않기로 했다."),
    )
    item = await assertion(client, auth, u, modality="proposal", polarity=False)
    await review(client, auth, item)
    result = (
        await client.post(BASE + "/recall", headers=auth, json={"query": "따까리"})
    ).json()
    assert result["assertions"][0]["modality"] == "proposal"
    assert result["assertions"][0]["polarity"] is False


async def test_import_identity_cannot_be_rebound_to_another_scope(client, auth):
    await ingest(client, auth)
    r = await client.post(
        BASE + "/imports",
        headers=auth,
        json={"source_key": "manual:decision", "scope": "shared", "body": "따까리"},
    )
    assert r.status_code == 409


async def test_span_bounds_and_temporal_interval_validation(client, auth):
    u = await first_unit(client, auth, await ingest(client, auth))
    for patch in (
        {"start": -1},
        {"end": 10000},
        {"valid_from": "2026-01-01T00:00:00Z", "valid_to": "2025-01-01T00:00:00Z"},
    ):
        body = {
            "unit_id": u["id"],
            "start": 0,
            "end": len(u["body"]),
            "quote": u["body"],
            "subject": "따까리",
            "predicate": "uses",
            "object": "도구",
            **patch,
        }
        r = await client.post(BASE + "/assertions", headers=auth, json=body)
        assert r.status_code == 422, r.text


async def test_knowledge_endpoints_require_user_authentication(client):
    assert (await client.get(BASE + "/status")).status_code == 401
    assert (
        await client.post(BASE + "/imports", json={"source_key": "x", "body": "x"})
    ).status_code == 401


async def test_indexer_captures_exact_source_revisions_and_removal_retires_projection(
    client, auth, make_dir
):
    from sqlalchemy import select

    from ttakkari.context.indexer import index_source
    from ttakkari.knowledge_models import KnowledgeRevision

    folder = make_dir()
    path = folder / "design.md"
    raw = "# 설계\n\n원본의 공백과 줄바꿈을 보존한다.\n"
    path.write_text(raw)
    async with sessionmaker()() as db:
        src = CtxSource(path=str(folder), scope="personal")
        db.add(src)
        await db.commit()
        await index_source(db, src)
        revisions = list(await db.scalars(select(KnowledgeRevision)))
        assert len(revisions) == 1
        r = revisions[0]
        rid = str(r.id)
    units = (
        await client.get(BASE + "/units", headers=auth, params={"revision_id": rid})
    ).json()["items"]
    assert "".join(u["body"] for u in units) == raw
    path.unlink()
    async with sessionmaker()() as db:
        await index_source(db, await db.get(CtxSource, src.id))
    result = (
        await client.post(BASE + "/recall", headers=auth, json={"query": "원본 공백"})
    ).json()
    assert result["evidence"] == []
    assert (
        await client.get(BASE + "/units", headers=auth, params={"revision_id": rid})
    ).json()["total"] == len(units)


async def test_units_and_evidence_cannot_be_rewritten_by_sql(client, auth):
    import pytest
    from sqlalchemy import text
    from sqlalchemy.exc import DBAPIError

    u = await first_unit(client, auth, await ingest(client, auth))
    await assertion(client, auth, u)
    async with sessionmaker()() as db:
        with pytest.raises(DBAPIError):
            await db.execute(
                text("UPDATE knowledge_units SET body=:v WHERE id=:id"),
                {"v": "corrupted", "id": uuid.UUID(u["id"])},
            )
        await db.rollback()
    assert (await client.get(BASE + "/units", headers=auth)).json()["items"][0][
        "body"
    ] == u["body"]


async def test_extraction_batch_accounting_replay_and_evidence_guard(client, auth):
    rev = await ingest(client, auth)
    r = await client.post(
        BASE + "/extractions",
        headers=auth,
        json={"revision_id": rev["id"], "extractor_version": "source-review-v1"},
    )
    assert r.status_code == 201, r.text
    job = r.json()
    batch = (
        await client.get(BASE + f"/extractions/{job['id']}/batch", headers=auth)
    ).json()
    assert batch["cursor"] == 0 and len(batch["units"]) == 1
    missing = await client.post(
        BASE + f"/extractions/{job['id']}/batches",
        headers=auth,
        json={"expected_cursor": 0, "results": []},
    )
    assert missing.status_code == 422
    u = batch["units"][0]
    payload = {
        "expected_cursor": 0,
        "results": [
            {
                "unit_id": u["id"],
                "outcome": "candidates",
                "claims": [
                    {
                        "start": 0,
                        "end": len(u["body"]),
                        "quote": u["body"],
                        "subject": "따까리",
                        "predicate": "uses_design_system",
                        "object": "디자인 시스템",
                    }
                ],
            }
        ],
    }
    accepted = await client.post(
        BASE + f"/extractions/{job['id']}/batches", headers=auth, json=payload
    )
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["status"] == "succeeded"
    assert accepted.json()["counts"]["accounted"] == 1
    replay = await client.post(
        BASE + f"/extractions/{job['id']}/batches", headers=auth, json=payload
    )
    assert replay.status_code == 200 and replay.json()["counts"]["candidates"] == 1
    assert (await client.get(BASE + "/assertions", headers=auth)).json()["items"][0][
        "state"
    ] == "candidate"


async def test_extraction_skips_no_domain_and_cannot_forge_confirmation(client, auth):
    rev = await ingest(
        client,
        auth,
        key="personal:career",
        body="진로를 바꾸는 방향을 생각 중이다.",
        sensitivity="sensitive",
        speaker="user",
    )
    job = (
        await client.post(
            BASE + "/extractions", headers=auth, json={"revision_id": rev["id"]}
        )
    ).json()
    batch = (
        await client.get(BASE + f"/extractions/{job['id']}/batch", headers=auth)
    ).json()
    assert batch["units"][0]["speaker"] == "user"
    assert batch["sensitivity"] == "sensitive"
    assert batch["account_usage"] == "inference_owned_by_calling_agent"
    bad = await client.post(
        BASE + f"/extractions/{job['id']}/batches",
        headers=auth,
        json={
            "expected_cursor": 0,
            "results": [
                {
                    "unit_id": batch["units"][0]["id"],
                    "outcome": "no_claim",
                    "claims": [],
                }
            ],
            "review_level": "user_confirmed",
        },
    )
    assert bad.status_code == 422


async def test_correction_links_new_evidence_and_preserves_old_history(client, auth):
    oldunit = await first_unit(client, auth, await ingest(client, auth))
    old = await review(client, auth, await assertion(client, auth, oldunit))
    newunit = await first_unit(
        client,
        auth,
        await ingest(client, auth, body="따까리는 별도 디자인 시스템을 사용한다."),
    )
    payload = {
        "expected_version": old["version"],
        "reason": "새 원문을 반영",
        "replacement": {
            "unit_id": newunit["id"],
            "start": 0,
            "end": len(newunit["body"]),
            "quote": newunit["body"],
            "subject": "따까리",
            "predicate": "uses_design_system",
            "object": "별도 디자인 시스템",
        },
    }
    changed = await client.post(
        BASE + f"/assertions/{old['id']}/corrections", headers=auth, json=payload
    )
    assert changed.status_code == 200, changed.text
    new = changed.json()["replacement"]
    assert new["supersedes_id"] == old["id"]
    assert new["review_level"] == "user_confirmed"
    historic = (
        await client.get(BASE + f"/assertions/{old['id']}", headers=auth)
    ).json()
    assert historic["state"] == "superseded"
    assert historic["history"][-1]["state"] == "superseded"
    assert historic["evidence"][0]["quote"] == oldunit["body"]


async def test_impact_preview_is_readonly_and_scoped(client, auth):
    u = await first_unit(client, auth, await ingest(client, auth))
    a = await assertion(client, auth, u)
    r = await client.get(BASE + f"/assertions/{a['id']}/impact", headers=auth)
    assert r.status_code == 200, r.text
    assert r.json()["assertions"] == [a["id"]]
    assert r.json()["evidence_count"] == 1
    assert (await client.get(BASE + f"/assertions/{a['id']}", headers=auth)).json()[
        "version"
    ] == 1


async def test_work_agent_cannot_read_personal_evidence_or_forge_user_review(
    client, auth, make_dir
):
    from ttakkari.orchestrator.runtime import issue_token

    folder = make_dir()
    ws = (
        await client.post(
            "/api/workspaces",
            headers=auth,
            json={"name": "KB scope", "root_path": str(folder)},
        )
    ).json()
    session = (
        await client.post(
            "/api/sessions",
            headers=auth,
            json={"workspace_id": ws["id"], "pool": "work"},
        )
    ).json()
    token = {"Authorization": "Bearer " + issue_token(session["id"])}
    rev = await ingest(client, auth)
    u = await first_unit(client, auth, rev)
    a = await review(client, auth, await assertion(client, auth, u))
    r = await client.post(
        "/api/agent/knowledge/recall",
        headers=token,
        json={"query": "따까리", "scope": "all"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["assertions"] == [] and r.json()["evidence"] == []
    job = await client.post(
        "/api/agent/knowledge/extractions",
        headers=token,
        json={"revision_id": rev["id"]},
    )
    assert job.status_code == 404
    review_response = await client.post(
        "/api/agent/knowledge/assertions/" + a["id"] + "/reviews",
        headers=token,
        json={
            "expected_version": 2,
            "state": "active",
            "review_level": "user_confirmed",
            "reason": "forged",
        },
    )
    assert review_response.status_code in (403, 404, 405)


async def test_deleted_bound_source_does_not_become_unrestricted(client, auth):
    from sqlalchemy import delete

    source_id = uuid.uuid4()
    async with sessionmaker()() as db:
        db.add(
            CtxSource(
                id=source_id,
                path="/tmp/bound",
                pattern="*.md",
                scope="personal",
                category="projects",
            )
        )
        await db.commit()
    r = await ingest(client, auth, source_id=str(source_id))
    u = await first_unit(client, auth, r)
    item = await review(client, auth, await assertion(client, auth, u))
    async with sessionmaker()() as db:
        await db.execute(delete(CtxSource).where(CtxSource.id == source_id))
        await db.commit()
    result = (
        await client.post(BASE + "/recall", headers=auth, json={"query": "따까리"})
    ).json()
    assert not result["assertions"] and not result["evidence"]
    assert (
        await client.get(BASE + f"/assertions/{item['id']}", headers=auth)
    ).status_code == 404


async def test_nul_source_is_rejected_before_hash_or_text_can_be_silently_changed(
    client, auth
):
    r = await client.post(
        BASE + "/imports", headers=auth, json={"source_key": "nul", "body": "a\u0000b"}
    )
    assert r.status_code == 422
    assert "unsupported_nul_in_source" in r.text


async def test_regular_agent_recall_includes_evidence_package_with_scope_enforced(
    client, auth, make_dir
):
    from ttakkari.orchestrator.runtime import issue_token

    folder = make_dir()
    ws = (
        await client.post(
            "/api/workspaces",
            headers=auth,
            json={"name": "evidence scope", "root_path": str(folder)},
        )
    ).json()
    session = (
        await client.post(
            "/api/sessions",
            headers=auth,
            json={"workspace_id": ws["id"], "pool": "work"},
        )
    ).json()
    token = {"Authorization": "Bearer " + issue_token(session["id"])}
    await ingest(client, auth, key="private-raw", body="비공개진로 검색맥락")
    await ingest(
        client, auth, key="shared-raw", body="공통자료 검색맥락", scope="shared"
    )
    r = await client.post(
        "/api/agent/recall", headers=token, json={"query": "검색맥락"}
    )
    assert r.status_code == 200, r.text
    data = r.json()
    assert "knowledge" in data
    assert [u["body"] for u in data["knowledge"]["evidence"]] == ["공통자료 검색맥락"]


async def test_raw_chatgpt_import_keeps_every_branch_and_exact_revision_identity(
    client, auth
):
    import hashlib
    import json

    from ttakkari.knowledge.chatgpt import import_shard

    raw = json.dumps(
        [
            {
                "id": "c",
                "current_node": "a",
                "mapping": {
                    "a": {
                        "parent": None,
                        "message": {
                            "author": {"role": "user"},
                            "content": {"parts": ["진로 원문"]},
                        },
                    },
                    "b": {
                        "parent": None,
                        "message": {
                            "author": {"role": "assistant"},
                            "content": {"parts": ["이전 제안"]},
                        },
                    },
                },
            }
        ],
        ensure_ascii=False,
    ).encode()
    async with sessionmaker()() as db:
        result = await import_shard(
            db,
            raw,
            source_key="chatgpt:test:file",
            locator="/fixture/conversations.json",
        )
        await db.commit()
        replay = await import_shard(
            db,
            raw,
            source_key="chatgpt:test:file",
            locator="/fixture/conversations.json",
        )
        assert (
            replay["replayed"] and replay["revision"]["id"] == result["revision"]["id"]
        )
    assert result["revision"]["sha256"] == hashlib.sha256(raw).hexdigest()
    units = (
        await client.get(
            BASE + "/units",
            headers=auth,
            params={"revision_id": result["revision"]["id"]},
        )
    ).json()["items"]
    assert len(units) == 2 and {u["speaker"] for u in units} == {"user", "assistant"}
    found = (
        await client.post(BASE + "/recall", headers=auth, json={"query": "이전 제안"})
    ).json()
    assert found["evidence"] == []
    current = (
        await client.post(BASE + "/recall", headers=auth, json={"query": "진로"})
    ).json()
    assert current["evidence"][0]["body"] == "진로 원문"


async def test_backfill_registered_keeps_exact_text_and_accounts_unavailable_source(
    client, auth, make_dir
):
    from sqlalchemy import delete, select

    from ttakkari.context.indexer import index_source
    from ttakkari.knowledge.commands import backfill_registered
    from ttakkari.knowledge_models import KnowledgeRevision

    folder = make_dir()
    path = folder / "valid.md"
    path.write_text("개인 가치관\n\n건강과 진로\n")
    async with sessionmaker()() as db:
        source = CtxSource(path=str(folder), scope="personal")
        db.add(source)
        await db.commit()
        await index_source(db, source)
        await db.execute(delete(KnowledgeRevision))
        await db.commit()
        result = await backfill_registered(db)
        assert result["captured"] == 1 and result["errors"] == []
        revision = await db.scalar(select(KnowledgeRevision))
        rid = str(revision.id)
        path.unlink()
        partial = await backfill_registered(db)
        assert partial["status"] == "partial" and len(partial["errors"]) == 1
    units = (
        await client.get(BASE + "/units", headers=auth, params={"revision_id": rid})
    ).json()["items"]
    assert "".join(u["body"] for u in units) == "개인 가치관\n\n건강과 진로\n"


async def test_retraction_is_attached_to_raw_evidence_to_prevent_relearning(
    client, auth
):
    u = await first_unit(client, auth, await ingest(client, auth))
    a = await review(client, auth, await assertion(client, auth, u))
    await review(client, auth, a, state="retracted", reason="이 해석은 잘못됨")
    result = (
        await client.post(BASE + "/recall", headers=auth, json={"query": "따까리"})
    ).json()
    assert not result["assertions"]
    assert result["evidence"][0]["assertion_annotations"][0]["state"] == "retracted"


async def test_relevant_old_claim_is_not_hidden_by_newer_unrelated_claims(client, auth):
    from ttakkari.knowledge_models import KnowledgeAssertion

    u = await first_unit(client, auth, await ingest(client, auth))
    a = await review(client, auth, await assertion(client, auth, u))
    async with sessionmaker()() as db:
        db.add_all(
            [
                KnowledgeAssertion(
                    fingerprint=f"fixture-{i}",
                    subject="별개",
                    predicate="uses",
                    object=str(i),
                    scope="personal",
                    state="active",
                    review_level="source_reviewed",
                )
                for i in range(501)
            ]
        )
        await db.commit()
    result = (
        await client.post(BASE + "/recall", headers=auth, json={"query": "따까리"})
    ).json()
    assert [v["id"] for v in result["assertions"]] == [a["id"]]


async def test_raw_recall_document_diversity_prevents_one_file_consuming_all_slots(
    client, auth
):
    await ingest(
        client, auth, key="many-units", body="설계\n\n" * 8, locator="/many.md"
    )
    await ingest(
        client,
        auth,
        key="other-document",
        body="설계 선택의 근거와 배경",
        locator="/other.md",
    )
    result = (
        await client.post(
            BASE + "/recall", headers=auth, json={"query": "설계", "k": 8}
        )
    ).json()
    assert {e["locator"] for e in result["evidence"]} == {"/many.md", "/other.md"}
    assert len(result["evidence"]) == 2


async def test_superseded_claim_cannot_be_reactivated_outside_correction_chain(
    client, auth
):
    u = await first_unit(client, auth, await ingest(client, auth))
    a = await review(client, auth, await assertion(client, auth, u))
    closed = await review(client, auth, a, state="superseded", reason="정정됨")
    result = await client.post(
        BASE + f"/assertions/{a['id']}/reviews",
        headers=auth,
        json={
            "expected_version": closed["version"],
            "state": "active",
            "review_level": "user_confirmed",
            "reason": "과거로 복귀",
        },
    )
    assert result.status_code == 409


async def test_invalid_source_identity_is_rejected_before_database_access(
    client, auth
):
    result = await client.post(
        BASE + "/imports",
        headers=auth,
        json={"source_key": "source\u0000identity", "body": "정확한 원문"},
    )
    assert result.status_code == 422
