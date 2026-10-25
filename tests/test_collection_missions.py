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
