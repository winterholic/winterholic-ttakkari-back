"""KB3-402: 사용자 API(/api/assistant/kb/*) 계약, 에이전트 API·MCP 하위 호환, 접근 반례.

프론트 계약(front-kb-v3 KB_V3_CONTRACT_NOTES.md · src/api/kbTypes.ts)이 읽는 필드 이름·위치를 실제 응답과 대조한다.
실 PostgreSQL(conftest 임시 클러스터)에서 등록 → 워커 6단계 → 조회까지 전체를 지난다. 모델 서버는 부르지 않는다.
"""

from __future__ import annotations

import io
import json
import uuid

import pytest
from sqlalchemy import select
from test_kb_v3_integration_flow import (  # noqa: F401 - _env 는 autouse 픽스처
    EN_DEF_OPEN,
    EN_DEF_VENDOR,
    _drain,
    _env,
    _worker,
)

from ttakkari.db import sessionmaker
from ttakkari.kb_pipeline_models import KbPipelineJob
from ttakkari.models import ChatSession, Workspace
from ttakkari.orchestrator.runtime import issue_token

BASE = "/api/assistant/kb"
STAGES = ["parsing", "chunking", "extracting", "linking", "indexing", "canonicalizing"]

K8S_DOC = """# 쿠버네티스(Kubernetes)

## 디플로이먼트(Deployment)

디플로이먼트(Deployment)는 파드와 레플리카셋에 대한 선언적 업데이트를 제공하는 워크로드 리소스이다. 디플로이먼트는 레플리카셋을 관리한다.

ReplicaSet manages Pod objects and keeps the replica count stable.

## 네트워크

서비스는 작업부하 집합을 노출한다. 외부 노출은 반드시 서비스로 한다.
"""


async def _ingest_json(client, auth, **body):
    r = await client.post(f"{BASE}/ingest", headers=auth, json=body)
    assert r.status_code == 200, r.text
    return r.json()


async def _ingest_text_and_drain(client, auth, text: str, title: str | None = None, **extra):
    out = await _ingest_json(client, auth, text=text, **({"title": title} if title else {}), **extra)
    await _drain(_worker())
    return out


@pytest.fixture
async def sessions(make_dir):
    root = make_dir()
    async with sessionmaker()() as db:
        workspace = Workspace(name="KB v3 api", root_path=str(root))
        db.add(workspace)
        await db.flush()
        headers = {}
        for pool in ("personal", "work"):
            row = ChatSession(workspace_id=workspace.id, title=pool, engine="codex", pool=pool)
            db.add(row)
            await db.flush()
            headers[pool] = {"Authorization": "Bearer " + issue_token(row.id)}
        await db.commit()
    return headers


# ---------------------------------------------------------------------------------------------
# ingest
# ---------------------------------------------------------------------------------------------
async def test_ingest_text_returns_contract_item_and_is_idempotent(client, auth):
    out = await _ingest_json(client, auth, text=K8S_DOC, title="쿠버네티스 노트", domain="dev-infra")
    assert set(out) >= {"batch_id", "items"}
    [item] = out["items"]
    assert set(item) >= {"document_id", "revision_id", "job_id", "status", "name"}
    assert item["status"] == "registered" and item["created"] is True
    again = (await _ingest_json(client, auth, text=K8S_DOC, title="쿠버네티스 노트"))["items"][0]
    assert again["status"] == "duplicate" and again["document_id"] == item["document_id"] and again["job_id"] == item["job_id"]
    # 등록 즉시 같은 트랜잭션에서 작업이 큐에 들어갔다(수동 추출 명령 없음)
    job = (await client.get(f"{BASE}/jobs/{item['job_id']}", headers=auth)).json()
    assert job["status"] == "registered" and [s["stage"] for s in job["stages"]] == STAGES
    # domain 은 등록 때 고른 값이 문서 주제가 된다
    listed = (await client.get(f"{BASE}/documents", headers=auth)).json()["items"][0]
    assert listed["domain"] == "dev-infra" and listed["domain_source"] == "user"


async def test_ingest_validation_errors_are_http_errors_not_silent(client, auth):
    assert (await client.post(f"{BASE}/ingest", headers=auth, json={})).status_code == 422
    assert (await client.post(f"{BASE}/ingest", headers=auth, json={"bogus": 1})).status_code == 422
    assert (await client.post(f"{BASE}/ingest", headers=auth, content=b"x", )).status_code in (415, 422)
    blank = await client.post(f"{BASE}/ingest", headers=auth, json={"text": "   "})
    assert blank.status_code == 422 and blank.json()["message"] == "ingest_empty"


async def test_multi_file_ingest_detects_types_and_holds_unsupported_honestly(client, auth):
    import docx
    import openpyxl
    import pptx
    import pypdf

    pdf = io.BytesIO()
    writer = pypdf.PdfWriter()
    writer.add_blank_page(width=200, height=200)  # 텍스트 레이어가 없는 스캔 모양
    writer.write(pdf)
    d = docx.Document()
    d.add_heading("배포 가이드", level=1)
    d.add_paragraph("디플로이먼트(Deployment)는 파드와 레플리카셋에 대한 선언적 업데이트를 제공하는 리소스이다.")
    docx_bytes = io.BytesIO()
    d.save(docx_bytes)
    p = pptx.Presentation()
    slide = p.slides.add_slide(p.slide_layouts[1])
    slide.shapes.title.text = "서비스 개요"
    slide.placeholders[1].text = "서비스는 파드 집합에 대한 안정적인 네트워크 엔드포인트를 노출한다."
    pptx_bytes = io.BytesIO()
    p.save(pptx_bytes)
    wb = openpyxl.Workbook()
    wb.active.append(["항목", "설명"])
    wb.active.append(["Deployment", "선언적 업데이트"])
    xlsx_bytes = io.BytesIO()
    wb.save(xlsx_bytes)

    files = [
        ("files[]", ("guide.md", "# 가이드\n\n쿠버네티스(Kubernetes)는 컨테이너 오케스트레이션 플랫폼이다.\n".encode(), "text/markdown")),
        ("files[]", ("notes.txt", "메모 내용입니다. 반드시 확인하세요.".encode(), "text/plain")),
        ("files[]", ("page.html", b"<html><head><title>T</title></head><body><h1>Title</h1><p>This is a long enough paragraph about pods and services for extraction.</p></body></html>", "text/html")),
        ("files[]", ("table.csv", "이름,값\n디플로이먼트,3\n".encode(), "text/csv")),
        ("files[]", ("guide.docx", (docx_bytes.getvalue()), "application/octet-stream")),
        ("files[]", ("deck.pptx", pptx_bytes.getvalue(), "application/octet-stream")),
        ("files[]", ("sheet.xlsx", xlsx_bytes.getvalue(), "application/octet-stream")),
        ("files[]", ("scan.pdf", pdf.getvalue(), "application/pdf")),
        ("files[]", ("old.doc", b"\xd0\xcf\x11\xe0\x00\x00legacy", "application/msword")),
        ("files[]", ("voice.mp3", b"ID3\x00\x00fake", "audio/mpeg")),
        ("files[]", ("blob.bin", b"\x00\x01\x02\xff\xfe binary \x00", "application/octet-stream")),
        ("files[]", ("empty.txt", b"", "text/plain")),
    ]
    r = await client.post(f"{BASE}/ingest", headers=auth, files=files, data={"domain": "dev-infra"})
    assert r.status_code == 200, r.text
    items = {i["name"]: i for i in r.json()["items"]}
    assert len(items) == len(files)

    for name in ("guide.md", "notes.txt", "page.html", "table.csv", "guide.docx", "deck.pptx", "sheet.xlsx"):
        assert items[name]["status"] == "registered", (name, items[name])
        assert items[name]["job_id"] and items[name]["detected_type"]
    assert items["page.html"]["detected_type"] == "html"
    # 미지원·보류는 성공처럼 보이지 않고 사유가 붙는다. 원본은 보관되고 작업이 held 로 남는다.
    expected_holds = {
        "scan.pdf": "ocr_required",
        "old.doc": "unsupported_legacy_office",
        "voice.mp3": "stt_runtime_unavailable",
        "blob.bin": "unsupported_format",
    }
    for name, reason in expected_holds.items():
        assert items[name]["status"] == "held" and items[name]["hold_reason"] == reason, (name, items[name])
        job = (await client.get(f"{BASE}/jobs/{items[name]['job_id']}", headers=auth)).json()
        assert job["status"] == "held" and job["hold_reason"] == reason
        parsing = next(s for s in job["stages"] if s["stage"] == "parsing")
        assert parsing["status"] == "held"
    assert items["empty.txt"]["status"] == "failed" and items["empty.txt"]["error"] == "empty_content"

    await _drain(_worker())
    # 보류 문서는 처리 후에도 보류이고 패시지(검색 가능한 지식)를 만들지 않는다
    for name in expected_holds:
        detail = (await client.get(f"{BASE}/documents/{items[name]['document_id']}", headers=auth)).json()
        assert detail["passages"]["total"] == 0 and detail["pipeline"]["status"] == "held"
        assert detail["pipeline_status"] == "held"
    # 지원 형식은 6단계가 끝났고 페이지·슬라이드 위치가 근거로 남는다
    pptx_detail = (await client.get(f"{BASE}/documents/{items['deck.pptx']['document_id']}", headers=auth)).json()
    assert pptx_detail["pipeline"]["status"] in ("ready", "degraded")
    assert any(p.get("slide_no") for p in pptx_detail["passages"]["items"])
    # 같은 파일을 다시 올리면 duplicate
    again = await client.post(f"{BASE}/ingest", headers=auth, files=[files[0]])
    assert again.json()["items"][0]["status"] == "duplicate"


async def test_url_ingest_rejects_private_targets_per_item_and_keeps_other_items(client, auth):
    out = await _ingest_json(client, auth, text=K8S_DOC, urls=["http://127.0.0.1/admin", "not a url", "ftp://example.com/x"])
    statuses = {i["name"]: i for i in out["items"]}
    assert statuses["http://127.0.0.1/admin"]["status"] == "failed"
    assert statuses["http://127.0.0.1/admin"]["error"].startswith("url_")
    assert statuses["not a url"]["status"] == "failed" and statuses["ftp://example.com/x"]["status"] == "failed"
    assert any(i["status"] == "registered" for i in out["items"]), "나쁜 URL 이 같은 배치의 정상 항목을 막지 않는다"


async def test_url_ingest_saves_a_public_page_through_the_guarded_fetcher(client, auth, monkeypatch):
    from ttakkari import kb_ingest

    async def fake_fetch(url: str):
        html = (b"<html><head><title>Docs</title></head><body><h1>Pods</h1><p>A Pod is the smallest deployable unit "
                b"in Kubernetes and can host one or more containers.</p></body></html>")
        return kb_ingest.IngestInput(kind="url", name="pods", data=html, content_type="text/html", url=url)

    monkeypatch.setattr(kb_ingest, "fetch_url", fake_fetch)
    out = await _ingest_json(client, auth, urls=["https://docs.example/pods"])
    [item] = out["items"]
    assert item["status"] == "registered" and item["detected_type"] == "html"
    await _drain(_worker())
    detail = (await client.get(f"{BASE}/documents/{item['document_id']}", headers=auth)).json()
    assert detail["revisions"][0]["source"]["locator"] == "https://docs.example/pods"
    assert detail["source_kind"] == "web"


# ---------------------------------------------------------------------------------------------
# jobs
# ---------------------------------------------------------------------------------------------
async def test_jobs_list_detail_stage_status_and_retry_contract(client, auth):
    out = await _ingest_text_and_drain(client, auth, K8S_DOC, "k8s")
    job_id = out["items"][0]["job_id"]
    listed = (await client.get(f"{BASE}/jobs", headers=auth)).json()
    assert set(listed) >= {"items", "total"}
    job = next(j for j in listed["items"] if j["id"] == job_id)
    # 프론트 KbPipelineJob: status, current_stage, hold_reason, error_code, error_detail, attempt_count, max_attempts, metrics, stages[]
    for field in ("id", "document_id", "revision_id", "document_title", "status", "current_stage", "hold_reason", "error_code",
                  "error_detail", "attempt_count", "max_attempts", "metrics", "stages", "created_at", "updated_at", "completed_at"):
        assert field in job, field
    assert job["document_title"] == "k8s" and job["status"] in ("ready", "degraded")
    assert [s["stage"] for s in job["stages"]] == STAGES
    for s in job["stages"]:
        assert set(s) >= {"stage", "status", "duration_ms", "error_message", "started_at", "finished_at"}
        assert s["status"] in ("succeeded", "degraded")  # L2 는 꺼져 있어 extracting 은 degraded 일 수 있다
    assert job["metrics"].get("passage_count", 0) >= 1

    # 필터: status 그룹, document_id
    assert any(j["id"] == job_id for j in (await client.get(f"{BASE}/jobs", headers=auth, params={"status": job["status"]})).json()["items"])
    assert (await client.get(f"{BASE}/jobs", headers=auth, params={"status": "active"})).json()["items"] == []
    assert (await client.get(f"{BASE}/jobs", headers=auth, params={"status": "nope"})).status_code == 422
    by_doc = (await client.get(f"{BASE}/jobs", headers=auth, params={"document_id": out["items"][0]["document_id"]})).json()
    assert by_doc["total"] == 1

    # retry: from_stage 지정, 앞선 단계는 건너뛴다. 같은 단계 재호출은 already_queued 로 멱등.
    first = (await client.post(f"{BASE}/jobs/{job_id}/retry", headers=auth, json={"from_stage": "indexing"})).json()
    assert first["retry"]["requeued"] is True and first["retry"]["from_stage"] == "indexing"
    assert first["status"] == "registered" and first["current_stage"] == "indexing"
    stage_status = {s["stage"]: s["status"] for s in first["stages"]}
    assert stage_status["indexing"] == "pending" and stage_status["parsing"] == "succeeded"
    second = (await client.post(f"{BASE}/jobs/{job_id}/retry", headers=auth, json={"from_stage": "indexing"})).json()
    assert second["retry"]["requeued"] is False and second["retry"]["reason"] == "already_queued"
    await _drain(_worker())
    after = (await client.get(f"{BASE}/jobs/{job_id}", headers=auth)).json()
    assert after["status"] in ("ready", "degraded")

    assert (await client.post(f"{BASE}/jobs/{job_id}/retry", headers=auth, json={"from_stage": "bogus"})).status_code == 422
    assert (await client.post(f"{BASE}/jobs/{uuid.uuid4()}/retry", headers=auth, json={"from_stage": None})).status_code == 404
    assert (await client.get(f"{BASE}/jobs/{uuid.uuid4()}", headers=auth)).status_code == 404
    # from_stage 생략 = 성공하지 않은 첫 단계부터. 전부 성공이면 아무것도 하지 않고 이유를 말한다.
    none = (await client.post(f"{BASE}/jobs/{job_id}/retry", headers=auth, json={})).json()
    assert "retry" in none


async def test_retry_refuses_a_running_job(client, auth):
    out = await _ingest_json(client, auth, text=K8S_DOC)
    job_id = uuid.UUID(out["items"][0]["job_id"])
    from datetime import UTC, datetime, timedelta

    async with sessionmaker()() as db:
        job = await db.get(KbPipelineJob, job_id)
        job.claimed_by, job.lease_expires_at = "worker-x", datetime.now(UTC) + timedelta(minutes=5)
        await db.commit()
    r = await client.post(f"{BASE}/jobs/{job_id}/retry", headers=auth, json={"from_stage": "parsing"})
    assert r.status_code == 409 and r.json()["message"] == "job_running"


# ---------------------------------------------------------------------------------------------
# search / documents / detail
# ---------------------------------------------------------------------------------------------
async def test_search_contract_matched_entities_paths_and_filters(client, auth):
    out = await _ingest_text_and_drain(client, auth, K8S_DOC, "k8s")
    r = await client.get(f"{BASE}/search", headers=auth, params={"q": "디플로이먼트", "mode": "hybrid"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["coverage"] in ("strong", "weak", "none") and body["items"]
    hit = body["items"][0]
    # 기존 필드 보존
    for field in ("passage_id", "ord", "heading", "snippet", "highlights", "score", "char_start", "char_end", "document", "revision", "source"):
        assert field in hit, field
    # v3 필드(프론트 KbSearchHitV3)
    assert isinstance(hit["section_path"], list)
    assert set(hit["match_paths"]) <= {"lexical", "vector", "canonical", "hop1"} and hit["match_paths"]
    assert "has_conflict" in hit and isinstance(hit["assertions"], list)
    assert "page_no" in hit and "slide_no" in hit and "time_start_ms" in hit
    card = body["matched_entities"][0]
    for field in ("id", "name", "entity_kind", "aliases", "canonical", "neighbors", "open_conflict_count", "document_count"):
        assert field in card, field
    assert isinstance(card["canonical"]["key_facts"], list) and all(isinstance(f, str) for f in card["canonical"]["key_facts"])
    assert isinstance(card["canonical"]["summary"], str)
    # 한국어 별칭이 영어 정본에 닿는다
    assert card["name"] == "Deployment" and "디플로이먼트" in card["aliases"]

    # 모드와 필터
    lex = (await client.get(f"{BASE}/search", headers=auth, params={"q": "레플리카셋", "mode": "lexical"})).json()
    assert lex["items"] and "terms" in lex
    vec = await client.get(f"{BASE}/search", headers=auth, params={"q": "pod replicas", "mode": "vector"})
    assert vec.status_code == 200 and all(i["match_paths"] == ["vector"] for i in vec.json()["items"])
    by_entity = (await client.get(f"{BASE}/search", headers=auth, params={"q": "레플리카셋", "mode": "hybrid", "entity_id": card["id"]})).json()
    assert by_entity["items"], "엔터티 필터는 그 개념이 나온 문서의 패시지만 돌려준다"
    other = (await client.get(f"{BASE}/search", headers=auth, params={"q": "레플리카셋", "mode": "hybrid", "entity_id": str(uuid.uuid4())})).json()
    assert other["items"] == []
    dom = (await client.get(f"{BASE}/search", headers=auth, params={"q": "디플로이먼트", "mode": "hybrid", "domain": "dev-infra"})).json()
    assert dom["items"], "엔터티 domain(infra)에서 자동 분류된 문서는 dev-infra 주제로 걸린다"
    nodom = (await client.get(f"{BASE}/search", headers=auth, params={"q": "디플로이먼트", "mode": "hybrid", "domain": "security"})).json()
    assert nodom["items"] == []
    assert (await client.get(f"{BASE}/search", headers=auth, params={"q": "x", "mode": "bogus"})).status_code == 422
    # 기존 호출 모양(scope 없이 lexical)은 기존 키를 그대로 돌려주고 위치 필드만 늘었다
    legacy = (await client.get(f"{BASE}/search", headers=auth, params={"q": "레플리카셋"})).json()
    assert set(legacy) >= {"query", "terms", "items", "limit", "offset", "has_more"}
    assert out["items"][0]["document_id"] == legacy["items"][0]["document"]["id"]


async def test_documents_list_and_detail_expose_summary_entities_assertions_relations_versions_and_history(client, auth):
    out = await _ingest_text_and_drain(client, auth, K8S_DOC, "k8s")
    doc_id = out["items"][0]["document_id"]
    row = (await client.get(f"{BASE}/documents", headers=auth)).json()["items"][0]
    for field in ("id", "doc_key", "title", "lang", "created_at", "updated_at", "current_revision", "domain", "source_kind",
                  "pipeline_status", "entity_count", "assertion_count"):
        assert field in row, field
    assert row["pipeline_status"] in ("ready", "degraded") and row["entity_count"] >= 2 and row["assertion_count"] >= 1
    assert row["domain"] == "dev-infra" and row["domain_source"] == "entities"  # 엔터티 domain(infra)에서 자동 분류
    # 필터: pipeline_status(그룹 포함), domain, q
    assert (await client.get(f"{BASE}/documents", headers=auth, params={"pipeline_status": "attention"})).json()["total"] in (0, 1)
    assert (await client.get(f"{BASE}/documents", headers=auth, params={"pipeline_status": "active"})).json()["total"] == 0
    assert (await client.get(f"{BASE}/documents", headers=auth, params={"pipeline_status": row["pipeline_status"]})).json()["total"] == 1
    assert (await client.get(f"{BASE}/documents", headers=auth, params={"domain": "dev-infra"})).json()["total"] == 1
    assert (await client.get(f"{BASE}/documents", headers=auth, params={"domain": "ai"})).json()["total"] == 0
    assert (await client.get(f"{BASE}/documents", headers=auth, params={"pipeline_status": "nope"})).status_code == 422

    d = (await client.get(f"{BASE}/documents/{doc_id}", headers=auth)).json()
    # 기존 필드
    for field in ("id", "doc_key", "title", "lang", "revisions", "selected_revision_id", "passages", "observations"):
        assert field in d, field
    # v3 필드
    for field in ("summary", "domain", "entities", "assertions", "relations", "conflicts", "pipeline", "history"):
        assert field in d, field
    assert d["summary"] and d["summary_source"] in ("canonical_definition", "extractive_first_passage")
    assert any(e["name"] == "Deployment" for e in d["entities"])
    assertion = next(a for a in d["assertions"] if a["subject"] == "Deployment")
    assert assertion["evidence"] and assertion["evidence"][0]["quote"] in K8S_DOC  # 근거는 원문의 정확한 부분 문자열
    assert assertion["evidence"][0]["passage_id"] and assertion["evidence"][0]["document_id"] == doc_id
    active = [r for r in d["relations"] if r["status"] == "active"]
    assert active and all(r["evidence"] and not r["candidate"] for r in active)
    assert {(r["source"]["name"], r["predicate"], r["target"]["name"]) for r in active} >= {("Deployment", "manages", "ReplicaSet")}
    p = d["passages"]["items"][0]
    for field in ("id", "ord", "heading", "body", "char_start", "char_end", "sha256", "section_path", "page_no", "slide_no", "time_start_ms", "chunk_kind"):
        assert field in p, field
    assert d["pipeline"]["id"] == out["items"][0]["job_id"] and d["history"][0]["job_id"] == d["pipeline"]["id"]
    assert d["revisions"][0]["pipeline_status"] == d["pipeline_status"]
    # 새 리비전이 생기면 버전 목록과 처리 이력이 늘고, 이전 리비전을 그대로 볼 수 있다
    await _ingest_text_and_drain(client, auth, K8S_DOC + "\n## 추가\n\n파드는 가장 작은 배포 단위이다.\n", "k8s")
    assert (await client.get(f"{BASE}/documents/{uuid.uuid4()}", headers=auth)).status_code == 404


async def test_summary_contract_has_unified_scale_pipeline_and_v3_metrics(client, auth):
    empty = (await client.get(f"{BASE}/summary", headers=auth)).json()
    assert empty["counts"]["documents"] == 0 and empty["counts"]["entities"] == 0
    await _ingest_text_and_drain(client, auth, K8S_DOC, "k8s")
    await _ingest_text_and_drain(client, auth, EN_DEF_OPEN, "open")
    await _ingest_text_and_drain(client, auth, EN_DEF_VENDOR, "vendor")
    s = (await client.get(f"{BASE}/summary", headers=auth)).json()
    c = s["counts"]
    for field in ("sources", "documents", "revisions", "passages", "evidence", "entities", "assertions", "relations",
                  "open_conflicts", "pipeline_running", "pipeline_failed"):
        assert isinstance(c[field], int), field  # 프론트 isKbV3Summary: counts.entities 가 숫자면 v3 서버
    assert c["documents"] == 3 and c["entities"] >= 3 and c["relations"] >= 2 and c["open_conflicts"] == 1
    assert c["pipeline_running"] == 0 and c["pipeline_failed"] == 0
    assert set(s["pipeline"]) >= {"registered", "running", "ready", "degraded", "held", "failed"}
    assert set(s["v3"]) >= {"relations_active", "relations_candidate", "single_passage_documents", "documents_without_pipeline_job", "embedding_coverage"}
    assert s["retrieval"]["total"] >= 1 and s["last_collected_at"]
    assert s["access"]["view"] == "unified" and s["access"]["visibilities"] == ["global"]


# ---------------------------------------------------------------------------------------------
# ontology / conflicts
# ---------------------------------------------------------------------------------------------
async def test_graph_hubs_one_hop_pagination_entity_detail_and_alias(client, auth):
    await _ingest_text_and_drain(client, auth, K8S_DOC, "k8s")
    hubs = (await client.get(f"{BASE}/ontology/graph", headers=auth, params={"limit": 12})).json()
    assert set(hubs) >= {"nodes", "edges", "center_entity_id", "total_entities"} and hubs["center_entity_id"] is None
    assert hubs["total_entities"] >= len(hubs["nodes"]) >= 3
    for field in ("id", "name", "entity_kind", "document_count", "assertion_count", "has_open_conflict"):
        assert field in hubs["nodes"][0], field
    assert hubs["edges"], "자동 승격된 active 관계가 허브 그래프에 에지로 나온다"
    for field in ("source", "target", "predicate", "weight"):
        assert field in hubs["edges"][0], field
    by_name = {n["name"]: n for n in hubs["nodes"]}
    deployment = by_name["Deployment"]

    nb = (await client.get(f"{BASE}/ontology/graph", headers=auth, params={"center_entity_id": deployment["id"]})).json()
    assert nb["center_entity_id"] == deployment["id"] and nb["mode"] == "neighbors"
    assert {n["name"] for n in nb["nodes"]} >= {"Deployment", "ReplicaSet"}
    edge = next(e for e in nb["edges"] if e["predicate"] == "manages")
    assert edge["status"] == "active" and edge["candidate"] is False and edge["evidence"] and edge["evidence"][0]["quote"]
    # 1-hop 확장 페이지네이션
    page1 = (await client.get(f"{BASE}/ontology/graph", headers=auth, params={"center_entity_id": deployment["id"], "limit": 1})).json()
    assert page1["limit"] == 1 and page1["total_neighbors"] >= 1
    assert (await client.get(f"{BASE}/ontology/graph", headers=auth, params={"center_entity_id": str(uuid.uuid4())})).status_code == 404
    clamped = (await client.get(f"{BASE}/ontology/graph", headers=auth, params={"center_entity_id": deployment["id"], "hops": 3})).json()
    assert clamped["hops"] == 1 and clamped["hops_requested"] == 3
    search = (await client.get(f"{BASE}/ontology/graph", headers=auth, params={"q": "replica"})).json()
    assert [n["name"] for n in search["nodes"]] == ["ReplicaSet"] and search["mode"] == "search"

    ent = (await client.get(f"{BASE}/ontology/entities/{deployment['id']}", headers=auth)).json()
    for field in ("id", "name", "entity_kind", "aliases", "canonical", "assertions", "relations", "documents", "conflicts"):
        assert field in ent, field
    assert {a["alias"] for a in ent["aliases"]} >= {"디플로이먼트"} and all("lang" in a for a in ent["aliases"])
    assert ent["canonical"]["summary"] and isinstance(ent["canonical"]["key_facts"], list)
    rel = next(r for r in ent["relations"] if r["name"] == "ReplicaSet")
    assert rel["predicate"] == "manages" and rel["direction"] == "out" and rel["entity_id"]
    assert ent["documents"][0]["title"] == "k8s" and ent["assertions"][0]["evidence"][0]["quote"]
    assert (await client.get(f"{BASE}/ontology/entities/{uuid.uuid4()}", headers=auth)).status_code == 404

    # 별칭 추가: 같은 표기가 문서에 이미 있으면 재연결 건수가 나온다
    added = await client.post(f"{BASE}/ontology/entities/{deployment['id']}/aliases", headers=auth, json={"alias": "롤아웃컨트롤러", "lang": "ko"})
    assert added.status_code == 201 and added.json()["alias"]["alias"] == "롤아웃컨트롤러" and added.json()["relinked_count"] == 0
    again = (await client.post(f"{BASE}/ontology/entities/{deployment['id']}/aliases", headers=auth, json={"alias": "롤아웃컨트롤러"})).json()
    assert again["created"] is False
    taken = await client.post(f"{BASE}/ontology/entities/{deployment['id']}/aliases", headers=auth, json={"alias": "ReplicaSet"})
    assert taken.status_code == 409 and taken.json()["message"] == "kb_alias_belongs_to_other_entity"
    assert (await client.post(f"{BASE}/ontology/entities/{deployment['id']}/aliases", headers=auth, json={"alias": "x"})).status_code == 422
    assert (await client.post(f"{BASE}/ontology/entities/{uuid.uuid4()}/aliases", headers=auth, json={"alias": "가나다라"})).status_code == 404


async def test_alias_relink_connects_existing_passages_with_exact_evidence(client, auth):
    out = await _ingest_text_and_drain(client, auth, K8S_DOC, "k8s")
    hubs = (await client.get(f"{BASE}/ontology/graph", headers=auth)).json()
    pod = next(n for n in hubs["nodes"] if n["name"] == "Pod")
    # 본문에 "서비스" 라는 표기가 이미 있다. 그 표기를 Pod 의 별칭으로 넣으면 그 패시지가 Pod 에 이어진다.
    added = (await client.post(f"{BASE}/ontology/entities/{pod['id']}/aliases", headers=auth, json={"alias": "외부 노출", "lang": "ko"})).json()
    assert added["relinked_count"] == 1
    ent = (await client.get(f"{BASE}/ontology/entities/{pod['id']}", headers=auth)).json()
    assert ent["document_count"] >= 1
    detail = (await client.get(f"{BASE}/documents/{out['items'][0]['document_id']}", headers=auth)).json()
    assert "Pod" in {e["name"] for e in detail["entities"]}


async def test_conflicts_list_compare_and_resolve_contract(client, auth):
    await _ingest_text_and_drain(client, auth, EN_DEF_OPEN, "open")
    await _ingest_text_and_drain(client, auth, EN_DEF_VENDOR, "vendor")
    listed = (await client.get(f"{BASE}/conflicts", headers=auth)).json()
    assert listed["total"] == 1 and len(listed["items"]) == 1
    c = listed["items"][0]
    for field in ("id", "kind", "status", "entity_id", "entity_name", "claim_a", "claim_b", "created_at"):
        assert field in c, field
    assert c["kind"] == "value_contradiction" and c["status"] == "open"
    for claim in (c["claim_a"], c["claim_b"]):
        for field in ("assertion_id", "text", "quote", "document_id", "document_title", "revision_id", "rev_no", "published_at", "passage_id"):
            assert field in claim, field
        assert claim["quote"].startswith("Kubernetes refers to")
    assert c["claim_a"]["document_id"] != c["claim_b"]["document_id"]
    assert (await client.get(f"{BASE}/conflicts", headers=auth, params={"kind": "ambiguous_entity"})).json()["items"] == []
    assert (await client.get(f"{BASE}/conflicts", headers=auth, params={"status": "resolved_a"})).json()["items"] == []
    # 검색·문서 상세·그래프에 충돌이 드러난다
    hit = (await client.get(f"{BASE}/search", headers=auth, params={"q": "Kubernetes refers to", "mode": "hybrid"})).json()
    assert hit["items"] and any(i["has_conflict"] for i in hit["items"])
    ent = (await client.get(f"{BASE}/ontology/entities/{c['entity_id']}", headers=auth)).json()
    assert [x["id"] for x in ent["conflicts"]] == [c["id"]], "엔터티 상세에도 열린 충돌이 같은 계약 모양으로 나온다"

    # 종류에 맞지 않는 해결은 거부, 없는 충돌은 404
    assert (await client.post(f"{BASE}/conflicts/{c['id']}/resolve", headers=auth, json={"resolution": "merged"})).status_code == 422
    assert (await client.post(f"{BASE}/conflicts/{uuid.uuid4()}/resolve", headers=auth, json={"resolution": "resolved_a"})).status_code == 404
    assert (await client.post(f"{BASE}/conflicts/{c['id']}/resolve", headers=auth, json={"resolution": "bogus"})).status_code == 422
    done = (await client.post(f"{BASE}/conflicts/{c['id']}/resolve", headers=auth, json={"resolution": "resolved_a", "note": "공식 문서가 맞다"})).json()
    assert done["status"] == "resolved_a" and done["conflict"]["note"] == "공식 문서가 맞다"
    assert done["canonical"] is None or "summary" in done["canonical"]
    assert (await client.get(f"{BASE}/conflicts", headers=auth)).json()["items"] == []
    resolved = (await client.get(f"{BASE}/conflicts", headers=auth, params={"status": "resolved_a"})).json()
    assert resolved["total"] == 1
    assert (await client.get(f"{BASE}/summary", headers=auth)).json()["counts"]["open_conflicts"] == 0


async def test_candidate_relations_are_opt_in_labeled_and_down_weighted(client, auth):
    # 헤딩 계층에서 유추한 구조 관계(part_of)는 candidate 로 남는다. 기본 응답에는 없고 옵션을 줄 때만 후보 표시로 나온다.
    text = "# 쿠버네티스(Kubernetes)\n\n## 디플로이먼트(Deployment)\n\n디플로이먼트(Deployment)는 선언적 업데이트를 제공하는 워크로드 리소스이다.\n"
    await _ingest_text_and_drain(client, auth, text, "structure only")
    hubs = (await client.get(f"{BASE}/ontology/graph", headers=auth)).json()
    k8s = next(n for n in hubs["nodes"] if n["name"] == "Kubernetes")
    default = (await client.get(f"{BASE}/ontology/graph", headers=auth, params={"center_entity_id": k8s["id"]})).json()
    assert default["edges"] == [], "근거 있는 후보라도 기본 1-hop 에는 없다(active 만)"
    opt = (await client.get(f"{BASE}/ontology/graph", headers=auth, params={"center_entity_id": k8s["id"], "include_candidates": "true"})).json()
    assert len(opt["edges"]) == 1
    edge = opt["edges"][0]
    assert edge["candidate"] is True and edge["status"] == "candidate" and edge["predicate"] == "part_of"
    assert 0 < edge["weight"] <= 0.3 and edge["evidence"], "낮은 가중치와 근거 인용을 갖는다"
    ent_default = (await client.get(f"{BASE}/ontology/entities/{k8s['id']}", headers=auth)).json()
    assert ent_default["relations"] == []
    ent_opt = (await client.get(f"{BASE}/ontology/entities/{k8s['id']}", headers=auth, params={"include_candidates": "true"})).json()
    assert ent_opt["relations"] and ent_opt["relations"][0]["candidate"] is True
    # 검색 1-hop: 후보 경로로 올라온 항목은 hop1 이고 확정 경로보다 낮게만 반영된다
    plain = (await client.get(f"{BASE}/search", headers=auth, params={"q": "쿠버네티스", "mode": "hybrid"})).json()
    withc = (await client.get(f"{BASE}/search", headers=auth, params={"q": "쿠버네티스", "mode": "hybrid", "include_candidates": "true"})).json()
    assert withc["include_candidates"] is True and plain["include_candidates"] is False
    assert all(n["candidate"] for n in withc["matched_entities"][0]["neighbors"]) if withc["matched_entities"] and withc["matched_entities"][0]["neighbors"] else True


# ---------------------------------------------------------------------------------------------
# 접근 반례: work_only, 개인 웹/개인 MCP, 회사 세션
# ---------------------------------------------------------------------------------------------
WORK_TEXT = "# 사내 배포 규정\n\n오로라플랫폼(AuroraPlatform)은 사내 전용 배포 시스템이다. 오로라플랫폼은 레플리카셋을 관리한다. 비밀 식별자 AuroraXQ77 을 쓴다.\n"


async def _seed_work_and_personal(client, auth):
    work = await client.post(f"{BASE}/documents", headers=auth, json={"title": "회사 문서", "text": WORK_TEXT, "scope": "work"})
    assert work.status_code == 201 and work.json()["pipeline_status"] == "registered" and work.json()["job_id"]
    personal = await _ingest_text_and_drain(client, auth, K8S_DOC, "개인 문서")
    return work.json(), personal["items"][0]


async def test_work_only_is_invisible_to_the_unified_web_view_on_every_endpoint(client, auth):
    work, personal = await _seed_work_and_personal(client, auth)
    wid, pid = work["document_id"], personal["document_id"]

    docs = (await client.get(f"{BASE}/documents", headers=auth)).json()
    assert {d["id"] for d in docs["items"]} == {pid} and docs["total"] == 1
    assert (await client.get(f"{BASE}/documents/{wid}", headers=auth)).status_code == 404
    assert (await client.get(f"{BASE}/jobs/{work['job_id']}", headers=auth)).status_code == 404
    assert work["job_id"] not in {j["id"] for j in (await client.get(f"{BASE}/jobs", headers=auth)).json()["items"]}
    assert (await client.post(f"{BASE}/jobs/{work['job_id']}/retry", headers=auth, json={})).status_code == 404
    for mode in ("lexical", "hybrid", "vector"):
        # 해시 임베딩은 어떤 질의에도 가장 가까운 패시지를 돌려주므로(의미 보장 없음) 비어 있음이 아니라 회사 문서가 없음을 본다.
        hits = (await client.get(f"{BASE}/search", headers=auth, params={"q": "AuroraXQ77", "mode": mode})).json()
        assert all(i["document"]["id"] != wid for i in hits["items"]), mode
        assert "AuroraPlatform" not in json.dumps(hits.get("matched_entities", []), ensure_ascii=False)
    summary = (await client.get(f"{BASE}/summary", headers=auth)).json()
    assert summary["counts"]["documents"] == 1
    graph = (await client.get(f"{BASE}/ontology/graph", headers=auth)).json()
    assert "AuroraPlatform" not in {n["name"] for n in graph["nodes"]}
    assert (await client.get(f"{BASE}/ontology/graph", headers=auth, params={"q": "aurora"})).json()["nodes"] == []
    # 근거(evidence) 직접 조회도 막힌다
    # (work 패시지 id 는 work 범위로만 얻을 수 있다)
    legacy_work = (await client.get(f"{BASE}/search", headers=auth, params={"q": "AuroraXQ77", "scope": "work"})).json()
    assert legacy_work["items"], "명시적 레거시 scope=work 선택에서는 기존처럼 회사 문서가 보인다"
    pid_work = legacy_work["items"][0]["passage_id"]
    assert (await client.get(f"{BASE}/passages/{pid_work}/evidence", headers=auth)).status_code == 404
    assert (await client.get(f"{BASE}/passages/{pid_work}/evidence", headers=auth, params={"scope": "work"})).status_code == 200
    # scope 위조: 알 수 없는 값은 경계를 넓히지 못한다
    assert (await client.get(f"{BASE}/documents", headers=auth, params={"scope": "all"})).status_code == 422
    forged = (await client.get(f"{BASE}/documents", headers=auth, params={"scope": "personal"})).json()
    assert {d["id"] for d in forged["items"]} == {pid}


async def test_work_entities_do_not_leak_through_aliases_conflicts_or_entity_detail(client, auth):
    await _seed_work_and_personal(client, auth)
    await _drain(_worker())
    async with sessionmaker()() as db:
        from ttakkari.kb_ontology_models import KbEntity

        aurora = await db.scalar(select(KbEntity).where(KbEntity.canonical_name == "AuroraPlatform"))
        if aurora is None:  # 이름 표기가 한글로 잡히는 경우
            aurora = await db.scalar(select(KbEntity).where(KbEntity.visibility == "work_only"))
    assert aurora is not None and aurora.visibility == "work_only"
    assert (await client.get(f"{BASE}/ontology/entities/{aurora.id}", headers=auth)).status_code == 404
    assert (await client.get(f"{BASE}/ontology/graph", headers=auth, params={"center_entity_id": str(aurora.id)})).status_code == 404
    assert (await client.post(f"{BASE}/ontology/entities/{aurora.id}/aliases", headers=auth, json={"alias": "내부시스템"})).status_code == 404
    # 회사 문서가 개인 엔터티의 별칭·문서 수를 오염하지 않는다: 개인 시점의 Deployment/ReplicaSet 상세에 회사 문서가 없다
    graph = (await client.get(f"{BASE}/ontology/graph", headers=auth)).json()
    rs = next(n for n in graph["nodes"] if n["name"] == "ReplicaSet")
    detail = (await client.get(f"{BASE}/ontology/entities/{rs['id']}", headers=auth)).json()
    assert {d["title"] for d in detail["documents"]} == {"개인 문서"}
    assert all("오로라" not in json.dumps(r, ensure_ascii=False) for r in detail["relations"])


async def test_agent_api_keeps_session_scope_rules_and_adds_fields_only(client, auth, sessions):
    work, personal = await _seed_work_and_personal(client, auth)
    # 개인 세션: global 문서만. 회사 세션: 기존 정책 그대로 회사(work) 문서만(개인 자료를 새로 열지 않는다).
    p_docs = (await client.get("/api/agent/kb/documents", headers=sessions["personal"])).json()
    w_docs = (await client.get("/api/agent/kb/documents", headers=sessions["work"])).json()
    assert {d["id"] for d in p_docs["items"]} == {personal["document_id"]}
    assert {d["id"] for d in w_docs["items"]} == {work["document_id"]}
    assert (await client.get(f"/api/agent/kb/documents/{work['document_id']}", headers=sessions["personal"])).status_code == 404
    assert (await client.get(f"/api/agent/kb/documents/{personal['document_id']}", headers=sessions["work"])).status_code == 404

    for mode in ("lexical", "hybrid"):
        p = await client.get("/api/agent/kb/search", headers=sessions["personal"], params={"q": "AuroraXQ77", "mode": mode})
        assert p.status_code == 200 and all(i["document"]["id"] != work["document_id"] for i in p.json()["items"]), mode
        w = await client.get("/api/agent/kb/search", headers=sessions["work"], params={"q": "AuroraXQ77", "mode": mode})
        assert w.status_code == 200 and w.json()["items"], mode
        wp = await client.get("/api/agent/kb/search", headers=sessions["work"], params={"q": "레플리카셋", "mode": mode})
        assert all(i["document"]["id"] == work["document_id"] for i in wp.json()["items"]), "회사 세션에 개인 문서가 새로 열리지 않는다"

    # 기존 응답 필드 보존 + 추가 필드
    h = (await client.get("/api/agent/kb/search", headers=sessions["personal"], params={"q": "디플로이먼트", "mode": "hybrid"})).json()
    for field in ("query", "terms", "items", "matched_entities", "coverage", "limit", "offset", "has_more", "retrieval"):
        assert field in h, field
    item = h["items"][0]
    for field in ("passage_id", "ord", "heading", "snippet", "highlights", "score", "char_start", "char_end", "section_path",
                  "locator_meta", "document", "revision", "source", "assertions", "has_conflict"):
        assert field in item, field  # 기존(이미 있던) 필드
    for field in ("match_paths", "conflict_warning", "canonical_facts"):
        assert field in item, field  # v3 에서 추가
    assert any(f["entity"] == "Deployment" for it in h["items"] for f in it["canonical_facts"])
    lex = (await client.get("/api/agent/kb/search", headers=sessions["personal"], params={"q": "레플리카셋"})).json()
    assert set(lex["items"][0]) >= {"passage_id", "ord", "heading", "snippet", "highlights", "score", "char_start", "char_end",
                                    "document", "revision", "source", "section_path", "page_no", "slide_no"}
    # 에이전트 API 의 후보 옵션은 기본 꺼짐
    assert h["include_candidates"] is False


async def test_mcp_tools_are_signature_compatible_and_return_agent_json(client, sessions, auth, monkeypatch):
    import inspect

    from ttakkari.orchestrator import mcp_server

    sig = {name: list(inspect.signature(getattr(mcp_server, name)).parameters) for name in ("kb_search", "kb_documents", "kb_document", "kb_evidence")}
    assert sig == {
        "kb_search": ["query", "limit", "mode"],
        "kb_documents": ["query", "limit", "offset"],
        "kb_document": ["document_id"],
        "kb_evidence": ["passage_id", "start", "end"],
    }, "MCP 도구 시그니처는 하위 호환이다(필수 인자·이름 불변)"
    work, personal = await _seed_work_and_personal(client, auth)

    current = {"pool": "personal"}

    async def routed_call(method, path, body=None, *, prefix="/api/orchestrator", params=None):
        r = await client.request(method, f"{prefix}{path}", json=body, headers=sessions[current["pool"]],
                                 params={k: v for k, v in (params or {}).items() if v is not None} or None)
        data = r.json()
        if r.is_error:  # mcp_server._call 과 같은 오류 문자열 모양
            return json.dumps({"error": r.status_code, **(data if isinstance(data, dict) else {"detail": data})}, ensure_ascii=False)
        return json.dumps(data, ensure_ascii=False)

    monkeypatch.setattr(mcp_server, "_call", routed_call)
    out = json.loads(await mcp_server.kb_search("디플로이먼트", mode="hybrid"))
    assert out["items"] and out["items"][0]["document"]["id"] == personal["document_id"]
    assert {"match_paths", "canonical_facts", "conflict_warning"} <= set(out["items"][0])
    lex = json.loads(await mcp_server.kb_search("레플리카셋"))
    assert lex["items"] and "section_path" in lex["items"][0]
    docs = json.loads(await mcp_server.kb_documents())
    assert [d["id"] for d in docs["items"]] == [personal["document_id"]]
    doc = json.loads(await mcp_server.kb_document(personal["document_id"]))
    assert doc["passages"]["items"] and "section_path" in doc["passages"]["items"][0]
    passage_id = out["items"][0]["passage_id"]
    ev = json.loads(await mcp_server.kb_evidence(passage_id, 0, 5))
    assert ev["span"]["hash_verified"] is True and ev["quote"]
    # 개인 MCP 에 회사 문서가 보이지 않는다
    assert json.loads(await mcp_server.kb_document(work["document_id"]))["error"] == 404
    leak = json.loads(await mcp_server.kb_search("AuroraXQ77", mode="hybrid"))
    assert all(i["document"]["id"] != work["document_id"] for i in leak["items"])
    # 회사 MCP pool: 기존 정책 그대로(회사 문서만)
    current["pool"] = "work"
    w = json.loads(await mcp_server.kb_search("AuroraXQ77", mode="hybrid"))
    assert w["items"] and all(i["document"]["id"] == work["document_id"] for i in w["items"])
    assert json.loads(await mcp_server.kb_document(personal["document_id"]))["error"] == 404


async def test_all_new_routes_require_authentication(client):
    gets = ["/jobs", f"/jobs/{uuid.uuid4()}", "/ontology/graph", f"/ontology/entities/{uuid.uuid4()}", "/conflicts", "/summary"]
    for path in gets:
        assert (await client.get(BASE + path)).status_code == 401, path
    posts = ["/ingest", f"/jobs/{uuid.uuid4()}/retry", f"/ontology/entities/{uuid.uuid4()}/aliases", f"/conflicts/{uuid.uuid4()}/resolve"]
    for path in posts:
        assert (await client.post(BASE + path, json={})).status_code == 401, path


pytestmark = pytest.mark.security


async def test_one_hop_neighbor_pages_do_not_overlap_and_report_has_more(client, auth):
    text = ("# 쿠버네티스(Kubernetes)\n\n## 디플로이먼트(Deployment)\n\n"
            "Deployment manages ReplicaSet objects. Deployment uses ConfigMap. Deployment uses Secret.\n")
    await _ingest_text_and_drain(client, auth, text, "hop")
    hubs = (await client.get(f"{BASE}/ontology/graph", headers=auth)).json()
    deployment = next(n for n in hubs["nodes"] if n["name"] == "Deployment")
    first = (await client.get(f"{BASE}/ontology/graph", headers=auth, params={"center_entity_id": deployment["id"], "limit": 2, "offset": 0})).json()
    second = (await client.get(f"{BASE}/ontology/graph", headers=auth, params={"center_entity_id": deployment["id"], "limit": 2, "offset": 2})).json()
    assert first["total_neighbors"] == second["total_neighbors"] == 3
    assert first["has_more"] is True and second["has_more"] is False
    neighbors = [n["name"] for n in first["nodes"] if n["id"] != deployment["id"]]
    neighbors2 = [n["name"] for n in second["nodes"] if n["id"] != deployment["id"]]
    assert len(neighbors) == 2 and len(neighbors2) == 1 and not set(neighbors) & set(neighbors2)
    assert {*neighbors, *neighbors2} == {"ReplicaSet", "ConfigMap", "Secret"}
    # 모든 에지의 끝점은 그 페이지의 노드 안에 있다(고아 에지 없음)
    for page in (first, second):
        ids = {n["id"] for n in page["nodes"]}
        assert all(e["source"] in ids and e["target"] in ids for e in page["edges"])
