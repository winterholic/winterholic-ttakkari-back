"""CP-S2-09: recovery, generation switch, scope isolation, SSRF and absolute-cost counterexamples through the real
pipeline, API and KB delivery path. Local fixture server only; the default (production) NetGuard is used where SSRF
is the point. No test here spends or can spend a model call."""
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
from ttakkari.collector.fetcher import SafeFetcher
from ttakkari.collector.netguard import NetGuard
from ttakkari.collector.polite import PoliteFetcher
from ttakkari.collector_models import CollectorItem, CollectorJob, CollectorOutbox
from ttakkari.config import get_settings
from ttakkari.db import sessionmaker
from ttakkari.kb_models import KbDocument


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
    for scope in ("personal", "work"):
        source = await make_source(client, auth, env, [url], scope=scope)
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
    work_hits = (await client.get("/api/assistant/kb/search", headers=auth, params={"q": "알파벳", "scope": "work"})).json()["items"]
    assert len(work_hits) == 1 and work_hits[0]["document"]["id"] == doc_id
    got = await client.get(f"/api/assistant/kb/documents/{doc_id}", headers=auth, params={"scope": "work"})
    assert got.status_code == 200


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


async def test_absolute_limits_are_independent_of_collection_volume(client, auth, env, monkeypatch):  # noqa: F811
    monkeypatch.setattr(get_settings(), "collector_daily_page_cap", 20)
    monkeypatch.setattr(get_settings(), "collector_pilot_enabled", False)
    admitted = {}
    for volume in (3, 30):  # N sources then 10N: the admitted work is capped by the same absolute page ceiling
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
    assert admitted[3] == 3 and admitted[30] == 20  # one page per source here: the 20-page ceiling holds for 10N
    async with sessionmaker()() as db:  # and the whole collection path made zero model reservations
        assert await db.scalar(select(func.count()).select_from(CollectorBudgetReservation)) == 0

pytestmark = pytest.mark.security
