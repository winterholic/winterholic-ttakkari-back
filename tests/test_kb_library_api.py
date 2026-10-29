"""지식 도서관(kb) 읽기·검색·등록 API: 멱등 등록, 현재 리비전 검색, 근거 위치, 인증(ADR 0063)."""

import shutil
import uuid

import pytest
from sqlalchemy import func, select

from ttakkari.db import sessionmaker
from ttakkari.kb_models import KbDocument, KbRevision, KbSource
from ttakkari.kb_store import originals_root

BASE = "/api/assistant/kb"
TEXT_V1 = "첫째 문단은 PostgreSQL 격리 수준을 설명한다.\n\n둘째 문단은 완전히 다른 주제인 요리법이다.\n\n셋째 문단도 격리 수준을 짧게 다시 언급한다.\n"
TEXT_V2 = "개정판 문단은 Serializable 격리 수준만 다룬다.\n\n부록은 요리법이다.\n"


@pytest.fixture(autouse=True)
def clean_originals():
    root = originals_root()
    if root.exists():
        shutil.rmtree(root)
    root.mkdir(parents=True, exist_ok=True)
    yield
    if root.exists():
        shutil.rmtree(root)


async def register(client, auth, **body):
    payload = {"title": "격리 수준 노트", "text": TEXT_V1, "lang": "ko", **body}
    r = await client.post(BASE + "/documents", headers=auth, json=payload)
    assert r.status_code == 201, r.text
    return r.json()


async def test_requires_auth(client):
    for method, path in (("get", "/summary"), ("get", "/observations"), ("get", "/documents"), ("get", "/search?q=x"),
                         ("get", f"/documents/{uuid.uuid4()}"), ("get", f"/passages/{uuid.uuid4()}/evidence")):
        r = await getattr(client, method)(BASE + path)
        assert r.status_code == 401, path
    r = await client.post(BASE + "/documents", json={"title": "t", "text": "x"})
    assert r.status_code == 401


async def test_register_is_idempotent_and_versions_by_source_key(client, auth):
    first = await register(client, auth)
    assert first["created"] is True and first["rev_no"] == 1 and first["passage_count"] == 3
    again = await register(client, auth)
    assert again["created"] is False
    assert (again["document_id"], again["revision_id"]) == (first["document_id"], first["revision_id"])
    assert again["passage_count"] == 3
    async with sessionmaker()() as db:
        for model in (KbSource, KbDocument, KbRevision):
            assert await db.scalar(select(func.count()).select_from(model)) == 1

    keyed = await register(client, auth, source_key="manual:isolation", text=TEXT_V1)
    revised = await register(client, auth, source_key="manual:isolation", text=TEXT_V2)
    assert revised["document_id"] == keyed["document_id"]
    assert revised["rev_no"] == 2 and revised["created"] is True


async def test_register_rejects_bad_input(client, auth):
    for body in ({"title": "t", "text": "a\x00b"}, {"title": "  ", "text": "x"}, {"title": "t", "text": ""},
                 {"title": "t", "text": "x", "unknown": 1}):
        r = await client.post(BASE + "/documents", headers=auth, json=body)
        assert r.status_code == 422, body
    async with sessionmaker()() as db:
        assert await db.scalar(select(func.count()).select_from(KbSource)) == 0


async def test_summary_and_document_list_paginate(client, auth):
    empty = (await client.get(BASE + "/summary", headers=auth)).json()
    assert empty["counts"]["documents"] == 0 and empty["last_collected_at"] is None
    for i in range(3):
        await register(client, auth, title=f"문서 {i}", text=f"본문 {i} 입니다.")
    summary = (await client.get(BASE + "/summary", headers=auth)).json()
    assert summary["counts"]["documents"] == 3 and summary["counts"]["passages"] == 3
    page = (await client.get(BASE + "/documents", params={"limit": 2, "offset": 0}, headers=auth)).json()
    assert page["total"] == 3 and len(page["items"]) == 2
    rest = (await client.get(BASE + "/documents", params={"limit": 2, "offset": 2}, headers=auth)).json()
    assert len(rest["items"]) == 1
    assert {d["id"] for d in page["items"]}.isdisjoint({d["id"] for d in rest["items"]})
    row = page["items"][0]
    assert row["current_revision"]["rev_no"] == 1 and row["current_revision"]["passage_count"] == 1
    filtered = (await client.get(BASE + "/documents", params={"q": "문서 1"}, headers=auth)).json()
    assert [d["title"] for d in filtered["items"]] == ["문서 1"]
    wildcard = (await client.get(BASE + "/documents", params={"q": "%"}, headers=auth)).json()
    assert wildcard["total"] == 0


async def test_observations_are_bounded_body_free_and_include_revision_passage_metadata(client, auth):
    made = await register(client, auth, title="관측 메타데이터", source_key="observation:metadata",
                          text="비밀본문이아니라테스트용관측문장입니다.")
    response = await client.get(BASE + "/observations", headers=auth,
                                params={"q": "관측 메타데이터", "limit": 1, "revision_limit": 1,
                                        "passage_limit": 1})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["scope"] == {"principal": "owner", "reader_pool": "personal",
                             "candidate_boundary": "access_filter(reader_pool)",
                             "hidden_and_other_tenant_counted": False}
    assert body["counts"] == {"candidates": 1, "visible": 1,
                              "excluded": {"context": 0, "covered": 0}}
    item = body["items"][0]
    revision = item["revisions"][0]
    assert item["document_id"] == made["document_id"]
    assert revision["id"] == made["revision_id"]
    assert revision["passage_total"] == 1 and revision["passages"][0]["id"]
    assert "비밀본문이아니라테스트용관측문장입니다." not in response.text
    assert "body" not in revision["passages"][0]


async def test_document_detail_lists_revisions_and_selected_passages(client, auth):
    v1 = await register(client, auth, source_key="manual:isolation", text=TEXT_V1)
    v2 = await register(client, auth, source_key="manual:isolation", text=TEXT_V2)
    detail = (await client.get(f"{BASE}/documents/{v1['document_id']}", headers=auth)).json()
    assert [r["rev_no"] for r in detail["revisions"]] == [2, 1]
    assert [r["current"] for r in detail["revisions"]] == [True, False]
    assert detail["selected_revision_id"] == v2["revision_id"]
    assert detail["passages"]["total"] == 2
    assert detail["revisions"][0]["source"]["sha256"] != detail["revisions"][1]["source"]["sha256"]
    old = (await client.get(f"{BASE}/documents/{v1['document_id']}",
                            params={"revision_id": v1["revision_id"], "passage_limit": 2}, headers=auth)).json()
    assert old["passages"]["total"] == 3 and len(old["passages"]["items"]) == 2
    assert old["passages"]["items"][0]["char_start"] == 0
    missing = await client.get(f"{BASE}/documents/{uuid.uuid4()}", headers=auth)
    assert missing.status_code == 404
    other_rev = await client.get(f"{BASE}/documents/{v1['document_id']}",
                                 params={"revision_id": str(uuid.uuid4())}, headers=auth)
    assert other_rev.status_code == 404


async def test_search_finds_passages_with_highlights_and_current_only(client, auth):
    v1 = await register(client, auth, source_key="manual:isolation", text=TEXT_V1)
    other = await register(client, auth, title="다른 문서", text="무관한 기록이다.")
    hits = (await client.get(BASE + "/search", params={"q": "격리 수준"}, headers=auth)).json()
    assert hits["terms"] == ["격리", "수준"]
    assert [h["document"]["id"] for h in hits["items"]] == [v1["document_id"]] * 2
    top = hits["items"][0]
    assert top["revision"]["current"] is True and top["source"]["source_key"] == "manual:isolation"
    assert top["snippet"][top["highlights"][0][0]:top["highlights"][0][1]] == "격리"
    assert other["document_id"] not in {h["document"]["id"] for h in hits["items"]}

    await register(client, auth, source_key="manual:isolation", text=TEXT_V2)
    now = (await client.get(BASE + "/search", params={"q": "격리 수준"}, headers=auth)).json()
    assert len(now["items"]) == 1 and now["items"][0]["revision"]["rev_no"] == 2
    history = (await client.get(BASE + "/search", params={"q": "격리 수준", "current_only": "false"},
                                headers=auth)).json()
    assert {h["revision"]["rev_no"] for h in history["items"]} == {1, 2}
    assert any(h["revision"]["current"] is False for h in history["items"])
    scoped = (await client.get(BASE + "/search", params={"q": "요리법", "document_id": other["document_id"]},
                               headers=auth)).json()
    assert scoped["items"] == []


async def test_search_pagination_and_degenerate_queries(client, auth):
    await register(client, auth, text="\n\n".join(f"격리 문단 {i}" for i in range(5)))
    first = (await client.get(BASE + "/search", params={"q": "격리", "limit": 2}, headers=auth)).json()
    assert len(first["items"]) == 2 and first["has_more"] is True
    last = (await client.get(BASE + "/search", params={"q": "격리", "limit": 2, "offset": 4}, headers=auth)).json()
    assert len(last["items"]) == 1 and last["has_more"] is False
    ids = {h["passage_id"] for h in first["items"]} | {h["passage_id"] for h in last["items"]}
    assert len(ids) == 3
    # 불용어·한 글자뿐인 질의는 빈 결과이며 전체를 돌려주지 않는다.
    blank = (await client.get(BASE + "/search", params={"q": "프로젝트 관련 a"}, headers=auth)).json()
    assert blank["items"] == [] and blank["terms"] == []
    assert (await client.get(BASE + "/search", params={"q": ""}, headers=auth)).status_code == 422
    # LIKE 메타문자는 문자 그대로 다룬다.
    assert (await client.get(BASE + "/search", params={"q": "%%"}, headers=auth)).json()["items"] == []


async def test_evidence_reports_original_position_and_hash(client, auth):
    doc = await register(client, auth)
    hits = (await client.get(BASE + "/search", params={"q": "요리법"}, headers=auth)).json()
    passage = hits["items"][0]
    ev = (await client.get(f"{BASE}/passages/{passage['passage_id']}/evidence", headers=auth)).json()
    assert ev["document"]["id"] == doc["document_id"]
    assert ev["quote"].startswith("둘째 문단")
    assert ev["span"]["revision_start"] == TEXT_V1.index("둘째")
    assert ev["span"]["line_start"] == 3 and ev["span"]["line_end"] == 3
    assert ev["span"]["hash_verified"] is True
    assert ev["before"].endswith("\n\n") and ev["after"].startswith("\n\n셋째")
    part = (await client.get(f"{BASE}/passages/{passage['passage_id']}/evidence",
                             params={"start": 3, "end": 5}, headers=auth)).json()
    assert part["quote"] == ev["quote"][3:5]
    assert part["span"]["revision_start"] == ev["span"]["revision_start"] + 3
    bad = await client.get(f"{BASE}/passages/{passage['passage_id']}/evidence",
                           params={"start": 5, "end": 2}, headers=auth)
    assert bad.status_code == 422
    too_far = await client.get(f"{BASE}/passages/{passage['passage_id']}/evidence",
                               params={"end": 9999}, headers=auth)
    assert too_far.status_code == 422
    assert (await client.get(f"{BASE}/passages/{uuid.uuid4()}/evidence", headers=auth)).status_code == 404


async def test_unsegmented_revision_becomes_searchable_after_backfill(client, auth):
    from ttakkari.kb_library import segment_unsegmented
    from ttakkari.kb_store import ingest_source

    async with sessionmaker()() as db:
        await ingest_source(db, source_key="file:legacy.md", data="레거시 문단은 격리 수준을 다룬다.\n".encode(), title="레거시")
        await db.commit()
    before = (await client.get(BASE + "/search", params={"q": "격리 수준"}, headers=auth)).json()
    assert before["items"] == []
    async with sessionmaker()() as db:
        assert await segment_unsegmented(db) == {"revisions_checked": 1, "passages": 1}
        await db.commit()
        assert await segment_unsegmented(db) == {"revisions_checked": 0, "passages": 0}
    after = (await client.get(BASE + "/search", params={"q": "격리 수준"}, headers=auth)).json()
    assert [h["document"]["title"] for h in after["items"]] == ["레거시"]


async def test_cli_kb_ingest_makes_the_document_searchable(client, auth, tmp_path):
    import asyncio
    import os
    import subprocess
    import sys
    from pathlib import Path

    sample = tmp_path / "cli.md"
    sample.write_text("CLI 로 수집한 문단은 체크포인트를 설명한다.\n", encoding="utf-8")
    env = {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src")}

    def run():
        return subprocess.run(
            [sys.executable, "-m", "ttakkari.cli", "kb-ingest", str(sample), "--source-key", "file:cli.md", "--title", "CLI 문서"],
            env=env, capture_output=True, text=True, timeout=120, check=False,
        )

    done = await asyncio.get_running_loop().run_in_executor(None, run)
    assert done.returncode == 0, done.stderr
    hits = (await client.get(BASE + "/search", params={"q": "체크포인트"}, headers=auth)).json()
    assert [h["document"]["title"] for h in hits["items"]] == ["CLI 문서"]
