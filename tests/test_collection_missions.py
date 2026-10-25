"""A mission must reach real collector jobs and KB receipts, not fabricated counts."""
import uuid

import pytest
from collector_support import article_html, prose
from test_collector_control import BASE, source_body
from test_collector_pipeline import env  # noqa: F401

from ttakkari import kb_delivery
from ttakkari.collector import runner
from ttakkari.collector_models import CollectorJob
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
    assert extended["plan"]["budget"]["pages"] == 6

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
