from __future__ import annotations

from ttakkari.api.assistant import wait_indexing
from ttakkari.context.insight import duplicate_suspects, histogram, norm_name

P = "/api/assistant/library"


async def _seed(client, auth, make_dir):
    a = (await client.post("/api/assistant/entities", json={
        "name": "Alpha Project", "type": "project", "aliases": ["알파"], "summary": "알파 프로젝트"}, headers=auth)).json()
    b = (await client.post("/api/assistant/entities", json={
        "name": "alpha-project", "type": "tool", "summary": "비슷한 이름"}, headers=auth)).json()
    c = (await client.post("/api/assistant/entities", json={
        "name": "Corp", "type": "org", "scope": "work", "summary": "회사"}, headers=auth)).json()
    await client.post("/api/assistant/relations", json={"subject": "Alpha Project", "predicate": "uses", "object": "alpha-project"}, headers=auth)
    await client.post("/api/assistant/memories", json={"content": "알파는 키워드 검색을 쓴다", "entities": ["Alpha Project"]}, headers=auth)
    await client.post("/api/assistant/memories", json={"content": "연결 없는 기억"}, headers=auth)
    d = make_dir()
    (d / "one.md").write_text("# 구조\n알파 프로젝트는 키워드 검색으로 문서를 찾는다.\n\n## 짧음\n끝\n")
    (d / "two.md").write_text("# 구조\n알파 프로젝트는 키워드 검색으로 문서를 찾는다.\n")
    r = await client.post("/api/assistant/sources", json={
        "path": str(d), "entity_id": a["id"], "category": "projects"}, headers=auth)
    assert r.status_code == 201, r.text
    await wait_indexing()
    return a, b, c


def test_pure_helpers():
    assert norm_name("Skein  Design-System") == "skeindesignsystem"
    pairs = duplicate_suspects([
        {"id": "1", "name": "Skein", "aliases": []},
        {"id": "2", "name": "Skein Design System", "aliases": []},
        {"id": "3", "name": "skein", "aliases": []},
        {"id": "4", "name": "홈서버", "aliases": ["homeserver"]},
        {"id": "5", "name": "HomeServer", "aliases": []},
    ])
    found = {frozenset((p["a"], p["b"])) for p in pairs}
    assert frozenset(("1", "3")) in found and frozenset(("4", "5")) in found
    assert frozenset(("1", "2")) not in found
    h = histogram([10, 100, 5000])
    assert sum(b["count"] for b in h) == 3 and h[0]["count"] == 1 and h[-1]["count"] == 1


async def test_overview_tree_and_scope(client, auth, make_dir):
    await _seed(client, auth, make_dir)
    o = (await client.get(f"{P}/overview", headers=auth)).json()
    assert o["retrieval"] == "lexical"
    assert o["totals"]["documents"] == 2 and o["totals"]["entities"] == 3 and o["totals"]["memories"] == 2
    assert o["totals"]["relations"] == 1 and o["last_indexed_at"] and o["error_sources"] == []
    work = (await client.get(f"{P}/overview?scope=work", headers=auth)).json()
    assert work["totals"]["entities"] == 1 and work["totals"]["documents"] == 0
    t = (await client.get(f"{P}/tree", headers=auth)).json()
    cat = t["categories"][0]
    assert cat["category"] == "projects" and cat["groups"][0]["name"] == "Alpha Project"
    src = cat["groups"][0]["sources"][0]
    docs = (await client.get(f"{P}/sources/{src['id']}/documents", headers=auth)).json()
    assert docs["total"] == 2
    chunks = (await client.get(f"{P}/documents/{docs['documents'][0]['id']}/chunks", headers=auth)).json()
    assert chunks["chunks"] and chunks["chunks"][0]["length"] > 0
    assert (await client.get(f"{P}/sources/{src['id']}/documents?scope=work", headers=auth)).status_code == 404
    assert (await client.get(f"{P}/overview")).status_code == 401


async def test_graph_flags_and_entity_evidence(client, auth, make_dir):
    a, _b, c = await _seed(client, auth, make_dir)
    g = (await client.get(f"{P}/graph", headers=auth)).json()
    by = {n["name"]: n for n in g["nodes"]}
    assert len(g["edges"]) == 1 and g["edges"][0]["predicate"] == "uses"
    assert by["Corp"]["flags"]["isolated"] and by["Corp"]["flags"]["no_alias"] and by["Corp"]["flags"]["no_evidence"]
    assert not by["Alpha Project"]["flags"]["isolated"] and by["Alpha Project"]["memories"] == 1
    assert by["Alpha Project"]["documents"] == 2
    assert by["Alpha Project"]["flags"]["duplicate_suspect"] and by["alpha-project"]["flags"]["duplicate_suspect"]
    assert g["duplicates"] and g["predicates"] == {"uses": 1}
    ev = (await client.get(f"{P}/entities/{a['id']}", headers=auth)).json()
    assert ev["sources"][0]["documents"] == 2 and ev["memories"] and ev["relations"]
    assert (await client.get(f"{P}/entities/{c['id']}?scope=personal", headers=auth)).status_code == 404
    assert (await client.get(f"{P}/entities/{c['id']}?scope=work", headers=auth)).status_code == 200
    work_graph = (await client.get(f"{P}/graph?scope=work", headers=auth)).json()
    assert [n["name"] for n in work_graph["nodes"]] == ["Corp"]


async def test_quality_metrics(client, auth, make_dir):
    await _seed(client, auth, make_dir)
    q = (await client.get(f"{P}/quality", headers=auth)).json()
    assert q["chunks"]["total"] >= 2 and sum(b["count"] for b in q["chunks"]["histogram"]) == q["chunks"]["total"]
    assert q["chunks"]["short"] >= 1 and q["chunks"]["short_examples"][0]["length"] < q["chunks"]["short_limit"]
    assert q["duplicates"]["extra_chunks"] >= 1 and q["duplicates"]["examples"][0]["copies"] >= 2
    assert q["entities"]["no_alias"] == 2 and q["entities"]["duplicate_pairs"]
    assert q["memories"]["unlinked"] == 1 and q["documents"]["empty"] == 0
    codes = {f["code"]: f for f in q["findings"]}
    assert codes["duplicate_entities"]["level"] == "warn" and "no_alias" in codes


async def test_search_lab_explains_matches(client, auth, make_dir):
    await _seed(client, auth, make_dir)
    r = (await client.post(f"{P}/search-lab", json={"query": "알파 키워드", "scope": "all"}, headers=auth)).json()
    assert r["terms"] == ["알파", "키워드"] and r["anchors"] == ["Alpha Project"] and r["elapsed_ms"] >= 0
    chunk = next(x for x in r["results"] if x["type"] == "chunk")
    assert "키워드" in chunk["matched_terms"] and "본문" in chunk["match_in"] and chunk["via"] == "keyword"
    mem = next(x for x in r["results"] if x["type"] == "memory")
    assert mem["entities"] == ["Alpha Project"]
    empty = (await client.post(f"{P}/search-lab", json={"query": "zzzqqq없는말"}, headers=auth)).json()
    assert empty["results"] == []
    work = (await client.post(f"{P}/search-lab", json={"query": "알파 키워드", "scope": "work"}, headers=auth)).json()
    assert all(x["type"] != "chunk" for x in work["results"])


async def test_search_lab_pages_past_ten_and_filters_scope(client, auth):
    for i in range(23):
        response = await client.post("/api/assistant/memories", json={
            "content": f"페이지네이션 항목 페이지테스트 {i:02d}", "scope": "personal"}, headers=auth)
        assert response.status_code == 201
    for i in range(3):
        response = await client.post("/api/assistant/memories", json={
            "content": f"페이지네이션 항목 페이지테스트 회사 {i:02d}", "scope": "work"}, headers=auth)
        assert response.status_code == 201

    page1 = (await client.post(f"{P}/search-lab", json={
        "query": "페이지테스트", "scope": "all", "limit": 10, "offset": 0}, headers=auth)).json()
    page2 = (await client.post(f"{P}/search-lab", json={
        "query": "페이지테스트", "scope": "all", "limit": 10, "offset": 10}, headers=auth)).json()
    assert page1["total"] == 26
    assert len(page1["results"]) == len(page2["results"]) == 10
    assert {x["id"] for x in page1["results"]}.isdisjoint({x["id"] for x in page2["results"]})

    work = (await client.post(f"{P}/search-lab", json={
        "query": "페이지테스트", "scope": "work", "limit": 10, "offset": 0}, headers=auth)).json()
    assert work["total"] == 3


async def test_library_item_detail_has_full_memory_and_related_data(client, auth, make_dir):
    entity = (await client.post("/api/assistant/entities", json={
        "name": "상세 프로젝트", "type": "project", "scope": "personal"}, headers=auth)).json()
    await client.post("/api/assistant/entities", json={
        "name": "연결 도구", "type": "tool", "scope": "personal"}, headers=auth)
    await client.post("/api/assistant/relations", json={
        "subject": "상세 프로젝트", "predicate": "uses", "object": "연결 도구"}, headers=auth)
    content = "상세 본문 " * 40
    memory = (await client.post("/api/assistant/memories", json={
        "content": content, "entities": ["상세 프로젝트"], "source_ref": "/docs/detail.md"}, headers=auth)).json()

    response = await client.get(f"{P}/items/memory/{memory['id']}", headers=auth)
    assert response.status_code == 200
    detail = response.json()
    assert detail["content"] == content.strip()
    assert detail["source_ref"] == "/docs/detail.md"
    assert detail["created_at"] and detail["updated_at"]
    assert detail["entities"][0]["name"] == entity["name"]
    assert detail["entities"][0]["relations"][0]["predicate"] == "uses"

    root = make_dir()
    body = "문서 조각의 전체 본문 " * 80
    (root / "detail.md").write_text(f"# 상세\n{body}")
    source = await client.post("/api/assistant/sources", json={
        "path": str(root), "entity_id": entity["id"], "category": "projects"}, headers=auth)
    assert source.status_code == 201
    await wait_indexing()
    docs = (await client.get(f"{P}/sources/{source.json()['id']}/documents", headers=auth)).json()
    chunks = (await client.get(f"{P}/documents/{docs['documents'][0]['id']}/chunks?limit=50", headers=auth)).json()
    chunk = chunks["chunks"][0]
    response = await client.get(f"{P}/items/chunk/{chunk['id']}", headers=auth)
    assert response.status_code == 200
    chunk_detail = response.json()
    assert len(chunk_detail["content"]) > 700
    assert chunk["content"] == chunk_detail["content"][:700]
    assert chunk_detail["source_path"] == str(root)
    assert chunk_detail["category"] == "projects" and chunk_detail["project"] == "상세 프로젝트"
    assert chunk_detail["created_at"] and chunk_detail["updated_at"]

    entity_detail = (await client.get(f"{P}/items/entity/{entity['id']}", headers=auth)).json()
    assert entity_detail["created_at"] and entity_detail["updated_at"]
