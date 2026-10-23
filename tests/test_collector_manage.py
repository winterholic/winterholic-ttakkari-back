"""CP-S2-07/08/10: the management surface behind /collect, delivery retry/reconcile, runtime pause and the pilot.

Everything runs against the isolated PostgreSQL and a local fixture web server; no public network.
"""
import pytest
from collector_support import article_html, prose
from sqlalchemy import select
from test_collector_control import BASE, source_body
from test_collector_pipeline import (
    env,  # noqa: F401 -- isolated HTTP/PG fixture
)

from ttakkari import generation_service, kb_delivery
from ttakkari.collector import pilot, runner, runtime
from ttakkari.collector.control_models import CollectorSource
from ttakkari.collector_models import CollectorJob, CollectorOutbox
from ttakkari.config import get_settings
from ttakkari.db import sessionmaker


async def collect(client, auth, env, monkeypatch, *, deliver=True):  # noqa: F811
    good = article_html("PostgreSQL 격리", [prose("ko", 33, 12) + " 직렬화 트랜잭션 격리 수준을 설명한다."])
    short = article_html("짧은 글", ["짧다."])
    env.server.page("/good", good)
    env.server.page("/short", short)
    body = source_body(seeds=[env.server.url("/good"), env.server.url("/short")], max_pages=2)
    body["policy"].update(allowed_hosts=["docs.test"], schemes=["http"], ports=[env.server.port], min_delay_seconds=0)
    source = (await client.post(BASE + "/sources", headers=auth, json=body)).json()
    job = (await client.post(BASE + f"/sources/{source['id']}/run", headers=auth)).json()
    monkeypatch.setattr(kb_delivery, "default_blob_store", lambda: env.blobs)
    assert await runner.consume_once(limit=1, managed_only=True) == 1
    if deliver:
        await kb_delivery.deliver_once()
    return source, job


async def test_summary_review_queue_and_item_detail_for_one_collection(client, auth, env, monkeypatch):  # noqa: F811
    _, job = await collect(client, auth, env, monkeypatch)
    summary = (await client.get(BASE + "/summary", headers=auth)).json()
    f = summary["funnel"]
    assert f["accepted"] == 1 and f["held"] == 1 and f["delivered"] == 1 and f["fetched"] >= 2
    assert summary["runtime"]["paused"] is False and summary["budget"]["teacher_enabled"] is False
    queue = (await client.get(BASE + "/review", headers=auth)).json()
    assert queue["total"] == 1 and queue["items"][0]["url"].endswith("/short")
    item_id = queue["items"][0]["id"]
    detail = (await client.get(BASE + f"/items/{item_id}", headers=auth)).json()
    assert detail["status"] == "held" and detail["hashes"]["text_sha256"] and detail["reviews"] == []
    assert "blob_handle" not in str(detail) and "attempt_token" not in str(detail)
    accept = await client.post(BASE + f"/items/{item_id}/reviews", headers=auth,
                               json={"decision": "accept", "reason": "짧지만 정의 문장으로 유용"})
    assert accept.status_code == 201
    assert (await client.get(BASE + "/review", headers=auth)).json()["total"] == 0
    detail = (await client.get(BASE + f"/items/{item_id}", headers=auth)).json()
    assert detail["reviews"][0]["decision"] == "accept" and detail["reviews"][0]["current"] is True
    assert (await client.get(BASE + "/summary", headers=auth)).json()["funnel"]["awaiting_review"] == 0
    items = (await client.get(BASE + f"/jobs/{job['job_id']}/items", headers=auth)).json()
    assert items["total"] == 2


async def test_management_reads_are_scoped_and_require_auth(client, auth, env, monkeypatch):  # noqa: F811
    await collect(client, auth, env, monkeypatch)
    queue = (await client.get(BASE + "/review", headers=auth)).json()
    item_id = queue["items"][0]["id"]
    for path in ("/summary", "/budgets", "/review", "/deliveries", "/runtime"):
        assert (await client.get(BASE + path)).status_code == 401, path
    assert (await client.get(BASE + f"/items/{item_id}")).status_code == 401
    other = {"scope": "work"}
    assert (await client.get(BASE + f"/items/{item_id}", headers=auth, params=other)).status_code == 404
    assert (await client.get(BASE + "/review", headers=auth, params=other)).json()["total"] == 0
    work = (await client.get(BASE + "/summary", headers=auth, params=other)).json()
    assert work["funnel"]["accepted"] == 0 and work["jobs"] == {}
    assert (await client.get(BASE + "/deliveries", headers=auth, params=other)).json()["total"] == 0
    out_id = (await client.get(BASE + "/deliveries", headers=auth)).json()["items"][0]["outbox_id"]
    assert (await client.post(BASE + f"/deliveries/{out_id}/reconcile", headers=auth, params=other)).status_code == 404


async def test_budgets_report_the_absolute_ceiling_and_pages(client, auth):
    body = (await client.get(BASE + "/budgets", headers=auth)).json()
    assert body["teacher_enabled"] is False
    assert body["calls"]["limit"] == 20 and body["tokens"]["limit"] == 60_000 and body["pages"]["limit"] == 500
    assert body["timezone"] == "Asia/Seoul"


async def test_delivery_list_and_reconcile_never_overwrite_an_outcome(client, auth, env, monkeypatch):  # noqa: F811
    await collect(client, auth, env, monkeypatch)
    rows = (await client.get(BASE + "/deliveries", headers=auth)).json()
    assert rows["total"] >= 1
    done = next(r for r in rows["items"] if r["status"] == "delivered")
    assert done["outcome"] == "imported"
    rec = (await client.post(BASE + f"/deliveries/{done['outbox_id']}/reconcile", headers=auth)).json()
    assert rec["state"] == "consistent" and rec["outcome"] == "imported"
    filtered = (await client.get(BASE + "/deliveries", headers=auth, params={"status": "failed"})).json()
    assert filtered["total"] == 0
    assert (await client.get(BASE + "/deliveries", headers=auth, params={"status": "bogus"})).status_code == 422


async def test_transient_delivery_failure_retries_with_backoff_then_surfaces_and_can_be_reopened(client, auth, env, monkeypatch):  # noqa: F811
    await collect(client, auth, env, monkeypatch, deliver=False)
    calls = {"n": 0}

    class Broken:
        def get(self, handle):
            calls["n"] += 1
            raise OSError("blob store unavailable")

    monkeypatch.setattr(kb_delivery, "default_blob_store", lambda: Broken())
    assert await kb_delivery.deliver_once() == 1
    async with sessionmaker()() as db:
        out = (await db.scalars(select(CollectorOutbox).where(CollectorOutbox.sink == "export"))).one()
        assert (out.status, out.attempts) == ("pending", 1) and out.not_before is not None
        assert out.last_error.startswith("transient:")
    assert await kb_delivery.deliver_once() == 0  # backoff holds: no tight retry loop
    for _ in range(kb_delivery.MAX_ATTEMPTS - 1):
        async with sessionmaker()() as db:
            out = (await db.scalars(select(CollectorOutbox).where(CollectorOutbox.sink == "export"))).one()
            out.not_before = None
            await db.commit()
        await kb_delivery.deliver_once()
    async with sessionmaker()() as db:
        out = (await db.scalars(select(CollectorOutbox).where(CollectorOutbox.sink == "export"))).one()
        assert out.status == "failed" and out.attempts == kb_delivery.MAX_ATTEMPTS
        out_id = str(out.id)
    listed = (await client.get(BASE + "/deliveries", headers=auth, params={"status": "failed"})).json()
    assert listed["total"] == 1 and listed["items"][0]["last_error"].startswith("transient:")
    monkeypatch.setattr(kb_delivery, "default_blob_store", lambda: env.blobs)
    reopened = (await client.post(BASE + f"/deliveries/{out_id}/reconcile", headers=auth)).json()
    assert reopened["state"] == "reopened" and reopened["status"] == "pending"
    assert await kb_delivery.deliver_once() == 1
    again = (await client.post(BASE + f"/deliveries/{out_id}/reconcile", headers=auth)).json()
    assert again["state"] == "consistent"


async def test_rights_failure_is_terminal_and_not_reopened_by_reconcile(client, auth, env, monkeypatch):  # noqa: F811
    source, _ = await collect(client, auth, env, monkeypatch, deliver=False)
    revoked = dict(source["policy"])
    revoked["permissions"] = {"store": "denied", "knowledge": "denied", "export": "denied"}
    patched = await client.patch(BASE + f"/sources/{source['id']}", headers=auth, json={"policy": revoked})
    assert patched.status_code == 200, patched.text
    await kb_delivery.deliver_once()
    failed = (await client.get(BASE + "/deliveries", headers=auth, params={"status": "failed"})).json()
    assert failed["total"] == 1 and failed["items"][0]["outcome"] == "failed"
    rec = (await client.post(BASE + f"/deliveries/{failed['items'][0]['outbox_id']}/reconcile", headers=auth)).json()
    assert rec["state"] == "terminal" and rec["status"] == "failed"


async def test_pause_stops_new_claims_and_resume_restores_them(client, auth, env, monkeypatch):  # noqa: F811
    env.server.page("/good", article_html("제목", [prose("ko", 7, 12)]))
    body = source_body(seeds=[env.server.url("/good")], max_pages=1, interval_minutes=60)
    body["policy"].update(allowed_hosts=["docs.test"], schemes=["http"], ports=[env.server.port], min_delay_seconds=0)
    monkeypatch.setattr(get_settings(), "collector_pilot_enabled", False)
    assert (await client.post(BASE + "/sources", headers=auth, json=body)).status_code == 201
    paused = await client.put(BASE + "/runtime", headers=auth, json={"paused": True, "reason": "점검"})
    assert paused.status_code == 200 and (await client.get(BASE + "/runtime", headers=auth)).json()["paused"] is True
    monkeypatch.setattr(kb_delivery, "default_blob_store", lambda: env.blobs)
    await generation_service._collector_control()
    async with sessionmaker()() as db:
        assert (await db.scalars(select(CollectorJob))).all() == []  # nothing scheduled while paused
        assert (await runtime.get(db, runtime.PILOT)) is None
    await client.put(BASE + "/runtime", headers=auth, json={"paused": False})
    await generation_service._collector_control()
    async with sessionmaker()() as db:
        jobs = (await db.scalars(select(CollectorJob))).all()
        assert len(jobs) == 1 and jobs[0].status in ("succeeded", "partial")
    summary = (await client.get(BASE + "/summary", headers=auth)).json()
    assert summary["runtime"]["paused"] is False and summary["jobs"]


async def test_pilot_sources_pass_the_normal_validation_and_seed_once(client, auth, monkeypatch):
    assert len(pilot.PILOT_SOURCES) == 3
    async with sessionmaker()() as db:
        created = await pilot.seed(db)
        await db.commit()
    assert sorted(created) == sorted(s["source_key"] for s in pilot.PILOT_SOURCES)
    async with sessionmaker()() as db:
        assert await pilot.seed(db) == []  # idempotent by flag
        rows = (await db.scalars(select(CollectorSource))).all()
        assert all(r.enabled and r.interval_minutes in (1440, 10080) and r.request["max_pages"] <= 10 for r in rows)
        by = {r.source_key: r for r in rows}
        assert by["pilot:govuk-news-atom"].auto_import is False and by["pilot:kubernetes-blog-feed"].auto_import is True
        rows[0].enabled = False  # an owner pause is never undone by the pilot
        await db.commit()
    async with sessionmaker()() as db:
        await pilot.seed(db)
        await db.commit()
        assert sum(r.enabled for r in (await db.scalars(select(CollectorSource))).all()) == 2
    listed = (await client.get(BASE + "/sources", headers=auth)).json()["items"]
    for source in listed:
        perms = source["policy"]["permissions"]
        assert perms["train"] == "unknown" and perms["store"] == "allowed" and source["policy"]["policy_url"]
    manual = (await client.post(BASE + "/pilot", headers=auth)).json()
    assert manual["created"] == [] and len(manual["available"]) == 3
    work = (await client.post(BASE + "/pilot", headers=auth, params={"scope": "work"})).json()
    assert len(work["created"]) == 3  # same keys are independent per scope

pytestmark = pytest.mark.security
