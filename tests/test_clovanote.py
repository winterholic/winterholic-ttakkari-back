"""ADR 0021: 클로바노트 연동(작업 큐·노트 DB·업로드 전사)과 회의록 벡터 저장. 실제 Chrome·네이버 대신 가짜 클라이언트를 끼운다.
가짜 응답은 2026-10-13 실제 API 응답(notes/{id})의 모양을 그대로 따른다."""
from __future__ import annotations

import asyncio
import time
import uuid

import pytest
from sqlalchemy import func, select
from test_meetings import _agent_headers, _create, _project, _until

from ttakkari.config import get_settings
from ttakkari.context import store
from ttakkari.db import sessionmaker
from ttakkari.meetings import clova_jobs, vectors
from ttakkari.meetings import clovanote as cn
from ttakkari.models import ClovaJob, ClovaNote, MeetingChunk


def _info(note_id: str, name: str, status: str = "DONE", progress: int = 100) -> dict:
    return {"noteId": note_id, "noteName": name, "noteStatus": status, "createdDate": "2026-10-12T15:00:00.000Z",
            "updatedDate": "2026-10-13T13:06:05.605Z", "audioStartDate": "2026-10-12T15:00:00.000Z", "audioDuration": 213520,
            "conversationType": "", "recognitionLanguage": "ko", "folderName": "전체 노트", "uploadType": "FILE",
            "attendeeList": [{"attendeeId": "A1", "attendeeName": "민수"}, {"attendeeId": "B2", "attendeeName": "지영"}],
            "recognitionStatus": {"status": "DONE" if status == "DONE" else "ONGOING", "errorMessage": "", "recognitionProgress": progress},
            "oneLineSummary": "", "abstractSummary": []}


def _detail(note_id: str, name: str, blocks: list[tuple[str, int, str]] | None = None) -> dict:
    blocks = blocks if blocks is not None else [("A1", 480, "목요일에 내자. FAKE_MEETING"), ("B2", 8980, "모바일 먼저 하고 데이터 이전은 다음 주에 보자.")]
    return {"noteId": note_id, "noteInfo": _info(note_id, name),
            "script": {"blockList": [{"speakerId": s, "start": t, "end": t + 3000, "sttLabel": "1", "text": x} for s, t, x in blocks]}}


class FakeClient:
    def __init__(self) -> None:
        self.lock = asyncio.Lock()
        self.notes = {"n-1": _detail("n-1", "주간 회의 녹음"), "n-2": _detail("n-2", "제품 회의")}
        self.uploaded: list[tuple[str, str]] = []
        self.polls = 0
        self.workspace_id = "ws-test"
        self.status_cache = None
        self.running = False
        self.login = cn.LoginState()

    async def list_notes(self, limit=50, max_pages=40):
        return [d["noteInfo"] for d in self.notes.values()]

    async def get_note(self, note_id):
        d = self.notes[note_id]
        if note_id.startswith("up-"):
            self.polls += 1
            if self.polls < 3:  # 두 번은 전사 중
                return {**d, "noteInfo": _info(note_id, d["noteInfo"]["noteName"], "RECOGNITION", 40 * self.polls)}
        return d

    async def create_note(self, name, language="ko"):
        nid = f"up-{len(self.uploaded) + 1}"
        self.notes[nid] = _detail(nid, name, [("A1", 0, "업로드한 녹음입니다. FAKE_MEETING 배포는 화요일.")])
        return nid

    async def upload_audio(self, note_id, path, filename, mime):
        self.uploaded.append((note_id, filename))
        return {"audioDuration": 8030}

    async def start_recognition(self, note_id):
        return None

    async def check(self):
        self.status_cache = cn.Status("ready", "연결됨", account={"name": "테스트", "email": "t@example.com"},
                                      workspace={"id": "ws-test", "name": "테스트"}, quota={"recognition_remain_minutes": 300})
        return self.status_cache

    async def close(self):
        return None


@pytest.fixture
def fake(monkeypatch):
    f = FakeClient()
    monkeypatch.setattr(cn, "client", f)
    monkeypatch.setattr(clova_jobs, "POLL_SECONDS", 0.05)
    s = get_settings()
    monkeypatch.setattr(s, "clovanote_enabled", True)
    return f


async def _job_done(client, auth, job_id: str, timeout=20.0) -> dict:
    deadline = time.monotonic() + timeout
    while True:
        j = (await client.get(f"/api/clovanote/jobs/{job_id}", headers=auth)).json()
        if j["status"] in ("succeeded", "failed"):
            return j
        assert time.monotonic() < deadline, f"작업 {job_id} 가 끝나지 않음: {j}"
        await asyncio.sleep(0.05)


async def _vectors_ready(mid: str, timeout=20.0):
    deadline = time.monotonic() + timeout
    while True:
        async with sessionmaker()() as db:
            n = await db.scalar(select(func.count()).select_from(MeetingChunk).where(MeetingChunk.meeting_id == uuid.UUID(mid)))
            from ttakkari.models import Meeting
            m = await db.get(Meeting, uuid.UUID(mid))
            if m.vector_status == "ready" and n:
                return n
        assert time.monotonic() < deadline, "벡터 색인이 끝나지 않음"
        await asyncio.sleep(0.1)


# ---- 노트 해석

def test_note_parsing_matches_real_shape():
    d = _detail("x", "회의", [("A1", 65_000, "안녕하세요."), ("", 3_725_000, "누군지 모름"), ("A1", 0, "  ")])
    segs = cn.segments_of(d)
    assert segs == [{"speaker": "민수", "start_ms": 65000, "end_ms": 68000, "text": "안녕하세요."},
                    {"speaker": "참석자 1", "start_ms": 3725000, "end_ms": 3728000, "text": "누군지 모름"}]
    assert cn.transcript_of(segs) == "[01:05] 민수: 안녕하세요.\n[01:02:05] 참석자 1: 누군지 모름"
    assert cn.note_state(_info("x", "a")) == "ready"
    assert cn.note_state(_info("x", "a", "RECOGNITION", 10)) == "processing"
    assert cn.note_state({"noteStatus": "INIT", "recognitionStatus": {"status": "FAIL"}}) == "failed"
    assert cn.parse_time("0001-01-01T00:00:00.000Z") is None and cn.parse_time("2026-10-12T15:00:00.000Z").year == 2026


# ---- 동기화·가져오기

async def test_sync_stores_notes_and_transcripts_then_import_creates_analyzed_meeting(client, auth, fake, make_dir):
    job = (await client.post("/api/clovanote/sync", headers=auth)).json()
    assert job["kind"] == "sync" and [s["id"] for s in job["stages"]][:2] == ["connect", "list"]
    done = await _job_done(client, auth, job["id"])
    assert done["status"] == "succeeded" and done["result"] == {"notes": 2, "fetched": 2}
    assert [s["stage"] for s in done["steps"]][:2] == ["connect", "list"]
    notes = (await client.get("/api/clovanote/notes", headers=auth)).json()["notes"]
    assert {n["title"] for n in notes} == {"주간 회의 녹음", "제품 회의"} and all(n["has_transcript"] for n in notes)
    full = (await client.get("/api/clovanote/notes/n-1", headers=auth)).json()
    assert full["segments"][0]["speaker"] == "민수" and full["transcript"].startswith("[00:00] 민수: 목요일에 내자.")

    # 다시 동기화하면 바뀐 노트만 전사를 받는다
    again = await _job_done(client, auth, (await client.post("/api/clovanote/sync", headers=auth)).json()["id"])
    assert again["result"] == {"notes": 2, "fetched": 0}

    pid = await _project("Clova")
    imp = (await client.post("/api/clovanote/notes/n-1/import", json={"scope": "personal", "project_id": pid}, headers=auth)).json()
    imp = await _job_done(client, auth, imp["id"])
    assert imp["status"] == "succeeded", imp
    mid = imp["result"]["meeting_id"]
    m = await _until(client, auth, mid)
    assert m["status"] in ("review", "analyzed") and m["transcript_source"] == "provider:clovanote"
    assert m["participants"] == ["민수", "지영"] and m["project"]["name"] == "Clova"
    assert m["clovanote"]["note"]["note_id"] == "n-1"
    # 같은 노트를 다시 가져오지 않는다
    r = await client.post("/api/clovanote/notes/n-1/import", json={}, headers=auth)
    assert r.status_code == 409 and r.json()["code"] == "already_imported"
    listed = {n["note_id"]: n for n in (await client.get("/api/clovanote/notes", params={"imported": "false"}, headers=auth)).json()["notes"]}
    assert set(listed) == {"n-2"}

    # 에이전트: 개인 세션은 노트를 읽고, 회사 세션은 회사 회의로 가져온 노트만 본다
    ph = await _agent_headers(client, auth, make_dir)
    wh = await _agent_headers(client, auth, make_dir, "work")
    assert {n["note_id"] for n in (await client.get("/api/agent/clovanote/notes", headers=ph)).json()["notes"]} == {"n-1", "n-2"}
    assert (await client.get("/api/agent/clovanote/notes", headers=wh)).json()["notes"] == []
    seg = (await client.get("/api/agent/clovanote/notes/n-2", params={"limit": 1}, headers=ph)).json()
    assert seg["segments_total"] == 2 and len(seg["segments"]) == 1 and seg["next_offset"] == 1
    assert (await client.get("/api/agent/clovanote/notes/n-2", headers=wh)).status_code == 404
    assert (await client.post("/api/agent/clovanote/sync", headers=wh)).status_code == 403
    r = (await client.post("/api/agent/clovanote/notes/n-2/import", json={"project": "없는것"}, headers=ph)).json()
    assert r["error"] == "project_not_found"


async def test_import_of_unfinished_note_fails_with_reason(client, auth, fake):
    fake.notes["n-3"] = {**_detail("n-3", "녹음 중"), "noteInfo": _info("n-3", "녹음 중", "RECOGNITION", 30)}
    j = await _job_done(client, auth, (await client.post("/api/clovanote/notes/n-3/import", json={}, headers=auth)).json()["id"])
    assert j["status"] == "failed" and "아직 전사가 끝나지" in j["error"]
    retry = await client.post(f"/api/clovanote/jobs/{j['id']}/retry", headers=auth)
    assert retry.status_code == 202 and retry.json()["id"] != j["id"]
    await _job_done(client, auth, retry.json()["id"])


# ---- 업로드 → 전사 → 분석(자동 처리)

async def test_audio_meeting_auto_process_uploads_waits_and_analyzes(client, auth, fake, new_session):
    ws, _ = await new_session()
    art = (await client.post("/api/uploads", files={"file": ("회의.m4a", b"\x00audio", "audio/mp4")}, data={"workspace_id": ws["id"]},
                             headers=auth)).json()
    m = await _create(client, auth, audio_artifact_id=art["id"], title="녹음 회의", auto_process=True)
    assert m["status"] == "transcribing" and m["provider"] == "clovanote", m
    m = await _until(client, auth, m["id"], timeout=30)
    assert m["status"] in ("review", "analyzed"), m
    assert "업로드한 녹음입니다" in m["transcript"] and fake.uploaded == [("up-1", "회의.m4a")]
    job = m["clovanote"]["job"]
    assert job["kind"] == "upload" and job["status"] == "succeeded" and job["note_id"] == "up-1"
    stages = [s["stage"] for s in job["steps"]]
    assert stages.index("upload") < stages.index("recognize") < stages.index("fetch")
    assert any("40%" in s["message"] for s in job["steps"])  # 전사 진행률이 기록에 남는다
    assert m["clovanote"]["note"]["meeting_id"] == m["id"]


async def test_upload_rejects_unsupported_format(client, auth, fake, new_session):
    ws, _ = await new_session()
    art = (await client.post("/api/uploads", files={"file": ("회의.ogg", b"\x00audio", "audio/ogg")}, data={"workspace_id": ws["id"]},
                             headers=auth)).json()
    m = await _create(client, auth, audio_artifact_id=art["id"], title="ogg 회의", auto_process=True)
    m = await _until(client, auth, m["id"], states={"failed"})
    assert m["failed_stage"] == "transcription" and "m4a" in m["error"]


async def test_login_required_blocks_transcription_with_reason(client, auth, fake, new_session):
    fake.status_cache = cn.Status("login_required", "로그인이 풀렸어요.")
    ws, _ = await new_session()
    art = (await client.post("/api/uploads", files={"file": ("a.m4a", b"\x00", "audio/mp4")}, data={"workspace_id": ws["id"]}, headers=auth)).json()
    m = await _create(client, auth, audio_artifact_id=art["id"], auto_process=True)
    assert m["status"] == "uploaded" and m["auto_error"]["code"] == "provider_unavailable" and "재로그인" in m["auto_error"]["message"]
    st = (await client.get("/api/clovanote/status", headers=auth)).json()
    assert st["state"] == "login_required" and "active_jobs" in st


# ---- 회의록 벡터 저장·검색

async def test_meeting_chunks_are_embedded_and_found_by_recall_and_agent(client, auth, make_dir):
    pid = await _project("VecP")
    wid = await _project("VecW", "work")
    m = await _create(client, auth, title="배포 회의", project_id=pid,
                      transcript="민수: 운영 배포는 다음 주 화요일로 미루자.\n지영: 좋아요. 롤백 계획도 같이 쓰죠.")
    await _vectors_ready(m["id"])
    w = await _create(client, auth, title="회사 회의", scope="work", project_id=wid, transcript="팀장: 예산 검토는 금요일까지.")
    await _vectors_ready(w["id"])
    async with sessionmaker()() as db:
        chunks = list(await db.scalars(select(MeetingChunk).where(MeetingChunk.meeting_id == uuid.UUID(m["id"]))))
        assert chunks and all(c.embedding is not None and c.model == "hash" for c in chunks)
        assert chunks[0].content.startswith("[회의 ") and "배포 회의" in chunks[0].content and "VecP" in chunks[0].content
        hits = await store.recall(db, "운영 배포 화요일", 5)
        assert any(h.kind == "meeting" and h.meta["meeting_id"] == m["id"] for h in hits)
        assert not [h for h in await store.recall(db, "운영 배포 화요일", 5, scope="work") if h.kind == "meeting" and h.meta["meeting_id"] == m["id"]]
    detail = (await client.get(f"/api/meetings/{m['id']}", headers=auth)).json()
    assert detail["vector"]["status"] == "ready" and detail["vector"]["chunks"] >= 1

    ph = await _agent_headers(client, auth, make_dir)
    wh = await _agent_headers(client, auth, make_dir, "work")
    res = (await client.get("/api/agent/meetings/semantic", params={"q": "배포 언제 하지"}, headers=ph)).json()["results"]
    assert res and res[0]["meeting_id"] == m["id"] and res[0]["similarity"] is not None
    assert all(r["scope"] == "work" for r in (await client.get("/api/agent/meetings/semantic", params={"q": "배포 예산"}, headers=wh)).json()["results"])
    rec = (await client.post("/api/agent/recall", json={"query": "롤백 계획"}, headers=ph)).json()["results"]
    assert any(r["type"] == "meeting" for r in rec)

    # 전사를 바꾸면 조각을 다시 만든다(이전 조각은 남지 않는다)
    async with sessionmaker()() as db:
        before = await db.scalar(select(func.count()).select_from(MeetingChunk).where(MeetingChunk.meeting_id == uuid.UUID(m["id"])))
    await vectors.reindex(uuid.UUID(m["id"]))
    async with sessionmaker()() as db:
        after = await db.scalar(select(func.count()).select_from(MeetingChunk).where(MeetingChunk.meeting_id == uuid.UUID(m["id"])))
    assert before == after


def test_transcript_split_keeps_lines_and_overlaps():
    text = "\n".join(f"[{i:02d}:00] 화자: " + "가" * 90 for i in range(30))
    parts = vectors.split_transcript(text)
    assert len(parts) > 3 and all(len(p) <= vectors.WINDOW + 120 for p in parts)
    assert parts[1].splitlines()[0] in parts[0]  # 앞 조각 끝 줄이 다음 조각 앞에 겹친다
    long = vectors.split_transcript("나" * 2000)
    assert len(long) >= 3 and all(len(p) <= vectors.WINDOW for p in long)


async def test_jobs_resume_after_restart(client, auth, fake):
    async with sessionmaker()() as db:
        j = ClovaJob(kind="sync", status="running", stage="list", params={}, steps=[], result={})
        db.add(j)
        await db.commit()
        jid = str(j.id)
    await clova_jobs.start()
    assert (await _job_done(client, auth, jid))["status"] == "succeeded"
    async with sessionmaker()() as db:
        assert await db.scalar(select(func.count()).select_from(ClovaNote)) >= 2


async def test_pms_project_import_and_semantic_filter(client, auth, fake, make_dir):
    p = (await client.post('/api/pms/projects', json={'name': 'PMS 녹음'}, headers=auth)).json()
    job = (await client.post('/api/clovanote/notes/n-1/import', json={'project_id': p['id'], 'analyze': False}, headers=auth)).json()
    done = await _job_done(client, auth, job['id'])
    assert done['status'] == 'succeeded', done
    m = (await client.get(f'/api/meetings/{done["meeting_id"]}', headers=auth)).json()
    assert m['project'] == {'id': p['id'], 'name': p['name']}
    await _vectors_ready(m['id'])
    ph = await _agent_headers(client, auth, make_dir)
    hits = (await client.get('/api/agent/meetings/semantic', params={'project': p['name'], 'q': '목요일'}, headers=ph)).json()['results']
    assert hits and all(h['meeting_id'] == m['id'] for h in hits)
