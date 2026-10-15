"""ADR 0010: 회의록 파이프라인. 분석 Run 은 가짜 엔진(FAKE_MEETING 마커)으로 돌린다."""
from __future__ import annotations

import asyncio
import time
import uuid

import pytest
from conftest import agent_calls
from sqlalchemy import select

from ttakkari.config import get_settings
from ttakkari.context import store
from ttakkari.db import sessionmaker
from ttakkari.meetings import providers
from ttakkari.meetings.schema import AnalysisError, extract_json, parse_analysis
from ttakkari.models import ChatSession, CtxMemory, Meeting, MeetingItem
from ttakkari.orchestrator.runtime import issue_token

TRANSCRIPT = "민수: 목요일에 내자. FAKE_MEETING 지연: 모바일 먼저 하고 데이터 이전은 다음 주에 보자."
DONE = {"review", "analyzed", "failed"}


async def _project(name="Alpha", scope="personal", **kw) -> str:
    async with sessionmaker()() as db:
        e = await store.upsert_entity(db, name, "project", scope=scope, summary="테스트 프로젝트", **kw)
        await db.commit()
        return str(e.id)


async def _memory(content, entity="Alpha", kind="decision", source="manual", scope="personal") -> str:
    async with sessionmaker()() as db:
        m = await store.remember(db, content, kind=kind, entities=[entity], source=source, scope=scope)
        await db.commit()
        return str(m.id)


async def _create(client, auth, **body) -> dict:
    body.setdefault("title", "주간 회의")
    r = await client.post("/api/meetings", json=body, headers=auth)
    assert r.status_code == 201, r.text
    return r.json()


async def _until(client, auth, mid: str, states=DONE, timeout=20.0) -> dict:
    deadline = time.monotonic() + timeout
    while True:
        m = (await client.get(f"/api/meetings/{mid}", headers=auth)).json()
        if m["status"] in states:
            return m
        assert time.monotonic() < deadline, f"회의 {mid} 가 {timeout}초 안에 끝나지 않음: {m['status']}"
        await asyncio.sleep(0.1)


async def _analyze(client, auth, mid: str, **kw) -> dict:
    r = await client.post(f"/api/meetings/{mid}/analyze", headers=auth)
    assert r.status_code == 202, r.text
    return await _until(client, auth, mid, **kw)


def _kinds(m: dict, kind: str) -> list[dict]:
    return [i for i in m["items"] if i["kind"] == kind]


async def _memories(source="meeting") -> list[CtxMemory]:
    async with sessionmaker()() as db:
        return list(await db.scalars(select(CtxMemory).where(CtxMemory.source == source)))


# ---- 스키마

def test_extract_json_prefers_last_fence_and_falls_back_to_braces():
    assert extract_json('앞\n```json\n{"a": 1}\n```\n뒤\n```json\n{"a": 2}\n```') == {"a": 2}
    assert extract_json('결과: {"a": 3} 끝') == {"a": 3}
    with pytest.raises(AnalysisError):
        extract_json("JSON 없음")


def test_parse_analysis_validates_shape_and_limits():
    ok = parse_analysis('{"summary": " 요약 ", "decisions": [{"text": "결정", "conflicts": [{"ref": "M1", "reason": "이유"}]}]}')
    assert ok.summary == "요약" and ok.decisions[0].conflicts[0].ref == "M1" and ok.action_items == []
    for bad in ('{"summary": ""}', '{"summary": "x", "decisions": [{"text": ""}]}',
                '{"summary": "x", "decisions": [{"text": "t", "conflicts": [{"ref": "Z1", "reason": "r"}]}]}',
                '{"decisions": []}', "", None):
        with pytest.raises(AnalysisError):
            parse_analysis(bad)


# ---- 전체 흐름: 전사 → 분석 → 구조화 → 충돌 제안 → 승인 반영

async def test_pipeline_text_to_conflict_proposals_and_approval(client, auth):
    pid = await _project()
    old = await _memory("[기존] 릴리스는 금요일에 한다")
    # 1) 첫 회의: 기존 기억 M1 만 후보다. 액션은 열린 채로 남는다.
    first = await _create(client, auth, project_id=pid, transcript=TRANSCRIPT)
    assert first["status"] == "transcribed" and first["source"] == "text" and first["transcript_chars"] == len(TRANSCRIPT)
    first = await _analyze(client, auth, first["id"])
    assert first["status"] == "review" and first["summary"]
    assert [i["text"] for i in _kinds(first, "action")] == ["배포 체크리스트를 작성한다"]
    assert _kinds(first, "open_issue") and _kinds(first, "discussion") and len(_kinds(first, "decision")) == 2
    # 첫 결정은 기존 기억 M1 과 충돌해 상충 제안이 되고, 둘째는 단순 추가 제안이다
    assert {p["kind"] for p in first["proposals"]} == {"supersede_memory", "add_memory"}
    sup = next(p for p in first["proposals"] if p["kind"] == "supersede_memory")
    assert sup["payload"]["targets"] == [{"id": old, "content": "[기존] 릴리스는 금요일에 한다"}] and "금요일" in sup["reason"]
    # 승인 전에는 맥락 도서관이 바뀌지 않는다
    assert await _memories("meeting") == []
    async with sessionmaker()() as db:
        assert (await db.get(CtxMemory, uuid.UUID(old))).archived_at is None
    # 후보 맥락이 프롬프트에 [M1] 로 들어갔다
    run = (await client.get(f"/api/runs/{first['analysis_run_id']}", headers=auth)).json()
    assert "[M1] (decision) [기존] 릴리스는 금요일에 한다" in run["prompt"] and TRANSCRIPT in run["prompt"]

    # 2) 두 번째 회의: 첫 회의의 열린 액션 A1 이 후보에 들어가 취소 제안이 생긴다
    second = await _create(client, auth, title="후속 회의", project_id=pid, transcript=TRANSCRIPT)
    second = await _analyze(client, auth, second["id"])
    kinds = sorted(p["kind"] for p in second["proposals"])
    assert kinds == ["add_memory", "supersede_memory", "update_action_item"]
    upd = next(p for p in second["proposals"] if p["kind"] == "update_action_item")
    assert upd["payload"]["change"] == "cancel" and upd["payload"]["target_meeting_title"] == "주간 회의"
    # 없는 참조(M99)는 버려진다
    dec = _kinds(second, "decision")[0]
    assert {c["ref"] for c in dec["conflicts"]} == {"M1", "A1"}
    first_action = _kinds(first, "action")[0]["id"]

    # 승인 반영: 기존 기억은 보관되고 새 결정이 같은 프로젝트 기억으로 생긴다
    sup = next(p for p in second["proposals"] if p["kind"] == "supersede_memory")
    r = await client.post(f"/api/meetings/{second['id']}/proposals/{sup['id']}/approve", headers=auth)
    assert r.status_code == 200, r.text
    mems = await _memories("meeting")
    assert len(mems) == 1 and mems[0].kind == "decision" and mems[0].scope == "personal" and str(pid) in mems[0].entity_ids
    assert "릴리스는 목요일로 앞당긴다" in mems[0].content and "'후속 회의'" in mems[0].content
    assert mems[0].source_ref.startswith(f"meeting:{second['id']}#")
    async with sessionmaker()() as db:
        assert (await db.get(CtxMemory, uuid.UUID(old))).archived_at is not None
    hits = await client.post("/api/assistant/recall", json={"query": "릴리스 목요일"}, headers=auth)
    assert any(h["type"] == "memory" and h["source"] == "meeting" for h in hits.json()["results"])

    r = await client.post(f"/api/meetings/{second['id']}/proposals/{upd['id']}/approve", headers=auth)
    assert r.status_code == 200
    prior = (await client.get(f"/api/meetings/{first['id']}", headers=auth)).json()
    assert next(i for i in prior["items"] if i["id"] == first_action)["status"] == "cancelled"
    # 남은 제안 거절 → 맥락에 추가되지 않고, 대기 제안이 없으면 analyzed
    add = next(p for p in second["proposals"] if p["kind"] == "add_memory")
    r = await client.post(f"/api/meetings/{second['id']}/proposals/{add['id']}/reject", headers=auth)
    done = r.json()
    assert done["status"] == "analyzed" and done["counts"]["pending_proposals"] == 0
    assert {p["status"] for p in done["proposals"]} == {"approved", "rejected"}
    assert len(await _memories("meeting")) == 1
    # 처리한 제안은 다시 처리할 수 없다
    assert (await client.post(f"/api/meetings/{second['id']}/proposals/{add['id']}/approve", headers=auth)).status_code == 409
    # 재조회·재분석 보호: reconcile 은 멱등이라 항목이 늘지 않는다
    again = (await client.get(f"/api/meetings/{second['id']}", headers=auth)).json()
    assert len(again["items"]) == len(second["items"])


async def test_reanalysis_replaces_items_and_pending_proposals_but_keeps_resolved(client, auth):
    pid = await _project()
    m = await _create(client, auth, project_id=pid, transcript=TRANSCRIPT)
    m = await _analyze(client, auth, m["id"])
    pending_before = {p["id"] for p in m["proposals"]}
    kept = m["proposals"][0]
    await client.post(f"/api/meetings/{m['id']}/proposals/{kept['id']}/reject", headers=auth)
    old_items = {i["id"] for i in m["items"]}
    m = await _analyze(client, auth, m["id"])
    by_id = {p["id"]: p for p in m["proposals"]}
    assert by_id[kept["id"]]["status"] == "rejected"  # 처리한 제안은 남는다
    assert not ((pending_before - {kept["id"]}) & set(by_id))  # 대기 제안은 새로 만들어졌다
    assert not (old_items & {i["id"] for i in m["items"]}) and len(_kinds(m, "decision")) == 2
    assert len(m["items"]) == len(_kinds(m, "discussion")) + 2 + 1 + 1  # 논의 + 결정 + 액션 + 쟁점(중복 저장 없음)


async def test_invalid_analysis_output_fails_without_saving_and_can_retry(client, auth):
    m = await _create(client, auth, transcript="FAKE_MEETING FAKE_MEETING_BADJSON 회의")
    m = await _analyze(client, auth, m["id"])
    assert m["status"] == "failed" and m["failed_stage"] == "analysis" and "JSON" in m["error"] and m["items"] == []
    # 전사를 고쳐 다시 분석할 수 있다
    await client.post(f"/api/meetings/{m['id']}/transcript", json={"text": TRANSCRIPT}, headers=auth)
    m = await _analyze(client, auth, m["id"])
    assert m["status"] in ("review", "analyzed") and m["error"] is None and m["items"]


async def test_decision_already_in_memory_makes_no_new_proposal(client, auth):
    pid = await _project()
    m = await _analyze(client, auth, (await _create(client, auth, project_id=pid, transcript=TRANSCRIPT, title="중복"))["id"])
    add = next(p for p in m["proposals"] if p["kind"] == "add_memory")
    await client.post(f"/api/meetings/{m['id']}/proposals/{add['id']}/approve", headers=auth)
    # 같은 회의를 다시 분석하면 이미 기억에 있는 결정은 제안하지 않는다
    m = await _analyze(client, auth, m["id"])
    pending = [p["payload"]["content"] for p in m["proposals"] if p["kind"] == "add_memory" and p["status"] == "pending"]
    assert add["payload"]["content"] not in pending and len(await _memories("meeting")) == 1


async def test_stale_target_blocks_approval_and_keeps_proposal_pending(client, auth):
    pid = await _project()
    old = await _memory("[기존] 릴리스는 금요일에 한다")
    m = await _create(client, auth, project_id=pid, transcript=TRANSCRIPT)
    m = await _analyze(client, auth, m["id"])
    sup = next(p for p in m["proposals"] if p["kind"] == "supersede_memory")
    async with sessionmaker()() as db, db.begin():
        await store.forget(db, uuid.UUID(old))
    r = await client.post(f"/api/meetings/{m['id']}/proposals/{sup['id']}/approve", headers=auth)
    assert r.status_code == 409 and r.json()["code"] == "stale"
    assert await _memories("meeting") == []
    cur = (await client.get(f"/api/meetings/{m['id']}", headers=auth)).json()
    assert next(p for p in cur["proposals"] if p["id"] == sup["id"])["status"] == "pending"


# ---- 범위·프로젝트

async def test_project_scope_rules_and_work_pool(client, auth, make_dir):
    personal = await _project("PersonalP", "personal")
    work = await _project("WorkP", "work")
    shared = await _project("SharedP", "shared")
    r = await client.post("/api/meetings", json={"title": "t", "scope": "work", "project_id": personal, "transcript": "x"}, headers=auth)
    assert r.status_code == 400 and r.json()["code"] == "scope_mismatch"
    r = await client.post("/api/meetings", json={"title": "t", "scope": "personal", "project_id": work, "transcript": "x"}, headers=auth)
    assert r.status_code == 400 and r.json()["code"] == "scope_mismatch"
    assert (await client.post("/api/meetings", json={"title": "t", "scope": "personal", "project_id": shared, "transcript": "x"}, headers=auth)).status_code == 201
    assert (await client.post("/api/meetings", json={"title": "t", "transcript": "x", "project_id": str(uuid.uuid4())}, headers=auth)).status_code == 400
    # 회사 회의의 분석 세션은 회사 계정 풀을 쓴다. 회사 계정이 없으면 실행하지 않고 이유를 남긴다.
    w = await _create(client, auth, scope="work", project_id=work, transcript=TRANSCRIPT)
    w = await _analyze(client, auth, w["id"])
    async with sessionmaker()() as db:
        sess = await db.get(ChatSession, uuid.UUID(w["analysis_session_id"]))
        assert sess.pool == "work" and sess.kind == "worker" and sess.archived_at is not None
    assert w["status"] == "failed" and "회사" in w["error"]
    # 회사 계정을 켜면 같은 회의를 다시 분석할 수 있다
    d = make_dir()
    acc = (await client.post("/api/accounts", json={"name": "co", "engine": "claude", "config_dir": str(d)}, headers=auth)).json()
    await client.patch(f"/api/accounts/{acc['id']}", json={"pool": "work"}, headers=auth)
    w = await _analyze(client, auth, w["id"])
    assert w["status"] in ("review", "analyzed") and agent_calls()[-1]["account_dir"] == str(d)


# ---- 입력: 녹음·Provider

async def test_audio_upload_waits_for_model_when_local_is_unconfigured(client, auth, new_session):
    ws, _ = await new_session()
    up = await client.post("/api/uploads", files={"file": ("회의.m4a", b"\x00audio", "audio/mp4")}, data={"workspace_id": ws["id"]}, headers=auth)
    art = up.json()
    m = await _create(client, auth, audio_artifact_id=art["id"], title="녹음 회의")
    assert m["status"] == "uploaded" and m["source"] == "audio" and m["audio"]["filename"] == "회의.m4a"
    prov = (await client.get("/api/meetings/providers", headers=auth)).json()["providers"]
    assert next(p for p in prov if p["id"] == "local_whisper")["state"] == "unconfigured"
    prov = [p for p in prov if p["id"] == "clovanote"]
    assert prov == [{"id": "clovanote", "label": "클로바노트", "state": "unconfigured", "profile_configured": False, "is_default": False,
                     "message": prov[0]["message"]}] and "꺼져" in prov[0]["message"]
    # 전사 요청은 상태를 바꾸지 않고 사유를 알린다. 전사 없이 분석도 못 한다.
    r = await client.post(f"/api/meetings/{m['id']}/transcribe", headers=auth)
    assert r.status_code == 409 and r.json()["code"] == "provider_unavailable" and "모델 설치" in r.json()["message"]
    assert (await client.get(f"/api/meetings/{m['id']}", headers=auth)).json()["status"] == "uploaded"
    assert (await client.post(f"/api/meetings/{m['id']}/analyze", headers=auth)).status_code == 400
    # 전사 텍스트를 직접 넣으면 같은 파이프라인으로 분석한다
    r = await client.post(f"/api/meetings/{m['id']}/transcript", json={"text": TRANSCRIPT, "source": "file"}, headers=auth)
    assert r.json()["status"] == "transcribed" and r.json()["transcript_source"] == "file"
    assert (await _analyze(client, auth, m["id"]))["status"] in ("review", "analyzed")
    # 삭제해도 보관함 파일은 남는다
    assert (await client.delete(f"/api/meetings/{m['id']}", headers=auth)).status_code == 204
    assert (await client.get(f"/api/artifacts/{art['id']}", headers=auth)).status_code == 200


class FakeProvider:
    id = "clovanote"
    label = "가짜 전사"

    def __init__(self):
        self.polls = 0
        self.submitted = None

    def availability(self):
        return providers.ProviderAvailability(providers.ProviderState.ready, "")

    async def submit(self, audio):
        self.submitted = audio
        return "job-1"

    async def poll(self, job_id):
        self.polls += 1
        assert job_id == "job-1"
        if self.polls == 1:
            return providers.TranscriptionPoll("pending")
        return providers.TranscriptionPoll("done", text=TRANSCRIPT)


async def test_fake_provider_feeds_the_same_analysis_without_changes(client, auth, new_session):
    ws, _ = await new_session()
    art = (await client.post("/api/uploads", files={"file": ("a.mp3", b"ID3", "audio/mpeg")}, data={"workspace_id": ws["id"]}, headers=auth)).json()
    fake, original = FakeProvider(), providers._REGISTRY["clovanote"]
    providers.register_provider(fake)
    try:
        m = await _create(client, auth, audio_artifact_id=art["id"])
        r = await client.post(f"/api/meetings/{m['id']}/transcribe", headers=auth, json={"provider_id": "clovanote"})
        assert r.status_code == 202 and r.json()["status"] == "transcribing" and r.json()["provider"] == "clovanote"
        assert fake.submitted.filename == "a.mp3" and fake.submitted.path.is_file()
        assert (await client.get(f"/api/meetings/{m['id']}", headers=auth)).json()["status"] == "transcribing"  # pending
        m = (await client.get(f"/api/meetings/{m['id']}", headers=auth)).json()
        assert m["status"] == "transcribed" and m["transcript_source"] == "provider:clovanote" and m["transcript_chars"] == len(TRANSCRIPT)
        assert (await _analyze(client, auth, m["id"]))["status"] in ("review", "analyzed")
    finally:
        providers._REGISTRY["clovanote"] = original


async def test_provider_failure_marks_transcription_failed(client, auth, new_session):
    ws, _ = await new_session()
    art = (await client.post("/api/uploads", files={"file": ("a.wav", b"RIFF", "audio/wav")}, data={"workspace_id": ws["id"]}, headers=auth)).json()

    class Failing(FakeProvider):
        async def poll(self, job_id):
            return providers.TranscriptionPoll("failed", error="변환 실패")

    original = providers._REGISTRY["clovanote"]
    providers.register_provider(Failing())
    try:
        m = await _create(client, auth, audio_artifact_id=art["id"])
        await client.post(f"/api/meetings/{m['id']}/transcribe", headers=auth, json={"provider_id": "clovanote"})
        m = (await client.get(f"/api/meetings/{m['id']}", headers=auth)).json()
        assert m["status"] == "failed" and m["failed_stage"] == "transcription" and m["error"] == "변환 실패"
    finally:
        providers._REGISTRY["clovanote"] = original


async def test_input_limits_and_state_guards(client, auth, monkeypatch):
    monkeypatch.setattr(get_settings(), "meeting_transcript_max_chars", 50)
    r = await client.post("/api/meetings", json={"title": "긴 전사", "transcript": "가" * 51}, headers=auth)
    assert r.status_code == 413 and r.json()["code"] == "too_large"
    assert (await client.post("/api/meetings", json={"title": "빈"}, headers=auth)).status_code == 400
    assert (await client.post("/api/meetings", json={"title": "  ", "transcript": "x"}, headers=auth)).status_code in (400, 422)
    m = await _create(client, auth, transcript="짧은 전사 FAKE_MEETING SLEEP")
    # 분석 중에는 전사를 바꾸거나 다시 분석할 수 없다
    assert (await client.post(f"/api/meetings/{m['id']}/analyze", headers=auth)).status_code == 202
    assert (await client.post(f"/api/meetings/{m['id']}/analyze", headers=auth)).status_code == 409
    assert (await client.post(f"/api/meetings/{m['id']}/transcript", json={"text": "x"}, headers=auth)).status_code == 409
    r = await client.post(f"/api/meetings/{m['id']}/analyze/cancel", headers=auth)
    assert r.status_code == 200 and r.json()["status"] == "failed" and "취소" in r.json()["error"]
    assert (await client.post(f"/api/meetings/{m['id']}/analyze/cancel", headers=auth)).status_code == 409


def test_transition_table_rejects_illegal_moves():
    from ttakkari.meetings.pipeline import PipelineError, transition

    m = Meeting(title="t", status="uploaded")
    with pytest.raises(PipelineError):
        transition(m, "analyzing")
    transition(m, "transcribed")
    transition(m, "analyzing")
    transition(m, "failed", stage="analysis", error="x" * 5000)
    assert m.failed_stage == "analysis" and len(m.error) == 2000
    transition(m, "analyzing")
    assert m.error is None and m.failed_stage is None


# ---- 항목·조회·삭제

async def test_item_status_search_and_delete_keeps_approved_memories(client, auth):
    pid = await _project()
    m = await _analyze(client, auth, (await _create(client, auth, project_id=pid, transcript=TRANSCRIPT))["id"])
    act = _kinds(m, "action")[0]
    r = await client.patch(f"/api/meetings/{m['id']}/items/{act['id']}", json={"status": "done"}, headers=auth)
    assert r.status_code == 200 and r.json()["status"] == "done"
    dec = _kinds(m, "decision")[0]
    assert (await client.patch(f"/api/meetings/{m['id']}/items/{dec['id']}", json={"status": "done"}, headers=auth)).status_code == 400
    lst = (await client.get("/api/meetings", params={"q": "릴리스", "scope": "personal"}, headers=auth)).json()["meetings"]
    assert [x["id"] for x in lst] == [m["id"]] and lst[0]["counts"]["decision"] == 2 and lst[0]["project"]["name"] == "Alpha"
    assert (await client.get("/api/meetings", params={"status": "analyzed"}, headers=auth)).json()["meetings"] == []
    add = next(p for p in m["proposals"] if p["kind"] == "add_memory")
    await client.post(f"/api/meetings/{m['id']}/proposals/{add['id']}/approve", headers=auth)
    assert (await client.delete(f"/api/meetings/{m['id']}", headers=auth)).status_code == 204
    assert (await client.get(f"/api/meetings/{m['id']}", headers=auth)).status_code == 404
    assert len(await _memories("meeting")) == 1
    async with sessionmaker()() as db:
        assert await db.scalar(select(MeetingItem.id).limit(1)) is None


async def test_meetings_require_login(client):
    for method, path in (("GET", "/api/meetings"), ("POST", "/api/meetings"), ("GET", "/api/meetings/providers"),
                         ("POST", f"/api/meetings/{uuid.uuid4()}/proposals/{uuid.uuid4()}/approve")):
        assert (await client.request(method, path)).status_code == 401


# ---- 에이전트 읽기 도구

async def _agent_headers(client, auth, make_dir, pool="personal"):
    root = make_dir()
    ws = (await client.post("/api/workspaces", json={"name": root.name, "root_path": str(root)}, headers=auth)).json()
    sess = (await client.post("/api/sessions", json={"workspace_id": ws["id"], "pool": pool}, headers=auth)).json()
    return {"Authorization": f"Bearer {issue_token(uuid.UUID(sess['id']))}"}


async def test_agent_reads_meetings_scoped_and_has_no_write_path(client, auth, make_dir):
    pid = await _project()
    wid = await _project("WorkP", "work")
    mine = await _analyze(client, auth, (await _create(client, auth, title="개인 회의", project_id=pid, transcript=TRANSCRIPT))["id"])
    co = await _create(client, auth, title="회사 회의", scope="work", project_id=wid, transcript=TRANSCRIPT)
    ph = await _agent_headers(client, auth, make_dir)
    wh = await _agent_headers(client, auth, make_dir, "work")

    lst = (await client.get("/api/agent/meetings", headers=ph)).json()["meetings"]
    assert {x["title"] for x in lst} == {"개인 회의", "회사 회의"}
    assert [x["title"] for x in (await client.get("/api/agent/meetings", headers=wh)).json()["meetings"]] == ["회사 회의"]
    assert (await client.get("/api/agent/meetings", params={"project": "Alpha"}, headers=ph)).json()["meetings"][0]["title"] == "개인 회의"
    assert (await client.get("/api/agent/meetings", params={"project": "없는 프로젝트"}, headers=ph)).json()["meetings"] == []

    detail = (await client.get(f"/api/agent/meetings/{mine['id']}", headers=ph)).json()
    assert detail["summary"] and [d["text"] for d in detail["items"]["action"]] == ["배포 체크리스트를 작성한다"]
    assert "transcript" not in detail and "proposals" not in detail and detail["pending_proposals"] > 0
    assert (await client.get(f"/api/agent/meetings/{mine['id']}", headers=wh)).status_code == 404
    assert (await client.get(f"/api/agent/meetings/{co['id']}", headers=ph)).status_code == 200

    hits = (await client.get("/api/agent/meetings/search", params={"kind": "action", "status": "open", "project": "Alpha"}, headers=ph)).json()["items"]
    assert [h["text"] for h in hits] == ["배포 체크리스트를 작성한다"] and hits[0]["meeting"]["title"] == "개인 회의"
    found = (await client.get("/api/agent/meetings/search", params={"q": "모바일 화면", "kind": "decision"}, headers=ph)).json()["items"]
    assert found and found[0]["text"] == "모바일 화면을 먼저 만든다"
    assert (await client.get("/api/agent/meetings/search", params={"q": "모바일"}, headers=wh)).json()["items"] == []
    # 쓰기·승인 경로는 에이전트 쪽에 없다
    for method, path in (("POST", "/api/agent/meetings"), ("PATCH", f"/api/agent/meetings/{mine['id']}"),
                         ("DELETE", f"/api/agent/meetings/{mine['id']}"),
                         ("POST", f"/api/agent/meetings/{mine['id']}/proposals/{uuid.uuid4()}/approve")):
        assert (await client.request(method, path, headers=ph)).status_code in (404, 405)
    assert (await client.get("/api/agent/meetings")).status_code == 401
    # 사용자 토큰으로는 에이전트 경로를 못 연다(반대도 마찬가지)
    assert (await client.get("/api/agent/meetings", headers=auth)).status_code == 401
    assert (await client.get("/api/meetings", headers=ph)).status_code == 401


async def test_agent_cannot_forget_memory_made_from_a_meeting(client, auth, make_dir):
    pid = await _project()
    m = await _analyze(client, auth, (await _create(client, auth, project_id=pid, transcript=TRANSCRIPT))["id"])
    add = next(p for p in m["proposals"] if p["kind"] == "add_memory")
    done = (await client.post(f"/api/meetings/{m['id']}/proposals/{add['id']}/approve", headers=auth)).json()
    mem_id = next(p for p in done["proposals"] if p["id"] == add["id"])["result"]["memory_id"]
    ph = await _agent_headers(client, auth, make_dir)
    r = await client.post("/api/agent/forget", json={"id": mem_id}, headers=ph)
    assert r.status_code == 403
    async with sessionmaker()() as db:
        assert (await db.get(CtxMemory, uuid.UUID(mem_id))).archived_at is None
