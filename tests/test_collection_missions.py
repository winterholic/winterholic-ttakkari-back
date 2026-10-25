"""A mission must reach real collector jobs and KB receipts, not fabricated counts."""
import asyncio
import uuid

import pytest
from collector_support import article_html, prose
from sqlalchemy import select
from test_collector_control import BASE, source_body
from test_collector_pipeline import env  # noqa: F401

from ttakkari import kb_delivery
from ttakkari.collector import missions, runner
from ttakkari.collector_models import (
    CollectionMission,
    CollectionMissionJob,
    CollectorJob,
)
from ttakkari.db import sessionmaker
from ttakkari.kb_models import KbRevision, KbSource

pytestmark = pytest.mark.security


async def test_github_mission_preparation_is_durable_and_provenance_reaches_kb(client, auth, env, monkeypatch):  # noqa: F811
    from ttakkari.collector import adapters

    called = []
    url = env.server.url("/SKILL.md")
    env.server.page("/SKILL.md", "# Durable tools\n" + prose("en", 90, 15), "text/plain")
    body = source_body(seeds=[url], max_pages=2)
    body["policy"].update(allowed_hosts=["docs.test"], schemes=["http"], ports=[env.server.port], min_delay_seconds=0)
    assert (await client.post(BASE + "/sources", headers=auth, json=body)).status_code == 201

    async def inventory(repo, **kwargs):
        called.append(repo)
        return {"repository": "owner/skills", "revision": "d" * 40,
                "documents": [{"url": url, "path": "skills/example/SKILL.md", "revision": "d" * 40,
                               "repository": "owner/skills", "adapter": "website", "provenance": "github_api"}],
                "license": {"id": None, "permission": "unknown"},
                "coverage": {"eligible_files": 1, "selected_files": 1, "truncated": False}}
    monkeypatch.setattr(adapters, "github_repository", inventory)
    made = await client.post(BASE + "/missions", headers=auth, json={"goal": "스킬 수집", "urls": ["https://github.com/owner/skills"]})
    assert made.status_code == 202, made.text
    assert called == []  # API must only enqueue; no network or model invocation here.
    assert len(made.json()["jobs"]) == 1
    assert await runner.consume_once(limit=1, managed_only=True) == 1
    assert called == ["https://github.com/owner/skills"]
    assert await runner.consume_once(limit=1, managed_only=True) == 1
    monkeypatch.setattr(kb_delivery, "default_blob_store", lambda: env.blobs)
    assert await kb_delivery.deliver_once() == 1
    detail = (await client.get(BASE + "/missions/" + made.json()["id"], headers=auth)).json()
    assert detail["counts"]["imported"] == 1, detail
    async with sessionmaker()() as db:
        revision = await db.get(KbRevision, uuid.UUID(detail["results"][0]["revision_id"]))
        source = await db.get(KbSource, revision.source_id)
        assert source.origin_meta["mission"]["reference"]["path"] == "skills/example/SKILL.md"
        assert source.origin_meta["mission"]["reference"]["revision"] == "d" * 40
    assert (await client.get("/api/assistant/kb/search?q=Durable", headers=auth)).json()["items"]


async def test_file_scoped_policy_can_read_origin_robots_without_widening_content_access(client, auth, env):  # noqa: F811
    url = env.server.url("/licensed/document")
    env.server.page("/licensed/document", article_html("Scoped license", [prose("en", 310, 15)]))
    body = source_body(seeds=[url], max_pages=1)
    body["policy"].update(allowed_hosts=["docs.test"], schemes=["http"], ports=[env.server.port], min_delay_seconds=0,
                          allowed_path_prefixes=["/licensed/document"])
    assert (await client.post(BASE + "/sources", headers=auth, json=body)).status_code == 201
    made = (await client.post(BASE + "/missions", headers=auth, json={"goal": "파일 권리 검증", "urls": [url]})).json()
    assert await runner.consume_once(limit=1, managed_only=True) == 1
    detail = (await client.get(BASE + "/missions/" + made["id"], headers=auth)).json()
    assert detail["counts"]["acquired"] == 1, detail
    assert env.server.count("/robots.txt") == 1


async def test_cancelled_preparation_does_not_enqueue_new_acquisition(client, auth, env, monkeypatch):  # noqa: F811
    from sqlalchemy import select

    from ttakkari.collector import adapters, jobs

    url = env.server.url("/SKILL.md")
    body = source_body(seeds=[url], max_pages=1)
    body["policy"].update(allowed_hosts=["docs.test"], schemes=["http"], ports=[env.server.port])
    assert (await client.post(BASE + "/sources", headers=auth, json=body)).status_code == 201
    async def inventory(repo, **kwargs):
        async with sessionmaker()() as db:
            job = await db.scalar(select(CollectorJob).where(CollectorJob.stage == "mission_prepare"))
            await jobs.cancel(db, job.tenant_id, job.scope, job.id)
            await db.commit()
        return {"documents": [{"url": url, "adapter": "website"}], "coverage": {}}
    monkeypatch.setattr(adapters, "github_repository", inventory)
    made = (await client.post(BASE + "/missions", headers=auth,
                              json={"goal": "취소 중 추가 실행 방지", "urls": ["https://github.com/owner/skills"]})).json()
    assert await runner.consume_once(limit=1, managed_only=True) == 1
    detail = (await client.get(BASE + "/missions/" + made["id"], headers=auth)).json()
    assert len(detail["jobs"]) == 1
    assert detail["jobs"][0]["status"] == "cancelled"


async def test_topic_mission_searches_in_worker_and_acquires_discovered_sources(client, auth, env, monkeypatch):  # noqa: F811
    from ttakkari.collector import adapters

    urls = [env.server.url("/theory"), env.server.url("/practice")]
    for index, url in enumerate(urls):
        env.server.page("/" + url.rsplit("/", 1)[1], article_html("Harness research", [prose("en", 130 + index, 15)]))
    body = source_body(seeds=urls, max_pages=10)
    body["policy"].update(allowed_hosts=["docs.test"], schemes=["http"], ports=[env.server.port], min_delay_seconds=0)
    assert (await client.post(BASE + "/sources", headers=auth, json=body)).status_code == 201
    calls = []

    async def search(query, **kwargs):
        calls.append(query)
        return [{"url": url, "adapter": "website", "provenance": "web_search", "query": query} for url in urls]
    monkeypatch.setattr(adapters, "search_web", search, raising=False)
    made = await client.post(BASE + "/missions", headers=auth, json={"goal": "하네스 엔지니어링 이론과 실무 연구"})
    assert calls == []
    assert await runner.consume_once(limit=1, managed_only=True) == 1
    assert len(calls) >= 2
    assert await runner.consume_once(limit=2, managed_only=True) == 2
    monkeypatch.setattr(kb_delivery, "default_blob_store", lambda: env.blobs)
    assert await kb_delivery.deliver_once() == 2
    detail = (await client.get(BASE + "/missions/" + made.json()["id"], headers=auth)).json()
    assert detail["counts"]["imported"] == 2
    assert len(detail["plan"]["search_history"]) == len(calls)
    assert detail["plan"]["search_history"][0]["new_sources"] == 2
    assert detail["plan"]["search_history"][-1]["new_sources"] == 0


async def test_empty_research_result_never_reports_success(client, auth, env, monkeypatch):  # noqa: F811
    from ttakkari.collector import adapters

    async def empty(query, **kwargs):
        return []
    monkeypatch.setattr(adapters, "search_web", empty, raising=False)
    made = await client.post(BASE + "/missions", headers=auth, json={"goal": "자료가 없는 주제"})
    assert await runner.consume_once(limit=1, managed_only=True) == 1
    detail = (await client.get(BASE + "/missions/" + made.json()["id"], headers=auth)).json()
    assert detail["status"] != "succeeded"
    assert detail["gaps"][0]["reason"] == "no_search_candidates"


async def test_mission_batches_yield_without_spending_retry_attempts_or_losing_frontier(client, auth, env, monkeypatch):  # noqa: F811
    from ttakkari.collector import pipeline

    monkeypatch.setattr(pipeline, "MISSION_BATCH_PAGES", 1, raising=False)
    monkeypatch.setattr(pipeline, "PAGE_SIZE", 1)
    html = article_html("Document index", [prose("en", 220, 15)])
    html = html.replace("<nav><a href='/'>home</a> <a href='/about'>about</a></nav>", "")
    env.server.page("/index", html + '<a href="/child">Child</a>')
    env.server.page("/child", article_html("Child document", [prose("en", 221, 15)]))
    url = env.server.url("/index")
    body = source_body(seeds=[url], max_pages=2)
    body["policy"].update(allowed_hosts=["docs.test"], schemes=["http"], ports=[env.server.port], min_delay_seconds=0, max_depth=2)
    body["max_depth"] = 2
    assert (await client.post(BASE + "/sources", headers=auth, json=body)).status_code == 201
    made = (await client.post(BASE + "/missions", headers=auth, json={"goal": "문서 전체 수집", "urls": [url], "max_pages": 2})).json()
    assert await runner.consume_once(limit=1, managed_only=True) == 1
    async with sessionmaker()() as db:
        job = await db.get(CollectorJob, uuid.UUID(made["jobs"][0]["job_id"]))
        assert job.status == "queued"
        assert job.attempts == 0
        assert any(entry["u"].endswith("/child") for entry in job.cursor["frontier"])
    assert await runner.consume_once(limit=1, managed_only=True) == 1
    assert env.server.count("/index") == 1
    assert env.server.count("/child") == 1
    detail = (await client.get(BASE + "/missions/" + made["id"], headers=auth)).json()
    assert detail["counts"]["acquired"] == 2
    async with sessionmaker()() as db:
        job = await db.get(CollectorJob, uuid.UUID(made["jobs"][0]["job_id"]))
        assert job.status == "partial"  # Home/about remain; page budget must not claim coverage complete.
        assert job.last_error["code"] == "mission_page_budget"
        assert job.cursor["frontier"]


async def test_mission_input_reaches_kb_and_reports_real_delivery(client, auth, env, monkeypatch):  # noqa: F811
    env.server.page("/document", article_html("직렬화 설계", [prose("ko", 33, 12) + " 직렬화 트랜잭션 설계 근거를 설명한다."]))
    url = env.server.url("/document")
    body = source_body(seeds=[url], max_pages=1)
    body["policy"].update(allowed_hosts=["docs.test"], schemes=["http"], ports=[env.server.port], min_delay_seconds=0)
    assert (await client.post(BASE + "/sources", headers=auth, json=body)).status_code == 201
    made = await client.post(BASE + "/missions", headers=auth, json={"goal": "직렬화 설계 수집", "urls": [url], "context": "가설: 직렬화가 필요한 설계다."})
    assert made.status_code == 202, made.text
    mission = made.json()
    assert mission["status"] == "running"
    assert mission["plan"]["context"]["provenance"] == "user_provided"
    assert len(mission["jobs"]) == 1
    async with sessionmaker()() as db:
        job = await db.get(CollectorJob, uuid.UUID(mission["jobs"][0]["job_id"]))
        assert job.envelope["extensions"]["collector:mission"]["id"] == mission["id"]
    assert await runner.consume_once(limit=1, managed_only=True) == 1
    monkeypatch.setattr(kb_delivery, "default_blob_store", lambda: env.blobs)
    assert await kb_delivery.deliver_once() == 1
    detail = (await client.get(BASE + "/missions/" + mission["id"], headers=auth)).json()
    assert detail["counts"]["imported"] == 1
    assert detail["results"][0]["document_id"]
    async with sessionmaker()() as db:
        revision = await db.get(KbRevision, uuid.UUID(detail["results"][0]["revision_id"]))
        source = await db.get(KbSource, revision.source_id)
        assert source.origin_meta["mission"]["id"] == mission["id"]
        assert source.origin_meta["mission"]["context_provenance"] == "user_provided"
    assert detail["knowledge_processing"]["semantic"] == "unverified"
    hits = (await client.get("/api/assistant/kb/search?q=직렬화", headers=auth)).json()
    assert hits["items"]
    assert (await client.get(BASE + "/missions")).status_code == 401


async def test_unknown_rights_is_visible_without_registering_fake_permission(client, auth):
    made = await client.post(BASE + "/missions", headers=auth, json={"goal": "문서 수집", "urls": ["https://unknown.example/docs/"]})
    assert made.status_code == 202, made.text
    mission = made.json()
    assert mission["status"] == "waiting_sources"
    assert mission["counts"]["imported"] == 0
    assert mission["gaps"][0]["reason"] == "source_policy_required"
    assert mission["jobs"] == []


async def test_mission_plan_preview_does_not_enqueue_and_create_is_idempotent(client, auth):
    body = {"goal": "하네스 엔지니어링 이론과 논문을 조사해줘", "request_key": "phone-retry-1"}
    preview = await client.post(BASE + "/missions/plan", headers=auth, json=body)
    assert preview.status_code == 200, preview.text
    assert preview.json()["mode"] == "research"
    assert (await client.get(BASE + "/missions", headers=auth)).json()["items"] == []
    a = await client.post(BASE + "/missions", headers=auth, json=body)
    b = await client.post(BASE + "/missions", headers=auth, json=body)
    assert a.status_code == b.status_code == 202
    assert a.json()["id"] == b.json()["id"]
    conflict = await client.post(BASE + "/missions", headers=auth, json={**body, "goal": "다른 주제"})
    assert conflict.status_code == 409


async def test_python_docs_use_verified_official_policy_without_manual_registration(client, auth):
    made = await client.post(BASE + "/missions", headers=auth,
                             json={"goal": "Python 공식 문서를 체계적으로 수집", "urls": ["https://docs.python.org/3/"]})
    assert made.status_code == 202, made.text
    detail = made.json()
    assert detail["status"] == "running"
    assert detail["gaps"] == []
    async with sessionmaker()() as db:
        from ttakkari.collector import store
        job = await db.get(CollectorJob, uuid.UUID(detail["jobs"][0]["job_id"]))
        policy = await store.active_policy(db, job.tenant_id, job.scope, job.source_key)
        assert policy[0].license_id == "PSF-2.0"
        assert policy[0].allows("knowledge")
        assert job.envelope["extensions"]["collector:request"]["max_depth"] == 3
        assert job.envelope["extensions"]["collector:request"]["max_pages"] == 200


async def test_exported_material_does_not_wait_forever_or_count_as_kb_import(client, auth, env, monkeypatch):  # noqa: F811
    url = env.server.url("/document")
    env.server.page("/document", article_html("Stored reference", [prose("en", 45, 15)]))
    body = source_body(seeds=[url], max_pages=1)
    body["policy"].update(allowed_hosts=["docs.test"], schemes=["http"], ports=[env.server.port], min_delay_seconds=0)
    body["policy"]["permissions"] = {"store": "allowed", "export": "allowed", "knowledge": "denied"}
    assert (await client.post(BASE + "/sources", headers=auth, json=body)).status_code == 201
    made = await client.post(BASE + "/missions", headers=auth, json={"goal": "보관과 적재 구분", "urls": [url]})
    assert made.status_code == 202
    assert await runner.consume_once(limit=1, managed_only=True) == 1
    monkeypatch.setattr(kb_delivery, "default_blob_store", lambda: env.blobs)
    assert await kb_delivery.deliver_once() == 1
    detail = (await client.get(BASE + "/missions/" + made.json()["id"], headers=auth)).json()
    assert detail["status"] == "partial"
    assert detail["counts"]["pending_delivery"] == 0
    assert detail["counts"]["imported"] == 0
    assert detail["results"][0]["delivery"] == "exported"


async def test_pdf_mission_preserves_pages_and_reaches_knowledge_search(client, auth, env, monkeypatch):  # noqa: F811
    from io import BytesIO

    from pypdf import PdfWriter
    from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

    text = ("Harness engineering separates model reasoning, tools, memory, and execution context. "
            "A durable checkpoint records observed tool results before advancing the workflow. "
            "Retry semantics preserve provenance and prevent duplicate side effects. "
            "Research hypotheses remain separate from source evidence. "
            "Low cost quality filters route uncertain documents for recovery rather than deleting originals.")
    writer = PdfWriter()
    page = writer.add_blank_page(600, 800)
    font = DictionaryObject({NameObject("/Type"): NameObject("/Font"), NameObject("/Subtype"): NameObject("/Type1"), NameObject("/BaseFont"): NameObject("/Helvetica")})
    page[NameObject("/Resources")] = DictionaryObject({NameObject("/Font"): DictionaryObject({NameObject("/F1"): writer._add_object(font)})})
    stream = DecodedStreamObject()
    stream.set_data(("BT /F1 12 Tf 20 700 Td (" + text + ") Tj ET").encode())
    page[NameObject("/Contents")] = writer._add_object(stream)
    output = BytesIO()
    writer.write(output)
    env.server.page("/paper.pdf", output.getvalue(), "application/pdf")
    url = env.server.url("/paper.pdf")
    body = source_body(seeds=[url], max_pages=1)
    body["policy"].update(allowed_hosts=["docs.test"], schemes=["http"], ports=[env.server.port], min_delay_seconds=0)
    assert (await client.post(BASE + "/sources", headers=auth, json=body)).status_code == 201
    made = await client.post(BASE + "/missions", headers=auth, json={"goal": "실행 환경 논문 수집", "urls": [url]})
    assert made.status_code == 202, made.text
    assert await runner.consume_once(limit=1, managed_only=True) == 1
    monkeypatch.setattr(kb_delivery, "default_blob_store", lambda: env.blobs)
    assert await kb_delivery.deliver_once() == 1
    detail = (await client.get(BASE + "/missions/" + made.json()["id"], headers=auth)).json()
    assert detail["counts"]["imported"] == 1, detail
    hits = (await client.get("/api/assistant/kb/search?q=checkpoint", headers=auth)).json()
    assert hits["items"]
    from ttakkari.collector_models import CollectorItem
    async with sessionmaker()() as db:
        item = await db.get(CollectorItem, uuid.UUID(detail["results"][0]["id"]))
        assert item.parser_version.startswith("pypdf-")
        assert item.envelope["extensions"]["collector:pdf"]["pages"][0]["page"] == 1


async def test_mission_pause_and_resume_preserves_progress_and_continues(client, auth, env):  # noqa: F811
    url = env.server.url("/resumable")
    env.server.page("/resumable", article_html("Paused mission", [prose("en", 80, 15)]))
    body = source_body(seeds=[url], max_pages=5)
    body["policy"].update(allowed_hosts=["docs.test"], schemes=["http"], ports=[env.server.port], min_delay_seconds=0)
    assert (await client.post(BASE + "/sources", headers=auth, json=body)).status_code == 201

    made = (await client.post(BASE + "/missions", headers=auth, json={"goal": "일시정지와 재개", "urls": [url]})).json()
    mid = made["id"]

    # Pause mission
    paused = (await client.post(BASE + f"/missions/{mid}/pause", headers=auth)).json()
    assert paused["status"] == "paused"
    # Idempotent pause
    assert (await client.post(BASE + f"/missions/{mid}/pause", headers=auth)).status_code == 200

    # In paused state, runner should NOT claim this job
    assert await runner.consume_once(limit=1, managed_only=True) == 0

    # Resume mission
    resumed = (await client.post(BASE + f"/missions/{mid}/resume", headers=auth)).json()
    assert resumed["status"] == "running"
    # Idempotent resume
    assert (await client.post(BASE + f"/missions/{mid}/resume", headers=auth)).status_code == 200

    # Now runner can claim and finish the job
    assert await runner.consume_once(limit=1, managed_only=True) == 1
    detail = (await client.get(BASE + f"/missions/{mid}", headers=auth)).json()
    assert detail["counts"]["acquired"] == 1


async def test_mission_cancel_prevents_child_jobs_and_cancels_active(client, auth, env):  # noqa: F811
    url = env.server.url("/cancellable")
    env.server.page("/cancellable", article_html("Cancellable mission", [prose("en", 80, 15)]))
    body = source_body(seeds=[url], max_pages=5)
    body["policy"].update(allowed_hosts=["docs.test"], schemes=["http"], ports=[env.server.port], min_delay_seconds=0)
    assert (await client.post(BASE + "/sources", headers=auth, json=body)).status_code == 201

    made = (await client.post(BASE + "/missions", headers=auth, json={"goal": "수집 취소", "urls": [url]})).json()
    mid = made["id"]

    cancelled = (await client.post(BASE + f"/missions/{mid}/cancel", headers=auth)).json()
    assert cancelled["status"] == "cancelled"
    assert len(cancelled["jobs"]) == 1
    assert cancelled["jobs"][0]["status"] == "cancelled"

    # Idempotent cancel
    assert (await client.post(BASE + f"/missions/{mid}/cancel", headers=auth)).status_code == 200

    # Runner should not run cancelled job and no child jobs should be created
    assert await runner.consume_once(limit=1, managed_only=True) == 0
    detail = (await client.get(BASE + f"/missions/{mid}", headers=auth)).json()
    assert detail["status"] == "cancelled"
    assert len(detail["jobs"]) == 1


async def test_mission_extend_reuses_preserved_frontier_with_additional_pages(client, auth, env, monkeypatch):  # noqa: F811
    p1 = env.server.url("/ext/page1")
    p2 = env.server.url("/ext/page2")
    h1 = article_html("Page 1", [prose("en", 80, 15)]).replace("<nav><a href='/'>home</a> <a href='/about'>about</a></nav>", "")
    h2 = article_html("Page 2", [prose("en", 85, 15)]).replace("<nav><a href='/'>home</a> <a href='/about'>about</a></nav>", "")
    env.server.page("/ext/page1", h1 + f'<a href="{p2}">Page 2</a>')
    env.server.page("/ext/page2", h2)

    body = source_body(seeds=[p1], max_pages=10, source_key="ext-test-source")
    body["policy"].update(allowed_hosts=["docs.test"], schemes=["http"], ports=[env.server.port], min_delay_seconds=0, max_depth=2)
    body["max_depth"] = 2
    assert (await client.post(BASE + "/sources", headers=auth, json=body)).status_code == 201

    # Mission created with budget of 1 page -> p1 is fetched, p2 remains in frontier
    made = (await client.post(BASE + "/missions", headers=auth, json={"goal": "확장 수집", "urls": [p1], "max_pages": 1})).json()
    mid = made["id"]

    assert await runner.consume_once(limit=1, managed_only=True) == 1
    detail = (await client.get(BASE + f"/missions/{mid}", headers=auth)).json()
    assert detail["status"] == "partial"
    assert detail["counts"]["acquired"] == 1
    assert detail["counts"]["discovered"] == 2

    # Extend mission: add 5 pages
    extended = (await client.post(BASE + f"/missions/{mid}/extend", headers=auth, json={"add_pages": 5})).json()
    assert extended["status"] == "running"
    assert extended["plan"]["budget"]["pages"] == 1  # the configured per-run budget is unchanged
    assert extended["runs"][0]["page_budget"] == 6 and extended["run_seq"] == 1

    # Runner consumes child job generated from preserved frontier
    assert await runner.consume_once(limit=1, managed_only=True) == 1
    monkeypatch.setattr(kb_delivery, "default_blob_store", lambda: env.blobs)
    assert await kb_delivery.deliver_once() == 2
    detail_after = (await client.get(BASE + f"/missions/{mid}", headers=auth)).json()
    assert detail_after["counts"]["acquired"] == 2
    assert detail_after["counts"]["imported"] == 2
    assert detail_after["status"] == "succeeded"


async def test_mission_auto_split_when_exceeding_source_policy_page_limit(client, auth, env, monkeypatch):  # noqa: F811
    p1 = env.server.url("/split/page1")
    p2 = env.server.url("/split/page2")
    p3 = env.server.url("/split/page3")
    h1 = article_html("P1", [prose("en", 80, 15)]).replace("<nav><a href='/'>home</a> <a href='/about'>about</a></nav>", "")
    h2 = article_html("P2", [prose("en", 80, 15)]).replace("<nav><a href='/'>home</a> <a href='/about'>about</a></nav>", "")
    h3 = article_html("P3", [prose("en", 80, 15)]).replace("<nav><a href='/'>home</a> <a href='/about'>about</a></nav>", "")
    env.server.page("/split/page1", h1 + f'<a href="{p2}">P2</a>')
    env.server.page("/split/page2", h2 + f'<a href="{p3}">P3</a>')
    env.server.page("/split/page3", h3)

    # Policy with max_pages_per_job = 2
    body = source_body(seeds=[p1], max_pages=2, source_key="split-policy-test")
    body["policy"].update(allowed_hosts=["docs.test"], schemes=["http"], ports=[env.server.port], min_delay_seconds=0, max_pages_per_job=2, max_depth=3)
    body["max_depth"] = 3
    assert (await client.post(BASE + "/sources", headers=auth, json=body)).status_code == 201

    made = (await client.post(BASE + "/missions", headers=auth, json={"goal": "자동 분할 수집", "urls": [p1], "max_pages": 10})).json()
    mid = made["id"]

    # Job 1 runs and acquires p1, p2 (reaching max_pages_per_job=2) -> finishes as partial, auto-splits child job
    assert await runner.consume_once(limit=1, managed_only=True) == 1
    mid_detail = (await client.get(BASE + f"/missions/{mid}", headers=auth)).json()
    assert mid_detail["counts"]["acquired"] == 2
    assert len(mid_detail["jobs"]) == 2  # Job 1 (finished) and Job 2 (auto-split queued)

    # Job 2 runs and acquires p3
    assert await runner.consume_once(limit=1, managed_only=True) == 1
    monkeypatch.setattr(kb_delivery, "default_blob_store", lambda: env.blobs)
    assert await kb_delivery.deliver_once() == 3
    final_detail = (await client.get(BASE + f"/missions/{mid}", headers=auth)).json()
    assert final_detail["counts"]["acquired"] == 3
    assert final_detail["counts"]["imported"] == 3
    assert final_detail["status"] == "succeeded"


async def test_concurrent_mission_reconciliation_creates_one_frontier_child(client, auth, env):  # noqa: F811
    url = env.server.url("/split-race/page1")
    next_url = env.server.url("/split-race/page2")
    env.server.page("/split-race/page1", article_html("Race", [prose("en", 80, 15)]))
    body = source_body(seeds=[url], max_pages=2)
    body["policy"].update(allowed_hosts=["docs.test"], schemes=["http"], ports=[env.server.port], min_delay_seconds=0,
                          max_pages_per_job=2)
    assert (await client.post(BASE + "/sources", headers=auth, json=body)).status_code == 201
    made = (await client.post(BASE + "/missions", headers=auth,
                              json={"goal": "동시 분할 검증", "urls": [url], "max_pages": 5})).json()
    mid = uuid.UUID(made["id"])
    async with sessionmaker()() as db:
        job = await db.scalar(select(CollectorJob).join(CollectionMissionJob).where(CollectionMissionJob.mission_id == mid))
        job.status = "partial"
        job.cursor = {"v": 1, "processed": 2, "frontier": [{"url": next_url, "u": next_url, "d": 1}],
                      "seen": [url], "counts": {}}
        await db.commit()

    async def reconcile():
        async with sessionmaker()() as db:
            mission = await db.get(CollectionMission, mid)
            await missions.reconcile_mission(db, mission)
            await db.commit()

    await asyncio.gather(reconcile(), reconcile())
    async with sessionmaker()() as db:
        children = list(await db.scalars(select(CollectorJob).join(CollectionMissionJob).where(
            CollectionMissionJob.mission_id == mid, CollectorJob.request_key.like("split:%"))))
    assert len(children) == 1


async def test_mission_fair_allocation_reclaims_unused_pages_for_other_sources(client, auth, env, monkeypatch):  # noqa: F811
    u1 = env.server.url("/fair/short")
    u2 = env.server.url("/fair/long1")
    u3 = env.server.url("/fair/long2")
    env.server.page("/fair/short", article_html("Short", [prose("en", 80, 15)]).replace("<nav><a href='/'>home</a> <a href='/about'>about</a></nav>", ""))  # 1 page total
    h2 = article_html("L1", [prose("en", 80, 15)]).replace("<nav><a href='/'>home</a> <a href='/about'>about</a></nav>", "")
    h3 = article_html("L2", [prose("en", 80, 15)]).replace("<nav><a href='/'>home</a> <a href='/about'>about</a></nav>", "")
    env.server.page("/fair/long1", h2 + f'<a href="{u3}">L2</a>')
    env.server.page("/fair/long2", h3)

    body1 = source_body(seeds=[u1], max_pages=10, source_key="fair-source-1")
    body1["policy"].update(allowed_hosts=["docs.test"], schemes=["http"], ports=[env.server.port], min_delay_seconds=0)
    assert (await client.post(BASE + "/sources", headers=auth, json=body1)).status_code == 201

    body2 = source_body(seeds=[u2], max_pages=1, source_key="fair-source-2")
    body2["policy"].update(allowed_hosts=["docs.test"], schemes=["http"], ports=[env.server.port], min_delay_seconds=0, max_pages_per_job=1, max_depth=2)
    body2["max_depth"] = 2
    assert (await client.post(BASE + "/sources", headers=auth, json=body2)).status_code == 201

    # Mission budget = 3 pages across 2 sources (initial allocation: fair share = 3 // 2 = 1 page each)
    made = (await client.post(BASE + "/missions", headers=auth, json={"goal": "공정 할당 검증", "urls": [u1, u2], "max_pages": 3})).json()
    mid = made["id"]

    # Consume job 1 (u1: acquires 1 page, finishes succeeded)
    assert await runner.consume_once(limit=1, managed_only=True) == 1
    # Consume job 2 (u2: acquires 1 page, reaches cap 1, frontier has u3)
    assert await runner.consume_once(limit=1, managed_only=True) == 1

    # Unused budget calculation: total budget=3, u1 used 1, u2 used 1. Remaining budget = 1.
    # Reconcile automatically creates child job for u2 with remaining 1 page!
    detail_mid = (await client.get(BASE + f"/missions/{mid}", headers=auth)).json()
    assert detail_mid["counts"]["acquired"] == 2

    # Consume job 3 (u2 child: acquires u3)
    assert await runner.consume_once(limit=1, managed_only=True) == 1
    monkeypatch.setattr(kb_delivery, "default_blob_store", lambda: env.blobs)
    assert await kb_delivery.deliver_once() == 3
    final_detail = (await client.get(BASE + f"/missions/{mid}", headers=auth)).json()
    assert final_detail["counts"]["acquired"] == 3
    assert final_detail["counts"]["imported"] == 3
    assert final_detail["status"] == "succeeded"


async def test_mission_preparation_metadata_failure_is_isolated_and_recorded_as_gap(client, auth, env, monkeypatch):  # noqa: F811
    from ttakkari.collector import adapters

    url_good = env.server.url("/good.md")
    env.server.page("/good.md", "# Good Doc\n" + prose("en", 90, 15), "text/plain")
    body = source_body(seeds=[url_good], max_pages=2)
    body["policy"].update(allowed_hosts=["docs.test"], schemes=["http"], ports=[env.server.port], min_delay_seconds=0)
    assert (await client.post(BASE + "/sources", headers=auth, json=body)).status_code == 201

    async def mock_github_repo(repo, **kwargs):
        if "broken" in repo:
            raise RuntimeError("GitHub API 404: Not Found")
        return {"repository": "owner/good", "revision": "a" * 40,
                "documents": [{"url": url_good, "path": "good.md", "revision": "a" * 40,
                               "repository": "owner/good", "adapter": "website", "provenance": "github_api"}],
                "license": {"id": None, "permission": "unknown"},
                "coverage": {"eligible_files": 1, "selected_files": 1, "truncated": False}}

    monkeypatch.setattr(adapters, "github_repository", mock_github_repo)
    made = (await client.post(BASE + "/missions", headers=auth, json={
        "goal": "메타데이터 실패 격리",
        "urls": ["https://github.com/owner/broken", "https://github.com/owner/good"]
    })).json()
    mid = made["id"]

    # Run preparation
    assert await runner.consume_once(limit=1, managed_only=True) == 1
    detail = (await client.get(BASE + f"/missions/{mid}", headers=auth)).json()

    # The broken repository should be recorded as a gap, not crashing the entire mission
    gap = next((g for g in detail["gaps"] if g.get("reason") == "github_metadata_failed"), None)
    assert gap is not None
    assert "broken" in gap["url"]

    # And the good repository document was successfully enqueued and can be acquired
    assert await runner.consume_once(limit=1, managed_only=True) == 1
    after_acq = (await client.get(BASE + f"/missions/{mid}", headers=auth)).json()
    assert after_acq["counts"]["acquired"] == 1


async def test_mission_control_security_and_validation(client, auth):
    fake_id = str(uuid.uuid4())
    # Unauthenticated requests must be 401
    assert (await client.post(BASE + f"/missions/{fake_id}/pause")).status_code == 401
    assert (await client.post(BASE + f"/missions/{fake_id}/resume")).status_code == 401
    assert (await client.post(BASE + f"/missions/{fake_id}/cancel")).status_code == 401
    assert (await client.post(BASE + f"/missions/{fake_id}/extend", json={"add_pages": 10})).status_code == 401
    assert (await client.post(BASE + f"/missions/{fake_id}/rerun")).status_code == 401
    assert (await client.post(BASE + f"/missions/{fake_id}/schedule", json={"interval_minutes": 60})).status_code == 401

    # Nonexistent mission must be 404
    assert (await client.post(BASE + f"/missions/{fake_id}/pause", headers=auth)).status_code == 404
    assert (await client.post(BASE + f"/missions/{fake_id}/resume", headers=auth)).status_code == 404
    assert (await client.post(BASE + f"/missions/{fake_id}/cancel", headers=auth)).status_code == 404
    assert (await client.post(BASE + f"/missions/{fake_id}/extend", headers=auth, json={"add_pages": 10})).status_code == 404
    assert (await client.post(BASE + f"/missions/{fake_id}/rerun", headers=auth)).status_code == 404
    assert (await client.post(BASE + f"/missions/{fake_id}/schedule", headers=auth, json={"interval_minutes": 60})).status_code == 404

    # Validation: invalid add_pages
    assert (await client.post(BASE + f"/missions/{fake_id}/extend", headers=auth, json={"add_pages": 0})).status_code == 422
    assert (await client.post(BASE + f"/missions/{fake_id}/schedule", headers=auth, json={"interval_minutes": 0})).status_code == 422


async def test_mission_schedule_rejects_intervals_below_source_constraint(client, auth, env):  # noqa: F811
    url = env.server.url("/schedule-minimum")
    env.server.page("/schedule-minimum", article_html("Schedule", [prose("en", 80, 15)]))
    body = source_body(seeds=[url], max_pages=5)
    body["policy"].update(allowed_hosts=["docs.test"], schemes=["http"], ports=[env.server.port], min_delay_seconds=0)
    assert (await client.post(BASE + "/sources", headers=auth, json=body)).status_code == 201
    mission = (await client.post(BASE + "/missions", headers=auth, json={"goal": "주기 최소값", "urls": [url]})).json()

    response = await client.post(BASE + f"/missions/{mission['id']}/schedule", headers=auth,
                                 json={"interval_minutes": 5})

    assert response.status_code == 422


async def test_mission_schedule_and_rerun_controls(client, auth, env):  # noqa: F811
    url = env.server.url("/sched")
    env.server.page("/sched", article_html("Scheduled mission", [prose("en", 80, 15)]))
    body = source_body(seeds=[url], max_pages=5)
    body["policy"].update(allowed_hosts=["docs.test"], schemes=["http"], ports=[env.server.port], min_delay_seconds=0)
    assert (await client.post(BASE + "/sources", headers=auth, json=body)).status_code == 201

    made = (await client.post(BASE + "/missions", headers=auth, json={"goal": "스케줄 및 재실행", "urls": [url]})).json()
    mid = made["id"]

    # Set schedule interval to 1440 minutes
    sched = (await client.post(BASE + f"/missions/{mid}/schedule", headers=auth, json={"interval_minutes": 1440})).json()
    assert sched["plan"]["schedule"]["interval_minutes"] == 1440

    # Rerun mission
    rerun = (await client.post(BASE + f"/missions/{mid}/rerun", headers=auth)).json()
    assert rerun["status"] == "running"


# ---- per-run budget for periodic refresh -------------------------------------------------------------------

NAV = "<nav><a href='/'>home</a> <a href='/about'>about</a></nav>"


async def _site(client, auth, env, name, *, pages=2, max_pages_per_job=None):  # noqa: F811
    """A chain page1 -> page2 -> ... on the local fixture server, registered with a store-allowed policy."""
    urls = [env.server.url(f"/{name}/page{i}") for i in range(1, pages + 1)]
    for i, url in enumerate(urls):
        link = f'<a href="{urls[i + 1]}">next</a>' if i + 1 < len(urls) else ""
        env.server.page(f"/{name}/page{i + 1}", article_html(f"{name} {i}", [prose("en", 80 + i, 15)]).replace(NAV, "") + link)
    body = source_body(seeds=[urls[0]], max_pages=max_pages_per_job or 10, source_key=f"{name}-source")
    body["policy"].update(allowed_hosts=["docs.test"], schemes=["http"], ports=[env.server.port], min_delay_seconds=0,
                          max_depth=min(pages + 1, 3), **({"max_pages_per_job": max_pages_per_job} if max_pages_per_job else {}))
    body["max_depth"] = min(pages + 1, 3)
    assert (await client.post(BASE + "/sources", headers=auth, json=body)).status_code == 201
    return urls


async def _view(client, auth, mid):
    return (await client.get(BASE + f"/missions/{mid}", headers=auth)).json()


async def _tick(minutes):
    from datetime import UTC, datetime, timedelta

    from ttakkari.collector import control
    return await control.schedule_once(now=datetime.now(UTC) + timedelta(minutes=minutes))


async def test_scheduled_runs_get_a_fresh_per_run_budget_and_never_exceed_it(client, auth, env, monkeypatch):  # noqa: F811
    urls = await _site(client, auth, env, "runs", pages=2)
    made = (await client.post(BASE + "/missions", headers=auth, json={"goal": "회차 예산", "urls": [urls[0]], "max_pages": 1})).json()
    mid = made["id"]
    assert (await client.post(BASE + f"/missions/{mid}/schedule", headers=auth, json={"interval_minutes": 15})).status_code == 200
    assert await runner.consume_once(limit=1, managed_only=True) == 1
    first = await _view(client, auth, mid)
    assert first["run_seq"] == 1 and first["runs"][0]["pages_used"] == 1 and first["counts"]["acquired"] == 1

    # The old defect: the 15-minute schedule enqueued the source again without any budget check.
    assert await _tick(20) == 1
    second = await _view(client, auth, mid)
    assert second["run_seq"] == 2 and second["runs"][0]["trigger"] == "scheduled"
    assert await runner.consume_once(limit=1, managed_only=True) == 1
    second = await _view(client, auth, mid)
    run1, run2 = second["runs"][1], second["runs"][0]
    assert (run1["pages_used"], run2["pages_used"]) == (1, 1)  # each run capped at its own 1-page budget
    assert run2["page_budget"] == 1 and run2["collected"] == 1  # the run continued with what run 1 left unfetched
    assert await runner.consume_once(limit=1, managed_only=True) == 0  # nothing beyond the budget was queued
    monkeypatch.setattr(kb_delivery, "default_blob_store", lambda: env.blobs)
    assert await kb_delivery.deliver_once() == 2
    assert (await _view(client, auth, mid))["runs"][0]["imported"] == 1


async def test_refresh_run_does_not_reload_unchanged_documents_but_imports_changed_ones(client, auth, env, monkeypatch):  # noqa: F811
    urls = await _site(client, auth, env, "incr", pages=2)
    mid = (await client.post(BASE + "/missions", headers=auth, json={"goal": "증분 갱신", "urls": [urls[0]], "max_pages": 2})).json()["id"]
    await client.post(BASE + f"/missions/{mid}/schedule", headers=auth, json={"interval_minutes": 15})
    assert await runner.consume_once(limit=1, managed_only=True) == 1
    monkeypatch.setattr(kb_delivery, "default_blob_store", lambda: env.blobs)
    assert await kb_delivery.deliver_once() == 2

    assert await _tick(20) == 1
    assert await runner.consume_once(limit=1, managed_only=True) == 1
    unchanged = (await _view(client, auth, mid))["runs"][0]
    assert (unchanged["collected"], unchanged["unchanged"], unchanged["pages_used"]) == (0, 2, 2)
    assert await kb_delivery.deliver_once() == 0  # nothing was handed to the knowledge base again

    env.server.page("/incr/page2", article_html("incr changed", [prose("en", 140, 15)]).replace(NAV, ""))
    assert await _tick(40) == 1
    assert await runner.consume_once(limit=1, managed_only=True) == 1
    changed = (await _view(client, auth, mid))["runs"][0]
    assert (changed["collected"], changed["unchanged"]) == (1, 1)
    assert await kb_delivery.deliver_once() == 1
    assert (await _view(client, auth, mid))["runs"][0]["imported"] == 1


async def test_scheduled_run_is_skipped_and_recorded_while_previous_run_is_in_progress(client, auth, env):  # noqa: F811
    urls = await _site(client, auth, env, "overlap", pages=1)
    mid = (await client.post(BASE + "/missions", headers=auth, json={"goal": "중첩 금지", "urls": urls, "max_pages": 5})).json()["id"]
    await client.post(BASE + f"/missions/{mid}/schedule", headers=auth, json={"interval_minutes": 15})
    assert await _tick(20) == 0  # run 1's job is still queued
    detail = await _view(client, auth, mid)
    assert detail["run_seq"] == 1 and len(detail["jobs"]) == 1
    assert detail["plan"]["schedule"]["skipped_runs"] == 1
    assert detail["plan"]["schedule"]["last_skip"]["reason"] == "previous_run_in_progress"
    assert await runner.consume_once(limit=1, managed_only=True) == 1
    assert await _tick(40) == 1  # a skip never starves later runs
    assert (await _view(client, auth, mid))["run_seq"] == 2


async def test_paused_mission_is_not_refreshed_by_schedule(client, auth, env):  # noqa: F811
    urls = await _site(client, auth, env, "paused", pages=1)
    mid = (await client.post(BASE + "/missions", headers=auth, json={"goal": "일시정지", "urls": urls, "max_pages": 5})).json()["id"]
    await client.post(BASE + f"/missions/{mid}/schedule", headers=auth, json={"interval_minutes": 15})
    assert (await client.post(BASE + f"/missions/{mid}/pause", headers=auth)).status_code == 200
    assert await _tick(20) == 0
    assert (await _view(client, auth, mid))["run_seq"] == 1


async def test_concurrent_reconcile_extend_and_split_cannot_exceed_the_run_budget(client, auth, env):  # noqa: F811
    urls = await _site(client, auth, env, "race-run", pages=3, max_pages_per_job=2)
    made = (await client.post(BASE + "/missions", headers=auth, json={"goal": "회차 내 동시성", "urls": [urls[0]], "max_pages": 3})).json()
    mid = uuid.UUID(made["id"])
    async with sessionmaker()() as db:
        job = await db.scalar(select(CollectorJob).join(CollectionMissionJob).where(CollectionMissionJob.mission_id == mid))
        job.status = "partial"
        job.cursor = {"v": 1, "processed": 2, "frontier": [{"u": urls[2], "url": urls[2], "d": 2}], "seen": [], "counts": {}}
        await db.commit()

    async def reconcile():
        async with sessionmaker()() as db:
            await missions.reconcile_mission(db, await db.get(CollectionMission, mid))
            await db.commit()

    async def extend():
        async with sessionmaker()() as db:
            await missions.extend(db, mid, add_pages=2)
            await db.commit()

    await asyncio.gather(reconcile(), extend(), reconcile(), reconcile())
    async with sessionmaker()() as db:
        rows = (await db.execute(select(CollectorJob, CollectionMissionJob.run_seq).join(CollectionMissionJob).where(
            CollectionMissionJob.mission_id == mid))).all()
        run = await db.get(missions.CollectionMissionRun, (mid, 1))
    assert {seq for _, seq in rows} == {1}
    children = [job for job, _ in rows if job.request_key.startswith("split:")]
    assert len(children) == 1
    allocated = sum((job.cursor or {}).get("processed", 0) if job.status in missions.TERMINAL
                    else job.envelope["extensions"]["collector:request"]["max_pages"] for job, _ in rows)
    assert run.extra_pages == 2 and allocated <= run.page_budget + run.extra_pages


async def test_extend_raises_current_run_only_and_rerun_starts_a_fresh_run(client, auth, env):  # noqa: F811
    urls = await _site(client, auth, env, "rerun-run", pages=1)
    mid = (await client.post(BASE + "/missions", headers=auth, json={"goal": "회차 확장과 재실행", "urls": urls, "max_pages": 1})).json()["id"]
    extended = (await client.post(BASE + f"/missions/{mid}/extend", headers=auth, json={"add_pages": 4})).json()
    assert extended["run_seq"] == 1 and extended["runs"][0]["page_budget"] == 5
    rerun = (await client.post(BASE + f"/missions/{mid}/rerun", headers=auth)).json()
    assert rerun["run_seq"] == 2 and rerun["runs"][0]["trigger"] == "rerun"
    assert rerun["runs"][0]["page_budget"] == 1 and rerun["runs"][1]["page_budget"] == 5


async def test_mission_sources_and_jobs_cannot_bypass_the_mission_budget(client, auth, env):  # noqa: F811
    urls = await _site(client, auth, env, "bypass", pages=1)
    mid = (await client.post(BASE + "/missions", headers=auth, json={"goal": "우회 차단", "urls": urls, "max_pages": 1})).json()["id"]
    assert await runner.consume_once(limit=1, managed_only=True) == 1
    async with sessionmaker()() as db:
        from ttakkari.collector.control_models import CollectorSource
        source = await db.scalar(select(CollectorSource).where(CollectorSource.source_key.like(f"mission:{mid}:%")))
        job = await db.scalar(select(CollectorJob).join(CollectionMissionJob).where(CollectionMissionJob.mission_id == uuid.UUID(mid)))
    assert (await client.post(BASE + f"/sources/{source.id}/run", headers=auth)).status_code == 409
    assert (await client.post(BASE + f"/jobs/{job.id}/retry", headers=auth)).status_code == 409


async def test_two_stale_scheduler_ticks_do_not_start_two_runs_when_reconcile_queues_no_job(
    client, auth, env, monkeypatch,  # noqa: F811
):
    from datetime import UTC, datetime, timedelta

    from ttakkari.collector.control_models import CollectorSource
    from ttakkari.collector.policy import SourcePolicySpec
    from ttakkari.collector_models import CollectorSourcePolicy

    urls = await _site(client, auth, env, "stale-ticks", pages=1)
    mid = (await client.post(BASE + "/missions", headers=auth, json={
        "goal": "동일 시각 중복 scheduler tick", "urls": urls, "max_pages": 1,
    })).json()["id"]
    await client.post(BASE + f"/missions/{mid}/schedule", headers=auth, json={"interval_minutes": 15})
    assert await runner.consume_once(limit=1, managed_only=True) == 1

    # A revoked store grant makes reconciliation create no child, exposing whether a stale due
    # candidate is revalidated after it waits for the mission lock.
    async with sessionmaker()() as db:
        source = await db.scalar(select(CollectorSource).where(CollectorSource.source_key.like(f"mission:{mid}:%")))
        current = await db.scalar(select(CollectorSourcePolicy).where(
            CollectorSourcePolicy.source_key == source.source_key,
        ).order_by(CollectorSourcePolicy.version.desc()).limit(1))
        spec = dict(current.spec)
        spec["permissions"] = {**spec["permissions"], "store": "denied"}
        parsed = SourcePolicySpec.from_dict(spec)
        db.add(CollectorSourcePolicy(
            tenant_id=source.tenant_id, scope=source.scope, source_key=source.source_key,
            version=current.version + 1, spec=parsed.to_dict(), spec_hash=parsed.spec_hash(),
        ))
        source.next_run_at = datetime.now(UTC) - timedelta(seconds=1)
        await db.commit()

    original_start = missions.start_scheduled_run
    arrivals = 0
    both_candidates_read = asyncio.Event()

    async def hold_candidates(db, mission_id, *, now, tenant_id="owner"):
        nonlocal arrivals
        arrivals += 1
        if arrivals == 2:
            both_candidates_read.set()
        await asyncio.wait_for(both_candidates_read.wait(), timeout=3)
        return await original_start(db, mission_id, now=now, tenant_id=tenant_id)

    monkeypatch.setattr(missions, "start_scheduled_run", hold_candidates)
    now = datetime.now(UTC)

    async def reconcile():
        async with sessionmaker()() as db:
            mission = await db.get(CollectionMission, uuid.UUID(mid))
            await missions.reconcile_mission(db, mission)
            await db.commit()

    ticks = await asyncio.wait_for(asyncio.gather(
        missions.schedule_due_runs(now=now), missions.schedule_due_runs(now=now), reconcile(),
    ), timeout=5)
    async with sessionmaker()() as db:
        mission = await db.get(CollectionMission, uuid.UUID(mid))
        run_jobs = list(await db.scalars(select(CollectionMissionJob).where(
            CollectionMissionJob.mission_id == mission.id,
            CollectionMissionJob.run_seq == 2,
        )))
    assert sum(ticks[:2]) == 1
    assert mission.current_run_seq == 2
    assert run_jobs == []


async def test_scheduler_and_reconcile_race_keep_one_run_child_within_budget(client, auth, env, monkeypatch):  # noqa: F811
    from datetime import UTC, datetime, timedelta

    from ttakkari.collector.control_models import CollectorSource

    urls = await _site(client, auth, env, "scheduler-reconcile-race", pages=2, max_pages_per_job=1)
    mid = uuid.UUID((await client.post(BASE + "/missions", headers=auth, json={
        "goal": "scheduler와 reconcile 경합", "urls": [urls[0]], "max_pages": 3,
    })).json()["id"])
    await client.post(BASE + f"/missions/{mid}/schedule", headers=auth, json={"interval_minutes": 15})
    assert await runner.consume_once(limit=1, managed_only=True) == 1
    assert await runner.consume_once(limit=1, managed_only=True) == 1

    now = datetime.now(UTC)
    async with sessionmaker()() as db:
        source = await db.scalar(select(CollectorSource).where(
            CollectorSource.source_key.like(f"mission:{mid}:%")))
        source.next_run_at = now - timedelta(seconds=1)
        await db.commit()

    original_start = missions.start_scheduled_run
    tick_has_due_candidate = asyncio.Event()
    reconcile_ready = asyncio.Event()

    async def gate_start(db, mission_id, *, now, tenant_id="owner"):
        tick_has_due_candidate.set()
        await asyncio.wait_for(reconcile_ready.wait(), timeout=3)
        return await original_start(db, mission_id, now=now, tenant_id=tenant_id)

    monkeypatch.setattr(missions, "start_scheduled_run", gate_start)

    async def reconcile():
        async with sessionmaker()() as db:
            await asyncio.wait_for(tick_has_due_candidate.wait(), timeout=3)
            mission = await db.get(CollectionMission, mid)
            reconcile_ready.set()
            await missions.reconcile_mission(db, mission)
            await db.commit()

    results = await asyncio.wait_for(asyncio.gather(missions.schedule_due_runs(now=now), reconcile()), timeout=5)
    async with sessionmaker()() as db:
        mission = await db.get(CollectionMission, mid)
        rows = (await db.execute(select(CollectorJob, CollectionMissionJob.run_seq).join(
            CollectionMissionJob, CollectionMissionJob.job_id == CollectorJob.id,
        ).where(CollectionMissionJob.mission_id == mid))).all()
        run2 = await db.get(missions.CollectionMissionRun, (mid, 2))
    run2_jobs = [(job, seq) for job, seq in rows if seq == 2]
    allocated = sum((job.cursor or {}).get("processed", 0) if job.status in missions.TERMINAL else
                    job.envelope["extensions"]["collector:request"]["max_pages"] for job, _ in run2_jobs)
    assert results[0] == 1
    assert mission.current_run_seq == 2
    assert len(run2_jobs) == 1
    assert all(seq == 2 for _, seq in run2_jobs)
    assert allocated <= run2.page_budget + run2.extra_pages == 3
