import pytest

"""Collection management, durable scheduling and KB delivery against an isolated PostgreSQL."""
import asyncio
import hashlib
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select

from ttakkari.collector_models import CollectorJob
from ttakkari.db import sessionmaker
from ttakkari.kb_models import KbDocument

BASE = "/api/assistant/collect"


def source_body(**changes):
    return {
        "source_key": "postgres-docs", "title": "PostgreSQL 공식 문서",
        "seeds": ["https://www.postgresql.org/docs/current/transaction-iso.html"],
        "policy": {
            "allowed_hosts": ["www.postgresql.org"],
            "permissions": {"store": "allowed", "knowledge": "allowed", "export": "allowed"},
            "basis": "PostgreSQL license", "license_id": "PostgreSQL",
            "policy_url": "https://www.postgresql.org/about/licence/",
            "checked_at": datetime.now(UTC).isoformat(),
        },
        "interval_minutes": None, "max_pages": 2, **changes,
    }


async def create(client, auth, **changes):
    response = await client.post(BASE + "/sources", headers=auth, json=source_body(**changes))
    assert response.status_code == 201, response.text
    return response.json()


async def test_sources_require_auth_and_scope_isolation(client, auth):
    assert (await client.get(BASE + "/sources")).status_code == 401
    source = await create(client, auth)
    rows = (await client.get(BASE + "/sources", headers=auth)).json()["items"]
    assert [r["id"] for r in rows] == [source["id"]]
    # 단일 도서관에서 소유자는 레거시 scope 파라미터와 무관하게 도서관의 출처를 조회/실행할 수 있다.
    assert [r["id"] for r in (await client.get(BASE + "/sources?scope=work", headers=auth)).json()["items"]] == [source["id"]]
    non_existent = str(uuid.uuid4())
    assert (await client.post(BASE + f"/sources/{non_existent}/run", headers=auth)).status_code == 404


async def test_policy_and_limits_are_validated_before_writing(client, auth):
    bad = source_body()
    bad["policy"]["permissions"]["store"] = "unknown"
    assert (await client.post(BASE + "/sources", headers=auth, json=bad)).status_code == 422
    for changes in ({"seeds": ["https://evil.test/"]}, {"max_pages": 501}, {"interval_minutes": 0},
                    {"source_key": "x\x00y"}, {"unexpected": True}):
        r = await client.post(BASE + "/sources", headers=auth, json=source_body(**changes))
        assert r.status_code == 422, r.text
    assert (await client.get(BASE + "/sources", headers=auth)).json()["items"] == []


async def test_run_replays_active_job_and_retry_preserves_cursor(client, auth):
    source = await create(client, auth)
    async def run():
        return await client.post(BASE + f"/sources/{source['id']}/run", headers=auth)
    a, b = await asyncio.gather(run(), run())
    assert a.status_code == b.status_code == 202, (a.text, b.text)
    assert a.json()["job_id"] == b.json()["job_id"]
    job_id = a.json()["job_id"]
    async with sessionmaker()() as db:
        row = await db.get(CollectorJob, uuid.UUID(job_id))
        assert row.envelope["extensions"]["collector:request"]["max_pages"] == 2
        row.status, row.cursor = "failed", {"v": 1, "processed": 1, "frontier": []}
        await db.commit()
    retried = await client.post(BASE + f"/jobs/{job_id}/retry", headers=auth)
    assert retried.status_code == 200, retried.text
    assert retried.json()["status"] == "queued"
    assert retried.json()["cursor"]["processed"] == 1


async def test_schedule_is_atomic_and_pause_blocks_new_runs(client, auth):
    source = await create(client, auth, interval_minutes=60)
    from ttakkari.collector.control import schedule_once
    now = datetime.now(UTC) + timedelta(minutes=1)
    await asyncio.gather(schedule_once(now=now), schedule_once(now=now))
    async with sessionmaker()() as db:
        assert await db.scalar(select(func.count()).select_from(CollectorJob)) == 1
    paused = await client.patch(BASE + f"/sources/{source['id']}", headers=auth, json={"enabled": False})
    assert paused.status_code == 200
    assert (await client.post(BASE + f"/sources/{source['id']}/run", headers=auth)).status_code == 409


async def test_manual_documents_are_scope_bound_and_detail_is_hidden(client, auth):
    # C2 단일 도서관: 레거시 scope="work" 입력이 들어와도 단일 도서관에 적재되어 소유자에게 기본 검색/조회된다.
    body = {"title": "회사 전용", "text": "프로젝트 비밀 검색어", "scope": "work"}
    made = await client.post("/api/assistant/kb/documents", headers=auth, json=body)
    assert made.status_code == 201, made.text
    did = made.json()["document_id"]
    assert (await client.get("/api/assistant/kb/documents", headers=auth)).json()["total"] == 1
    assert (await client.get("/api/assistant/kb/documents?scope=work", headers=auth)).json()["total"] == 1
    assert (await client.get(f"/api/assistant/kb/documents/{did}", headers=auth)).status_code == 200
    assert (await client.get(f"/api/assistant/kb/documents/{did}?scope=work", headers=auth)).status_code == 200
    assert len((await client.get("/api/assistant/kb/search?q=비밀", headers=auth)).json()["items"]) == 1


async def test_delivery_preserves_raw_and_nfc_text_with_idempotent_receipt(client, auth):
    source = await create(client, auth)
    result = await client.post(BASE + f"/sources/{source['id']}/run", headers=auth)
    from ttakkari import kb_delivery as delivery
    from ttakkari.collector.blobs import default_blob_store
    from ttakkari.collector.contracts import compute_payload_hash
    from ttakkari.collector_models import CollectorItem, CollectorOutbox
    raw = b"<html><body>Original raw HTML</body></html>"
    handle = default_blob_store().put(raw)
    async with sessionmaker()() as db:
        job = await db.get(CollectorJob, uuid.UUID(result.json()["job_id"]))
        env = dict(job.envelope)
        env["envelope_id"] = str(uuid.uuid4())
        env["decision"] = {"state": "accept", "reason_codes": ["test"], "policy_version": "test"}
        env["raw"].update(sha256=hashlib.sha256(raw).hexdigest(), byte_size=len(raw), blob_handle=handle)
        body = "가나다 ① 원문에서 추출한 본문입니다."
        env["normalized"] = {"text": body, "sha256": hashlib.sha256(body.encode()).hexdigest(),
                             "language": "ko", "parser_version": "test-v1", "normalizer_version": "nfc-v1",
                             "chunker_version": "paragraph-v1", "raw_alignment_handle": None, "truncated": False}
        item = CollectorItem(job_id=job.id, tenant_id="owner", scope="personal", source_key=job.source_key,
                             status="accepted", stage="decision", url=source["seeds"][0], url_key="a" * 64,
                             envelope_id=uuid.UUID(env["envelope_id"]), envelope=env,
                             payload_hash=compute_payload_hash(env))
        db.add(item)
        await db.flush()
        out = CollectorOutbox(job_id=job.id, tenant_id="owner", scope="personal", sink="export",
                              envelope_id=item.envelope_id, payload_hash=item.payload_hash)
        db.add(out)
        await db.commit()
    assert await delivery.deliver_once() == 1
    assert await delivery.deliver_once() == 0
    documents = (await client.get("/api/assistant/kb/documents", headers=auth)).json()["items"]
    assert len(documents) == 1
    detail = (await client.get("/api/assistant/kb/documents/" + documents[0]["id"], headers=auth)).json()
    assert detail["passages"]["items"][0]["body"] == body  # NFC must preserve compatibility character ①
    assert detail["revisions"][0]["source"]["sha256"] == hashlib.sha256(raw).hexdigest()
    assert detail["observations"][0]["envelope_id"] == env["envelope_id"]


async def test_knowledge_permission_revocation_prevents_delivery(client, auth):
    source = await create(client, auth)
    from ttakkari import kb_delivery as delivery
    # No accepted documents: intake receipts are never interpreted as collected documents.
    await client.post(BASE + f"/sources/{source['id']}/run", headers=auth)
    assert await delivery.deliver_once() == 0
    async with sessionmaker()() as db:
        assert await db.scalar(select(func.count()).select_from(KbDocument)) == 0


async def test_source_update_uses_version_precondition(client, auth):
    source = await create(client, auth)
    a = await client.patch(BASE + f"/sources/{source['id']}", headers=auth,
                           json={"enabled": False, "expected_version": source["version"]})
    assert a.status_code == 200
    b = await client.patch(BASE + f"/sources/{source['id']}", headers=auth,
                           json={"enabled": True, "expected_version": source["version"]})
    assert b.status_code == 409

pytestmark = pytest.mark.security
