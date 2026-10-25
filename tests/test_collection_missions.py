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


async def test_precise_rescue_uses_reviewed_knowledge_delivery_contract(
    client, auth, env, monkeypatch  # noqa: F811
):
    from types import SimpleNamespace

    from sqlalchemy import select

    from ttakkari.collector import pipeline, precise_eval
    from ttakkari.collector.control_models import CollectorDelivery, CollectorReview
    from ttakkari.collector_models import CollectorItem, CollectorOutbox

    env.server.page(
        "/held",
        article_html(
            "직렬화 설계",
            [prose("ko", 33, 12) + " 직렬화 트랜잭션 설계 근거를 설명한다."],
        ),
    )
    url = env.server.url("/held")
    body = source_body(seeds=[url], max_pages=1)
    body["policy"].update(
        allowed_hosts=["docs.test"],
        schemes=["http"],
        ports=[env.server.port],
        min_delay_seconds=0,
    )
    assert (
        await client.post(BASE + "/sources", headers=auth, json=body)
    ).status_code == 201
    made = await client.post(
        BASE + "/missions",
        headers=auth,
        json={"goal": "프로그래밍 언어 컴파일러 최적화", "urls": [url]},
    )
    assert made.status_code == 202, made.text
    mission = made.json()

    original_triage = pipeline.triage.evaluate_triage

    def pending_hold(**kwargs):
        outcome = original_triage(**kwargs)
        signals = dict(outcome.signals)
        signals["precise_eval"] = {
            "needed": True,
            "state": "pending",
            "reasons": ["fixture"],
        }
        return SimpleNamespace(
            action="hold", reasons=["triage:irrelevant_to_goal"], signals=signals
        )

    monkeypatch.setattr(pipeline.triage, "evaluate_triage", pending_hold)

    monkeypatch.setattr(
        precise_eval,
        "engine_status",
        lambda: {
            "status": "ready",
            "detail": "fixture",
            "model": "fixture",
            "endpoint": "http://127.0.0.1:19300",
        },
    )

    async def fixture_verdict(**kwargs):
        return {
            "topic_relevance": 0.95,
            "relevance_explanation": "fixture verdict",
            "information_depth": 0.88,
            "low_quality_signals": ["none"],
            "recommendation": "accept",
            "confidence": 0.95,
        }

    monkeypatch.setattr(precise_eval, "evaluate_document_guarded", fixture_verdict)
    monkeypatch.setattr(kb_delivery, "default_blob_store", lambda: env.blobs)
    assert await runner.consume_once(limit=1, managed_only=True) == 1
    async with sessionmaker()() as db:
        item = await db.scalar(
            select(CollectorItem).where(
                CollectorItem.job_id == uuid.UUID(mission["jobs"][0]["job_id"])
            )
        )
        assert item is not None
        assert item.signals["triage"]["precise_eval"]["state"] == "evaluated", (
            item.signals
        )
        assert item.status == "accepted", item.status
    assert await kb_delivery.deliver_once() == 1

    detail = (
        await client.get(BASE + "/missions/" + mission["id"], headers=auth)
    ).json()
    assert detail["counts"]["imported"] == 1, detail
    result = detail["results"][0]
    hits = (await client.get("/api/assistant/kb/search?q=직렬화", headers=auth)).json()[
        "items"
    ]
    assert hits
    async with sessionmaker()() as db:
        item = await db.get(CollectorItem, uuid.UUID(result["id"]))
        assert item.status == "accepted"
        assert item.decision_state == "accept"
        review = await db.scalar(
            select(CollectorReview).where(CollectorReview.item_id == item.id)
        )
        assert (
            review
            and review.decision == "accept"
            and review.text_sha256 == item.text_sha256
        )
        assert (
            "utility review only" in review.reason
            and "factuality unverified" in review.reason
        )
        outbox = await db.scalar(
            select(CollectorOutbox).where(
                CollectorOutbox.envelope_id == item.envelope_id,
                CollectorOutbox.sink == "knowledge",
            )
        )
        assert outbox and outbox.status == "delivered"
        receipt = await db.get(CollectorDelivery, outbox.id)
        assert receipt and receipt.outcome == "imported"
        assert item.envelope["decision"]["state"] == "hold"
        assert item.envelope["rights"]["permission"] == "allowed"
        assert "knowledge" in item.envelope["rights"]["purposes"]
        assert item.envelope["rights"]["retain_until"] is None


async def test_precise_rescue_keeps_item_held_when_collection_rights_deny_knowledge(
    client, auth, env, monkeypatch  # noqa: F811
):
    from types import SimpleNamespace

    from sqlalchemy import select

    from ttakkari.collector import pipeline, precise_eval
    from ttakkari.collector.control_models import CollectorReview
    from ttakkari.collector_models import CollectorItem, CollectorOutbox

    env.server.page(
        "/held-no-kb",
        article_html(
            "직렬화 설계",
            [prose("ko", 33, 12) + " 직렬화 트랜잭션 설계 근거를 설명한다."],
        ),
    )
    url = env.server.url("/held-no-kb")
    body = source_body(seeds=[url], max_pages=1)
    body["policy"].update(
        allowed_hosts=["docs.test"],
        schemes=["http"],
        ports=[env.server.port],
        min_delay_seconds=0,
    )
    body["policy"]["permissions"]["knowledge"] = "denied"
    assert (
        await client.post(BASE + "/sources", headers=auth, json=body)
    ).status_code == 201
    made = await client.post(
        BASE + "/missions",
        headers=auth,
        json={"goal": "프로그래밍 언어 컴파일러 최적화", "urls": [url]},
    )
    assert made.status_code == 202, made.text

    original_triage = pipeline.triage.evaluate_triage

    def pending_hold(**kwargs):
        outcome = original_triage(**kwargs)
        signals = dict(outcome.signals)
        signals["precise_eval"] = {
            "needed": True,
            "state": "pending",
            "reasons": ["fixture"],
        }
        return SimpleNamespace(
            action="hold", reasons=["triage:irrelevant_to_goal"], signals=signals
        )

    monkeypatch.setattr(pipeline.triage, "evaluate_triage", pending_hold)
    monkeypatch.setattr(
        precise_eval,
        "engine_status",
        lambda: {
            "status": "ready",
            "detail": "fixture",
            "model": "fixture",
            "endpoint": "http://127.0.0.1:19300",
        },
    )

    async def fixture_verdict(**kwargs):
        return {
            "topic_relevance": 0.95,
            "relevance_explanation": "fixture verdict",
            "information_depth": 0.88,
            "low_quality_signals": ["none"],
            "recommendation": "accept",
            "confidence": 0.95,
        }

    monkeypatch.setattr(precise_eval, "evaluate_document_guarded", fixture_verdict)
    assert await runner.consume_once(limit=1, managed_only=True) == 1
    async with sessionmaker()() as db:
        item = await db.scalar(
            select(CollectorItem).where(
                CollectorItem.job_id == uuid.UUID(made.json()["jobs"][0]["job_id"])
            )
        )
        assert item.status == "held"
        assert item.decision_state == "hold"
        assert item.signals["triage"]["precise_eval"]["state"] == "evaluated"
        assert "policy:knowledge_import_not_permitted" in item.reason_codes
        assert (
            await db.scalar(
                select(CollectorReview).where(CollectorReview.item_id == item.id)
            )
            is None
        )
        assert (
            await db.scalar(
                select(CollectorOutbox).where(
                    CollectorOutbox.envelope_id == item.envelope_id
                )
            )
            is None
        )


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
