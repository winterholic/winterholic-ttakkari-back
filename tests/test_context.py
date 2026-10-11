"""ADR 0007: 비서 맥락(온톨로지 + RAG), 에이전트 도구 API, 비서 관리 API, 시스템 프롬프트 주입."""
from __future__ import annotations

from conftest import ALLOWED, HOME, agent_calls
from helpers import run_to_end

from ttakkari.context.indexer import chunk_markdown
from ttakkari.orchestrator.runtime import issue_token


async def _session(client, auth, make_dir, **body) -> dict:
    root = make_dir()
    ws = (await client.post("/api/workspaces", json={"name": root.name, "root_path": str(root)}, headers=auth)).json()
    return (await client.post("/api/sessions", json={"workspace_id": ws["id"], **body}, headers=auth)).json()


def _agent(sid: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {issue_token(sid)}"}


# ---- 온톨로지·기억

async def test_remember_recall_and_entities_via_assistant_api(client, auth):
    r = await client.post("/api/assistant/memories", json={"content": "백엔드는 FastAPI 를 주로 쓴다", "kind": "preference",
                                                          "entities": ["FastAPI"]}, headers=auth)
    assert r.status_code == 201
    await client.post("/api/assistant/memories", json={"content": "점심은 김치찌개를 좋아한다"}, headers=auth)
    await client.post("/api/assistant/relations", json={"subject": "winterholic", "predicate": "works on", "object": "Ttakkari"},
                      headers=auth)
    hits = (await client.post("/api/assistant/recall", json={"query": "FastAPI 백엔드"}, headers=auth)).json()["results"]
    # FastAPI 엔터티와 그 기억이 맨 앞에 온다(김치찌개 기억보다).
    top = [h for h in hits if h["type"] == "memory"]
    assert "FastAPI" in top[0]["text"]
    assert {h["type"] for h in hits[:2]} <= {"memory", "entity"} and all("김치찌개" not in h["text"] for h in hits[:2])
    ents = {e["name"]: e for e in (await client.get("/api/assistant/entities", headers=auth)).json()["entities"]}
    assert {"FastAPI", "winterholic", "Ttakkari"} <= set(ents)
    d = (await client.get(f"/api/assistant/entities/{ents['winterholic']['id']}", headers=auth)).json()
    assert d["relations"] == [{"subject": "winterholic", "predicate": "works_on", "object": "Ttakkari", "note": None}]
    fa = (await client.get(f"/api/assistant/entities/{ents['FastAPI']['id']}", headers=auth)).json()
    assert fa["memories"][0]["content"] == "백엔드는 FastAPI 를 주로 쓴다"


async def test_entity_recall_carries_relations(client, auth):
    await client.post("/api/assistant/entities", json={"name": "Ttakkari", "type": "project", "summary": "원격 AI 에이전트 PWA"},
                      headers=auth)
    await client.post("/api/assistant/relations", json={"subject": "winterholic", "predicate": "works_on", "object": "Ttakkari"},
                      headers=auth)
    hits = (await client.post("/api/assistant/recall", json={"query": "Ttakkari"}, headers=auth)).json()["results"]
    ent = next(h for h in hits if h["type"] == "entity")
    assert "원격 AI 에이전트 PWA" in ent["text"] and "winterholic works_on Ttakkari" in ent["text"]


async def test_memory_pin_edit_delete(client, auth):
    m = (await client.post("/api/assistant/memories", json={"content": "존댓말 대신 반말로 답한다", "kind": "instruction"},
                           headers=auth)).json()
    r = await client.patch(f"/api/assistant/memories/{m['id']}", json={"pinned": True, "content": "반말로 짧게 답한다"}, headers=auth)
    assert r.json()["pinned"] and r.json()["content"] == "반말로 짧게 답한다"
    st = (await client.get("/api/assistant/status", headers=auth)).json()
    assert "반말로 짧게 답한다" in st["brief"] and st["counts"]["memories"] == 1 and st["embedder"] == "hash"
    assert (await client.delete(f"/api/assistant/memories/{m['id']}", headers=auth)).status_code == 204
    assert (await client.get("/api/assistant/memories", headers=auth)).json()["memories"] == []


# ---- 지침·고정 기억이 모든 Run 에 들어간다

async def test_instructions_and_pinned_facts_reach_every_run(client, auth, make_dir):
    r = await client.put("/api/assistant/instructions", json={"text": "항상 한국어 반말로 답한다."}, headers=auth)
    assert r.status_code == 200 and (HOME / "harness" / "instructions.md").read_text() == "항상 한국어 반말로 답한다."
    await client.post("/api/assistant/memories", json={"content": "사용자는 단순한 쪽을 선호한다", "pinned": True}, headers=auth)
    sess = await _session(client, auth, make_dir)
    await run_to_end(client, auth, sess["id"], "hi")
    argv = agent_calls()[-1]["argv"]
    note = argv[argv.index("--append-system-prompt") + 1]
    assert "항상 한국어 반말로 답한다." in note and "사용자는 단순한 쪽을 선호한다" in note
    assert (await client.get("/api/assistant/instructions", headers=auth)).json()["text"] == "항상 한국어 반말로 답한다."
    (HOME / "harness" / "instructions.md").unlink()


# ---- RAG 색인

def test_chunk_markdown_groups_short_subsections_under_their_section():
    text = ("## 1206 설정 키 논의\n### 한줄 요약\n키는 안 쓰기로.\n### 작업 내용\n코드 변경 없음.\n"
            "## 1227 워크스페이스\n### 한줄 요약\n홈 전체는 위험.\n")
    pieces = chunk_markdown(text)
    # 앞 절 제목이 뒤 절에 쌓이지 않고, 짧은 하위 절은 상위 절 하나로 합쳐진다.
    assert [h for h, _ in pieces] == ["1206 설정 키 논의", "1227 워크스페이스"]
    assert "[한줄 요약]\n키는 안 쓰기로." in pieces[0][1] and "[작업 내용]\n코드 변경 없음." in pieces[0][1]


def test_chunk_markdown_nested_trail_and_long_split():
    text = "# 일지\n## 10-11\n오늘.\n### 결정\n엔진은 Claude 우선.\n## 10-12\n" + ("문단입니다. " * 120 + "\n\n") * 3
    pieces = chunk_markdown(text)
    assert pieces[0][0] == "일지 > 10-11" and "[결정]" in pieces[0][1]
    assert all(h == "일지 > 10-12" for h, _ in pieces[1:]) and len(pieces) > 2


async def test_source_indexing_is_incremental_and_searchable(client, auth):
    from ttakkari.api.assistant import wait_indexing

    d = ALLOWED / "notes"
    d.mkdir()
    (d / "2026-10-11.md").write_text("# 10-11\n## 오케스트레이터\n메인 오케스트레이터가 작업 세션에 일을 맡기는 구조로 바꿨다.\n")
    (d / ".hidden.md").write_text("# 숨김\n보이면 안 된다")
    (d / "secret.env").write_text("TOKEN=x")
    r = await client.post("/api/assistant/sources", json={"path": str(d)}, headers=auth)
    assert r.status_code == 201, r.text
    await wait_indexing()
    src = (await client.get("/api/assistant/sources", headers=auth)).json()["sources"][0]
    assert src["documents"] == 1 and src["chunks"] == 1 and src["last_indexed_at"]
    hits = (await client.post("/api/assistant/recall", json={"query": "오케스트레이터 작업 세션"}, headers=auth)).json()["results"]
    assert hits[0]["type"] == "chunk" and hits[0]["path"].endswith("2026-10-11.md") and hits[0]["heading"] == "10-11 > 오케스트레이터"
    # 파일을 고치면 그 파일만 다시, 지우면 빠진다.
    (d / "2026-10-12.md").write_text("# 10-12\n파일 보내기를 만들었다.\n")
    (d / "2026-10-11.md").unlink()
    await client.post(f"/api/assistant/sources/{src['id']}/reindex", headers=auth)
    await wait_indexing()
    src = (await client.get("/api/assistant/sources", headers=auth)).json()["sources"][0]
    assert src["documents"] == 1
    hits = (await client.post("/api/assistant/recall", json={"query": "파일 보내기"}, headers=auth)).json()["results"]
    assert hits[0]["path"].endswith("2026-10-12.md")


async def test_source_path_rules(client, auth, outside_dir):
    for bad in (str(outside_dir), str(HOME), "relative", str(ALLOWED / "nope")):
        r = await client.post("/api/assistant/sources", json={"path": bad}, headers=auth)
        assert r.status_code == 400, (bad, r.text)


# ---- 에이전트 도구 API

async def test_agent_tools_remember_recall_link_and_forget_rules(client, auth, make_dir):
    sess = await _session(client, auth, make_dir)
    h = _agent(sess["id"])
    m = (await client.post("/api/agent/remember", json={"content": "사용자는 아이폰을 주로 쓴다", "entities": ["아이폰"]}, headers=h)).json()
    hits = (await client.post("/api/agent/recall", json={"query": "아이폰"}, headers=h)).json()["results"]
    assert any(x["type"] == "memory" and x["source"] == "agent" for x in hits)
    assert (await client.post("/api/agent/link", json={"subject": "winterholic", "predicate": "uses", "object": "아이폰"},
                              headers=h)).status_code == 200
    ent = (await client.get("/api/agent/entities/아이폰", headers=h)).json()
    assert ent["relations"][0]["predicate"] == "uses"
    assert (await client.post("/api/agent/forget", json={"id": m["id"]}, headers=h)).json() == {"forgotten": True}
    manual = (await client.post("/api/assistant/memories", json={"content": "사용자가 넣은 기억"}, headers=auth)).json()
    assert (await client.post("/api/agent/forget", json={"id": manual["id"]}, headers=h)).status_code == 403
    # 사용자 토큰으로는 에이전트 API 를 못 쓴다.
    assert (await client.post("/api/agent/recall", json={"query": "x"}, headers=auth)).status_code == 401


async def test_agent_share_file_and_sessions(client, auth, make_dir, outside_dir):
    root = make_dir()
    ws = (await client.post("/api/workspaces", json={"name": "w", "root_path": str(root)}, headers=auth)).json()
    sess = (await client.post("/api/sessions", json={"workspace_id": ws["id"]}, headers=auth)).json()
    await run_to_end(client, auth, sess["id"], "hello")
    h = _agent(sess["id"])
    (root / "report.md").write_text("# 보고서")
    r = await client.post("/api/agent/share", json={"path": "report.md"}, headers=h)
    assert r.status_code == 200 and r.json()["artifact"]["filename"] == "report.md"
    (outside_dir / "x.md").write_text("x")
    assert (await client.post("/api/agent/share", json={"path": str(outside_dir / "x.md")}, headers=h)).status_code == 403
    (root / ".env").write_text("A=1")
    assert (await client.post("/api/agent/share", json={"path": ".env"}, headers=h)).status_code == 403
    arts = (await client.get("/api/agent/artifacts", params={"q": "report"}, headers=h)).json()["artifacts"]
    assert arts[0]["path"].endswith("report.md")
    ss = (await client.get("/api/agent/sessions", headers=h)).json()["sessions"]
    assert sess["id"] in [s["id"] for s in ss]
    turns = (await client.get(f"/api/agent/sessions/{sess['id']}", headers=h)).json()["turns"]
    assert turns[0]["prompt"] == "hello" and turns[0]["result"] == "done"


async def test_tools_listing_matches_mcp_server(client, auth):
    from test_orchestrator import AGENT_TOOLS, WORKER_TOOLS

    listed = {t["name"] for t in (await client.get("/api/assistant/tools", headers=auth)).json()["tools"]}
    assert listed == AGENT_TOOLS | WORKER_TOOLS
