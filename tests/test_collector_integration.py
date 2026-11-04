"""CP-S2-09: recovery, generation switch, scope isolation, SSRF and absolute-cost counterexamples through the real
pipeline, API and KB delivery path. Local fixture server only; the default (production) NetGuard is used where SSRF
is the point. No test here spends or can spend a model call."""
import hashlib
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from collector_support import article_html, fake_resolver, prose
from sqlalchemy import func, select, update
from test_collector_control import BASE, source_body
from test_collector_pipeline import (
    env,  # noqa: F401 -- isolated HTTP/PG fixture
)

from ttakkari import kb_delivery
from ttakkari.collector import jobs, pipeline, runner, store
from ttakkari.collector.budget_models import CollectorBudgetReservation
from ttakkari.collector.contracts import AttemptFencedError
from ttakkari.collector.control_models import KbObservation
from ttakkari.collector.fetcher import SafeFetcher
from ttakkari.collector.netguard import NetGuard
from ttakkari.collector.polite import PoliteFetcher
from ttakkari.collector_models import CollectorItem, CollectorJob, CollectorOutbox
from ttakkari.config import get_settings
from ttakkari.db import sessionmaker
from ttakkari.kb_models import KbDocument, KbPassage, KbRevision, KbSource
from ttakkari.kb_pipeline import get_default_worker
from ttakkari.kb_pipeline.stages import DefaultParsingProcessor
from ttakkari.kb_pipeline.types import StageContext
from ttakkari.kb_pipeline_models import KbPipelineJob


class Crash(BaseException):
    """A process death: not an Exception, so the runner's handler-error path cannot absorb it."""


def many_pages(env, n):  # noqa: F811
    urls = []
    for i in range(n):
        env.server.page(f"/p{i}", article_html(f"문서 {i}", [prose("ko", 100 + i, 12) + f" 고유 표지 {i}번."]))
        urls.append(env.server.url(f"/p{i}"))
    return urls


async def make_source(client, auth, env, urls, scope="personal", key="postgres-docs", **kw):  # noqa: F811
    body = source_body(seeds=urls, max_pages=len(urls), source_key=key, **kw)
    body["policy"].update(allowed_hosts=["docs.test"], schemes=["http"], ports=[env.server.port], min_delay_seconds=0)
    r = await client.post(BASE + "/sources", headers=auth, params={"scope": scope}, json=body)
    assert r.status_code == 201, r.text
    return r.json()


async def counts():
    async with sessionmaker()() as db:
        items = (await db.scalars(select(CollectorItem).where(CollectorItem.kind == "document"))).all()
        out = await db.scalar(select(func.count()).select_from(CollectorOutbox).where(CollectorOutbox.sink == "export"))
        return len(items), len({(i.scope, i.url_key) for i in items}), out


async def test_dead_owner_mid_job_resumes_without_duplicates_and_old_commit_is_fenced(client, auth, env, monkeypatch):  # noqa: F811
    urls = many_pages(env, 8)  # two commit pages: 5 + 3
    source = await make_source(client, auth, env, urls)
    await client.post(BASE + f"/sources/{source['id']}/run", headers=auth)
    calls = {"n": 0}
    real = pipeline._finalize_page

    async def die_on_second_page(*a, **kw):
        calls["n"] += 1
        if calls["n"] == 2:
            raise Crash()
        return await real(*a, **kw)

    monkeypatch.setattr(pipeline, "_finalize_page", die_on_second_page)
    async with sessionmaker()() as db:
        stale = (await jobs.claim_batch(db, owner_generation="single-99999999", managed_only=True))[0]
        await db.commit()
    with pytest.raises(Crash):
        await pipeline.handle_job(stale, runner.JobContext(stale))
    assert (await counts())[0] == 5  # the first page is committed with its cursor and outbox, atomically
    async with sessionmaker()() as db:
        await db.execute(update(CollectorJob).values(heartbeat_at=datetime.now(UTC) - timedelta(hours=1)))
        await db.commit()
    monkeypatch.setattr(pipeline, "_finalize_page", real)
    assert await runner.consume_once(limit=1, managed_only=True) == 1  # recovers the dead owner's job, then resumes
    total, distinct, exports = await counts()
    assert (total, distinct, exports) == (8, 8, 8)  # no duplicate item or delivery after the resume
    async with sessionmaker()() as db:
        job = (await db.scalars(select(CollectorJob))).one()
        assert job.status == "succeeded" and job.attempts == 2
        with pytest.raises(AttemptFencedError):  # the dead owner waking up late cannot commit
            await jobs.commit_page(db, job.id, stale.attempt_token, cursor={"v": 1})
    assert (await counts())[0] == 8


async def test_draining_generation_claims_nothing_and_an_alive_owner_keeps_its_job(client, auth, env, monkeypatch):  # noqa: F811
    source = await make_source(client, auth, env, many_pages(env, 2))
    await client.post(BASE + f"/sources/{source['id']}/run", headers=auth)
    monkeypatch.setattr(runner.generation_module, "current", SimpleNamespace(state="draining", id=uuid.uuid4()))
    assert await runner.consume_once(limit=1, managed_only=True) == 0
    async with sessionmaker()() as db:
        assert (await db.scalars(select(CollectorJob))).one().status == "queued"
    monkeypatch.setattr(runner.generation_module, "current", None)  # single-process owner: this live pid
    async with sessionmaker()() as db:  # a live owner with a stale heartbeat is not robbed
        claimed = (await jobs.claim_batch(db, owner_generation=runner.current_owner(), managed_only=True))[0]
        await db.execute(update(CollectorJob).values(heartbeat_at=datetime.now(UTC) - timedelta(hours=1)))
        await db.commit()
    report = await runner.recover()
    assert claimed.id in report.alive and not report.recovered
    async with sessionmaker()() as db:
        assert (await db.scalars(select(CollectorJob))).one().status == "running"


async def test_same_source_key_in_two_scopes_never_mixes_documents_or_search(client, auth, env, monkeypatch):  # noqa: F811
    monkeypatch.setattr(kb_delivery, "default_blob_store", lambda: env.blobs)
    env.server.page("/shared", article_html("공유 제목", [prose("ko", 5, 12) + " 구별 표지 알파벳."]))
    url = env.server.url("/shared")
    # C2 단일 도서관: 출처는 단일 도서관에 등록되며, 동일 키 중복 등록은 409 거절된다.
    source = await make_source(client, auth, env, [url], scope="personal", key="postgres-docs")
    dup_body = source_body(seeds=[url], max_pages=1, source_key="postgres-docs")
    dup_body["policy"].update(allowed_hosts=["docs.test"], schemes=["http"], ports=[env.server.port], min_delay_seconds=0)
    dup = await client.post(BASE + "/sources", headers=auth, params={"scope": "work"}, json=dup_body)
    assert dup.status_code == 409

    for scope in ("personal", "work"):
        await client.post(BASE + f"/sources/{source['id']}/run", headers=auth, params={"scope": scope})
    for _ in range(2):
        await runner.consume_once(limit=1, managed_only=True)
    delivered = await kb_delivery.deliver_once()
    assert delivered >= 1
    async with sessionmaker()() as db:
        # C2 단일 도서관: 동일 tenant·출처·URL 은 단일 문서 계보로 통합된다.
        docs = (await db.scalars(select(KbDocument))).all()
        assert len(docs) == 1
    # 소유자는 단일 도서관에서 해당 문서를 정상 검색하고 조회할 수 있다.
    hits = (await client.get("/api/assistant/kb/search", headers=auth, params={"q": "알파벳"})).json()["items"]
    assert len(hits) == 1
    doc_id = hits[0]["document"]["id"]
    # scope=work 는 v3 의 레거시 회사 범위 조회다. 통합 문서가 personal 출처로 남아 있으면 그 범위에서는 검색도 직접 조회도 되지 않는다
    # (두 범위가 섞이지 않는다는 이 시험의 이름과 같은 뜻). 통합 도서관(scope 생략)에서는 직접 조회가 된다.
    work_hits = (await client.get("/api/assistant/kb/search", headers=auth, params={"q": "알파벳", "scope": "work"})).json()["items"]
    assert work_hits == []
    got = await client.get(f"/api/assistant/kb/documents/{doc_id}", headers=auth, params={"scope": "work"})
    assert got.status_code == 404
    unified = await client.get(f"/api/assistant/kb/documents/{doc_id}", headers=auth)
    assert unified.status_code == 200


async def test_ssrf_counterexamples_are_stopped_before_any_connection(client, auth, env, monkeypatch):  # noqa: F811
    # registration refuses metadata/loopback/private seeds, plain http on odd ports, file URLs and userinfo
    for seed in ("https://169.254.169.254/latest/meta-data/", "https://127.0.0.1/", "http://10.0.0.5/", "https://docs.test:8443/",
                 "file:///etc/passwd", "https://user:pw@docs.test/"):
        body = source_body(seeds=[seed])
        body["policy"]["allowed_hosts"] = ["docs.test"]
        assert (await client.post(BASE + "/sources", headers=auth, json=body)).status_code == 422, seed
    # An allowlisted name that resolves to a private address, and a redirect to one, through the production-default
    # guard (loopback is NOT permitted here, only the fixture server's own address is reachable by the test).
    env.server.page("/ok", article_html("정상", [prose("ko", 9, 12)]))

    async def to_private(request):
        from aiohttp import web
        raise web.HTTPFound(f"http://evil.test:{env.server.port}/secret")

    env.server.routes["/redir"] = to_private
    env.server.page("/secret", "internal metadata " * 50)
    guard = NetGuard(resolver=fake_resolver({"docs.test": ["10.1.2.3"], "evil.test": ["10.9.9.9"]}))
    fetcher = SafeFetcher(guard, user_agent="TtakkariCollector/1.0")
    monkeypatch.setattr(pipeline, "_deps", pipeline.Deps(
        PoliteFetcher(fetcher, store.DbHostState(), product_token="TtakkariCollector", clock=env.clock, sleep=env.clock.sleep),
        env.blobs, env.clock))
    source = await make_source(client, auth, env, [env.server.url("/ok"), env.server.url("/redir")])
    await client.post(BASE + f"/sources/{source['id']}/run", headers=auth)
    assert await runner.consume_once(limit=1, managed_only=True) == 1
    assert env.server.hits == []  # DNS answered private for docs.test: the connection was never made
    async with sessionmaker()() as db:
        job = (await db.scalars(select(CollectorJob))).one()
        # robots.txt could not be fetched safely, so the whole host is deferred: no item, no raw bytes, no document
        assert job.status == "retry_wait" and job.last_error["reason"] == "robots_unreachable"
        assert await db.scalar(select(func.count()).select_from(CollectorItem).where(CollectorItem.kind == "document")) == 0
        assert await db.scalar(select(func.count()).select_from(KbDocument)) == 0
    await fetcher.aclose()


async def test_collection_volume_is_not_capped_by_a_daily_page_ceiling(client, auth, env, monkeypatch):  # noqa: F811
    monkeypatch.setattr(get_settings(), "collector_pilot_enabled", False)
    admitted = {}
    for volume in (3, 30):  # N sources then 10N: every run is admitted, there is no daily page ceiling
        async with sessionmaker()() as db:
            from sqlalchemy import delete

            from ttakkari.collector.control_models import (
                CollectorDailyBudget,
                CollectorSource,
            )
            await db.execute(update(CollectorSource).values(last_job_id=None))
            await db.execute(delete(CollectorJob))
            await db.execute(delete(CollectorSource))
            await db.execute(delete(CollectorDailyBudget))
            await db.commit()
        env.server.page("/doc", article_html("문서", [prose("ko", 1, 12)]))
        ok = 0
        for i in range(volume):
            source = await make_source(client, auth, env, [env.server.url("/doc")], key=f"s{volume}-{i}")
            r = await client.post(BASE + f"/sources/{source['id']}/run", headers=auth)
            ok += r.status_code == 202
        admitted[volume] = ok
    assert admitted[3] == 3 and admitted[30] == 30
    async with sessionmaker()() as db:  # and the whole collection path made zero model reservations
        assert await db.scalar(select(func.count()).select_from(CollectorBudgetReservation)) == 0

pytestmark = pytest.mark.security


async def test_real_collector_html_delivery_keeps_heading_offsets_and_raw_provenance(client, auth, env, monkeypatch):  # noqa: F811
    """Exercise adapter -> envelope -> delivery -> revision/passages with nested HTML headings."""
    monkeypatch.setattr(kb_delivery, "default_blob_store", lambda: env.blobs)
    html = """<!doctype html><html><head><title>Structured guide</title></head><body>
    <article><h1>Structured guide</h1>
    <p>The introduction describes the system and preserves a long, exact sentence for evidence.</p>
    <h2>Network design</h2>
    <p>Service endpoints connect workloads across the cluster with explicit policy controls.</p>
    <h3>Traffic policy</h3>
    <p>Traffic policy keeps ingress routes and access restrictions together.</p></article></body></html>"""
    env.server.page("/structured", html)
    source = await make_source(client, auth, env, [env.server.url("/structured")], key="html-heading-evidence")
    await client.post(BASE + f"/sources/{source['id']}/run", headers=auth)
    assert await runner.consume_once(limit=1, managed_only=True) == 1

    worker = get_default_worker()
    was_paused = worker.claiming_paused
    worker.pause_claiming()
    try:
        assert await kb_delivery.deliver_once() == 1
        async with sessionmaker()() as db:
            revision = (await db.scalars(select(KbRevision))).one()
            source_row = await db.get(KbSource, revision.source_id)
            passages = list(await db.scalars(select(KbPassage).where(
                KbPassage.revision_id == revision.id
            ).order_by(KbPassage.ord)))
            job = (await db.scalars(select(KbPipelineJob).where(
                KbPipelineJob.revision_id == revision.id
            ))).one()
            observation = (await db.scalars(select(KbObservation).where(
                KbObservation.revision_id == revision.id
            ))).one()
            assert source_row.media_type == "text/html"
            assert revision.parser_version.endswith("parse-2")
            assert source_row.origin_meta["raw_handle"]
            assert source_row.sha256 == hashlib.sha256(html.encode()).hexdigest()
            assert source_row.origin_meta["parser_version"] == revision.parser_version
            assert source_row.origin_meta["normalized_sha256"] == revision.text_sha256
            assert source_row.origin_meta["chunker_version"]
            assert source_row.origin_meta["heading_structure"] == "dom-headings-to-markdown-v1"
            assert observation.event_time == source_row.collected_at
            assert "# Structured guide" in revision.body
            assert "## Network design" in revision.body
            assert "### Traffic policy" in revision.body
            assert passages
            assert all(revision.body[p.char_start:p.char_end] == p.body for p in passages)
            assert all(p.sha256 == hashlib.sha256(p.body.encode()).hexdigest() for p in passages)
            assert any(p.section_path == ["Structured guide", "Network design"] for p in passages)
            assert any(p.section_path == ["Structured guide", "Network design", "Traffic policy"] for p in passages)
            assert len({(p.char_start, p.char_end, p.sha256) for p in passages}) == len(passages)
            ctx = StageContext(
                job_id=job.id,
                document_id=revision.document_id,
                revision_id=revision.id,
                stage="parsing",
                attempt_token=job.attempt_token,
                session_factory=sessionmaker(),
            )
        parsed = await DefaultParsingProcessor()(ctx)
        assert parsed.status == "succeeded", parsed
        assert parsed.detail["source_media_type"] == "text/html"
        assert parsed.detail["heading_spans"] >= 3
    finally:
        if not was_paused:
            worker.resume_claiming()
