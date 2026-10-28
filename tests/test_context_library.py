from __future__ import annotations

from conftest import ALLOWED

from ttakkari.api.assistant import wait_indexing


async def test_library_never_loads_an_embedding_model(client, auth, monkeypatch):
    import sys

    from ttakkari.context import indexer, store

    async def forbidden(*args, **kwargs):
        raise AssertionError("embedding must not run")

    monkeypatch.setitem(sys.modules, "fastembed", None)
    monkeypatch.setattr(store, "embed", forbidden, raising=False)
    monkeypatch.setattr(indexer, "embed", forbidden, raising=False)
    r = await client.post("/api/assistant/entities", json={"name": "Library", "type": "project"}, headers=auth)
    assert r.status_code == 201
    r = await client.post("/api/assistant/memories", json={"content": "Library는 모델 없이 검색한다"}, headers=auth)
    assert r.status_code == 201
    hits = (await client.post("/api/assistant/recall", json={"query": "모델 없이 검색"}, headers=auth)).json()["results"]
    assert any(h["type"] == "memory" for h in hits)
    status = (await client.get("/api/assistant/status", headers=auth)).json()
    assert status["embedder"] == "none" and status["retrieval"] == "lexical" and status["model"] is None


async def test_alias_resolves_project_documents_without_keyword_overlap(client, auth):
    e = (await client.post("/api/assistant/entities", json={
        "name": "winterholic-ttakkari", "type": "project", "aliases": ["따까리", "TTAKKARI"],
        "summary": "원격 비서", "attributes": {"checked_at": "2026-10-12"},
    }, headers=auth)).json()
    d = ALLOWED / "project-docs"
    d.mkdir()
    (d / "architecture.md").write_text("# 실행 구조\n스마트폰에서 작업을 맡기고 결과를 받는다.\n")
    r = await client.post("/api/assistant/sources", json={
        "path": str(d), "entity_id": e["id"], "category": "projects", "scope": "personal",
    }, headers=auth)
    assert r.status_code == 201, r.text
    await wait_indexing()
    hits = (await client.post("/api/assistant/recall", json={"query": "따까리 구조"}, headers=auth)).json()["results"]
    chunks = [h for h in hits if h["type"] == "chunk"]
    assert chunks and chunks[0]["project"] == "winterholic-ttakkari"
    assert chunks[0]["path"].endswith("architecture.md") and chunks[0]["indexed_at"]
    from ttakkari.context import store
    from ttakkari.db import sessionmaker

    async with sessionmaker()() as db:
        alias = await store.find_entity(db, "ttakkari")
        assert str(alias.id) == e["id"]
        assert alias.attributes["checked_at"] == "2026-10-12"


async def test_recall_scope_and_disabled_sources(client, auth):
    for scope, content in (("personal", "개인 검색표식"), ("work", "회사 검색표식")):
        r = await client.post("/api/assistant/memories", json={"content": content, "scope": scope}, headers=auth)
        assert r.status_code == 201
    hits = (await client.post("/api/assistant/recall", json={"query": "검색표식", "scope": "work"}, headers=auth)).json()["results"]
    assert any("회사" in h["text"] for h in hits)
    assert all("개인" not in h["text"] for h in hits)
    d = ALLOWED / "disabled"
    d.mkdir()
    (d / "doc.md").write_text("# 비활성표식\n비활성표식 검색 자료\n")
    r = await client.post("/api/assistant/sources", json={"path": str(d)}, headers=auth)
    await wait_indexing()
    import uuid

    from ttakkari.db import sessionmaker
    from ttakkari.models import CtxSource

    async with sessionmaker()() as db:
        src = await db.get(CtxSource, uuid.UUID(r.json()["id"]))
        src.enabled = False
        await db.commit()
    hits = (await client.post("/api/assistant/recall", json={"query": "비활성표식"}, headers=auth)).json()["results"]
    assert not hits


async def test_unrelated_query_returns_no_context(client, auth):
    await client.post("/api/assistant/memories", json={"content": "FastAPI 백엔드 개발"}, headers=auth)
    hits = (await client.post("/api/assistant/recall", json={"query": "양자색역학 중성미자"}, headers=auth)).json()["results"]
    assert hits == []


async def test_index_excludes_generated_files_and_symlink_escape(client, auth, outside_dir):
    d = ALLOWED / "safe-corpus"
    d.mkdir()
    (d / "README.md").write_text("# 정상\n정상 검색 자료")
    (d / "node_modules").mkdir()
    (d / "node_modules" / "README.md").write_text("# 의존성\n제외 대상")
    outside = outside_dir / "outside.md"
    outside.write_text("# 외부\n제외 대상")
    (d / "escape.md").symlink_to(outside)
    await client.post("/api/assistant/sources", json={"path": str(d)}, headers=auth)
    await wait_indexing()
    sources = (await client.get("/api/assistant/sources", headers=auth)).json()["sources"]
    assert sources[0]["documents"] == 1


async def test_import_catalog_is_idempotent_and_preserves_manual_memory(client, auth):
    from ttakkari.context.library import import_catalog
    from ttakkari.db import sessionmaker

    r = await client.post("/api/assistant/memories", json={"content": "사용자 원문은 보존"}, headers=auth)
    manual_id = r.json()["id"]
    doc = ALLOWED / "project-card.md"
    doc.write_text("# 테스트 프로젝트\n명시한 경로에서 작업한다.\n")
    data = {"projects": [{"name": "Sample", "aliases": ["샘플"], "scope": "personal",
                          "summary": "검증용 프로젝트", "attributes": {"checked_at": "2026-10-12"},
                          "sources": [{"path": str(doc)}],
                          "facts": [{"key": "purpose", "content": "Sample은 검색을 검증한다", "source_ref": str(doc)}]}],
            "relations": []}
    async with sessionmaker()() as db:
        await import_catalog(db, data)
        await import_catalog(db, data)
    memories = (await client.get("/api/assistant/memories", headers=auth)).json()["memories"]
    assert len(memories) == 2 and any(m["id"] == manual_id for m in memories)
    assert len((await client.get("/api/assistant/sources", headers=auth)).json()["sources"]) == 1
    hits = (await client.post("/api/assistant/recall", json={"query": "샘플"}, headers=auth)).json()["results"]
    assert {"entity", "memory", "chunk"} <= {h["type"] for h in hits}


async def test_company_worker_recall_crosses_groups_while_dispatch_brief_stays_grouped(client, auth, make_dir):
    from conftest import agent_calls
    from helpers import run_to_end
    from test_context import _agent, _session

    for scope in ("personal", "work"):
        await client.post("/api/assistant/memories", json={"content": f"{scope} 공통표식",
                                                         "scope": scope, "pinned": True}, headers=auth)
    session = await _session(client, auth, make_dir, pool="work")
    from ttakkari.db import sessionmaker
    from ttakkari.models import EngineAccount

    async with sessionmaker()() as db:
        db.add(EngineAccount(name="company", engine="claude", pool="work", enabled=True, priority=0))
        await db.commit()
    h = _agent(session["id"])
    hits = (await client.post("/api/agent/recall", json={"query": "공통표식"}, headers=h)).json()["results"]
    assert any("work" in x["text"] for x in hits)
    assert any("personal" in x["text"] for x in hits)
    await run_to_end(client, auth, session["id"], "hello")
    argv = agent_calls()[-1]["argv"]
    note = argv[argv.index("--append-system-prompt") + 1]
    # 실행 주입 기본값은 분류별 유지고, 막힌 접근이 아니라 recall 로 언제든 더 가져온다.
    assert "work 공통표식" in note and "personal 공통표식" not in note
    await client.post("/api/agent/remember", json={"content": "회사에서 저장한 기억"}, headers=h)
    memories = (await client.get("/api/assistant/memories", headers=auth)).json()["memories"]
    assert next(m for m in memories if m["content"] == "회사에서 저장한 기억")["scope"] == "work"


async def test_failed_file_read_keeps_previous_chunks_and_reports_error(client, auth, monkeypatch):
    from pathlib import Path

    from ttakkari.context import indexer

    d = ALLOWED / "error-source"
    d.mkdir()
    f = d / "doc.md"
    f.write_text("# 기존자료\n기존자료는 보존한다")
    r = await client.post("/api/assistant/sources", json={"path": str(d)}, headers=auth)
    await wait_indexing()
    original = Path.read_bytes

    def fail_one(path):
        if path == f.resolve():
            raise PermissionError("read denied")
        return original(path)

    monkeypatch.setattr(Path, "read_bytes", fail_one)
    await client.post(f"/api/assistant/sources/{r.json()['id']}/reindex", headers=auth)
    await wait_indexing()
    sources = (await client.get("/api/assistant/sources", headers=auth)).json()["sources"]
    assert sources[0]["documents"] == 1 and "read denied" in sources[0]["last_error"]
    monkeypatch.setattr(Path, "read_bytes", original)
    monkeypatch.setattr(indexer, "MAX_FILES", 1)
    (d / "second.md").write_text("# 추가자료\n내용")
    await client.post(f"/api/assistant/sources/{r.json()['id']}/reindex", headers=auth)
    await wait_indexing()
    sources = (await client.get("/api/assistant/sources", headers=auth)).json()["sources"]
    assert sources[0]["documents"] == 1 and "상한" in sources[0]["last_error"]


async def test_library_catalog_lists_project_landmarks_with_scope(client, auth, make_dir):
    from test_context import _agent, _session

    for scope in ("personal", "work"):
        await client.post("/api/assistant/entities", json={"name": f"{scope}-project", "type": "project",
                                                         "summary": "프로젝트 이정표", "scope": scope}, headers=auth)
    result = await client.get("/api/assistant/library", headers=auth)
    assert result.status_code == 200
    assert len(result.json()["projects"]) == 2
    session = await _session(client, auth, make_dir, pool="work")
    result = await client.get("/api/agent/library", headers=_agent(session["id"]))
    assert result.status_code == 200
    assert {p["name"] for p in result.json()["projects"]} == {"personal-project", "work-project"}


async def test_library_catalog_indexes_knowledge_collections_by_scope(client, auth, make_dir):
    from test_context import _agent, _session

    for scope, name in (("shared", "공용 스킬"), ("work", "사내 스킬"), ("personal", "개인 스킬")):
        e = (await client.post("/api/assistant/entities", json={"name": name, "type": "topic", "scope": scope,
                                                              "summary": "스킬 모음"}, headers=auth)).json()
        d = make_dir()
        (d / "SKILL.md").write_text("# 스킬\n설명")
        s = await client.post("/api/assistant/sources", json={"path": str(d), "scope": scope, "category": "knowledge",
                                                            "entity_id": e["id"]}, headers=auth)
        assert s.status_code in (200, 201), s.text
    await wait_indexing()
    result = (await client.get("/api/assistant/library", headers=auth)).json()
    assert {c["name"] for c in result["collections"]} == {"공용 스킬", "사내 스킬", "개인 스킬"}
    session = await _session(client, auth, make_dir, pool="work")
    result = (await client.get("/api/agent/library", headers=_agent(session["id"]))).json()
    assert {c["name"] for c in result["collections"]} == {"공용 스킬", "사내 스킬", "개인 스킬"}
