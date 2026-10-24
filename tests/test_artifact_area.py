"""보관함 분류: 파일(files)과 에이전트 파일(agent)."""
from __future__ import annotations

import asyncio
import importlib.util
import os
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from helpers import run_to_end, wait_run
from sqlalchemy import select, text, update
from test_orchestrator import _orch, _session

from ttakkari.config import get_settings
from ttakkari.db import sessionmaker
from ttakkari.models import (
    Artifact,
    ArtifactArea,
    ChatSession,
    Run,
    default_artifact_area,
)
from ttakkari.orchestrator.runtime import issue_token


async def _areas(client, auth, **params) -> dict[str, str]:
    r = await client.get("/api/artifacts", params=params, headers=auth)
    assert r.status_code == 200, r.text
    return {a["filename"]: a["area"] for a in r.json()}


def test_default_area_by_source():
    assert default_artifact_area("upload") == "files"
    assert default_artifact_area("user_registered") == "files"
    # 불확실한 출처는 에이전트 쪽
    assert default_artifact_area("agent_output") == "agent"
    assert default_artifact_area("agent_modified") == "agent"
    assert default_artifact_area("something_new") == "agent"


async def test_upload_and_user_registered_are_files(client, auth, new_session):
    ws, _ = await new_session()
    root = Path((await client.get(f"/api/workspaces/{ws['id']}", headers=auth)).json()["root_path"])
    (root / "mine.md").write_text("# mine")
    r = await client.post("/api/uploads", files={"file": ("up.md", b"# up", "text/markdown")},
                          data={"workspace_id": ws["id"]}, headers=auth)
    assert r.status_code == 201 and r.json()["area"] == "files"
    r = await client.post("/api/artifacts", json={"workspace_id": ws["id"], "rel_path": "mine.md"}, headers=auth)
    assert r.status_code == 201 and r.json()["area"] == "files" and r.json()["source"] == "user_registered"


async def test_direct_chat_outbox_remains_agent_on_share_and_files_on_keep(client, auth, new_session, make_git_dir):
    """AI 생성·수정 및 자발적 share_file 은 AI 작업 파일(agent)로 유지된다.
    대화에서 명시적 보관 요청(keep)이 있을 때만 내 보관함(files)으로 들어가고 출처(agent)와 근거가 남는다."""
    _ws, sess = await new_session(make_git_dir())
    ask = "MODIFY 그리고 만든 report.md 를 내 보관함에 보관해줘"
    run = await run_to_end(client, auth, sess["id"], ask)
    assert await _areas(client, auth, session_id=sess["id"]) == {
        "report.md": "agent", "README.md": "agent", "new_file.txt": "agent"}
    assert await _areas(client, auth, session_id=sess["id"], area="files") == {}

    # 에이전트가 share_file 로 넘겨도 내 보관함(files)이 아닌 AI 작업 파일(agent)에 남는다.
    run_id = (await client.get(f"/api/sessions/{sess['id']}/runs", headers=auth)).json()[0]["id"]
    assert run_id == run["id"]
    outbox = get_settings().outbox_root / run_id
    h = _orch(issue_token(sess["id"]))
    before = (await client.get("/api/artifacts", params={"session_id": sess["id"]}, headers=auth)).json()
    before_id = next(a["id"] for a in before if a["filename"] == "report.md")
    r = await client.post("/api/agent/share", json={"path": str(outbox / "report.md")}, headers=h)
    assert r.status_code == 200 and r.json()["artifact"]["area"] == "agent"
    assert r.json()["artifact"]["id"] == before_id
    assert await _areas(client, auth, session_id=sess["id"]) == {
        "report.md": "agent", "README.md": "agent", "new_file.txt": "agent"}

    # 다시 공유해도 같은 행이며 area=agent 유지.
    r2 = await client.post("/api/agent/share", json={"path": str(outbox / "report.md")}, headers=h)
    assert r2.json()["artifact"]["id"] == before_id
    assert r2.json()["artifact"]["area"] == "agent"

    # 사용자가 대화에서 명시적으로 보관을 요청했을 때(keep) 비로소 내 보관함(files)이 되고 근거가 기록된다.
    r_keep = await client.post("/api/agent/keep", json={"path": str(outbox / "report.md"),
                                                         "quote": ask}, headers=h)
    assert r_keep.status_code == 200, r_keep.text
    kept = r_keep.json()["artifact"]
    assert kept["id"] == before_id
    assert kept["area"] == "files"
    assert kept["origin"] == "agent"  # AI 파일 출처 유지!
    assert kept["keep_reason"] == "chat_request"
    assert kept["keep_evidence"]["quote"] == ask
    assert kept["keep_evidence"]["binding"] == "filename"
    assert kept["keep_evidence"]["message_id"] == run["id"]  # 실제 사용자 메시지(Run prompt)
    assert kept["keep_evidence"]["message_source"] == "message"
    assert await _areas(client, auth, session_id=sess["id"], area="files") == {"report.md": "files"}


async def test_share_refreshes_modified_original_before_download(client, auth, new_session, make_git_dir):
    """수집 후 원본을 고치고 공유하면 다운로드가 최신 내용이다. 재공유도 같은 행이며 area는 agent 유지."""
    _ws, sess = await new_session(make_git_dir())
    await run_to_end(client, auth, sess["id"], "write report")
    run_id = (await client.get(f"/api/sessions/{sess['id']}/runs", headers=auth)).json()[0]["id"]
    target = get_settings().outbox_root / run_id / "report.md"
    h = _orch(issue_token(sess["id"]))
    old = (await client.get("/api/artifacts", params={"session_id": sess["id"]}, headers=auth)).json()[0]
    assert old["storage_mode"] == "copy"
    new_bytes = b"# report v2 with more bytes\n"
    target.write_bytes(new_bytes)
    r = await client.post("/api/agent/share", json={"path": str(target)}, headers=h)
    assert r.status_code == 200 and r.json()["artifact"]["area"] == "agent"
    art = (await client.get(f"/api/artifacts/{old['id']}", headers=auth)).json()
    assert art["id"] == old["id"]
    assert art["size_bytes"] == len(new_bytes) and art["sha256"] != old["sha256"]
    body = await client.get(f"/api/artifacts/{old['id']}/content", headers=auth)
    assert body.status_code == 200 and body.content == new_bytes
    r2 = await client.post("/api/agent/share", json={"path": str(target)}, headers=h)
    assert r2.json()["artifact"]["id"] == old["id"]
    body2 = await client.get(f"/api/artifacts/{old['id']}/content", headers=auth)
    assert body2.content == new_bytes


async def test_worker_outbox_is_agent_until_kept(client, auth, make_dir):
    orch = await _session(client, auth, make_dir, kind="orchestrator")
    h = _orch(issue_token(orch["id"]))
    wid = (await client.post("/api/orchestrator/workers", json={"title": "w", "prompt": "write report", "engine": "claude"},
                             headers=h)).json()["worker_id"]
    wrun = (await client.get(f"/api/sessions/{wid}/runs", headers=auth)).json()[0]
    await wait_run(client, auth, wrun["id"])
    assert await _areas(client, auth, session_id=wid) == {"report.md": "agent"}
    assert await _areas(client, auth, session_id=wid, area="files") == {}

    # 오케스트레이터가 사용자에게 넘기려고 share_file 로 올려도 AI 작업 파일(agent)로 유지된다.
    outbox = get_settings().outbox_root / wrun["id"]
    r = await client.post("/api/agent/share", json={"path": str(outbox / "report.md")}, headers=h)
    assert r.status_code == 200 and r.json()["artifact"]["area"] == "agent"
    assert r.json()["artifact"]["session_id"] == orch["id"]
    # 작업 세션 쪽 원본도 에이전트 파일로 남는다.
    assert await _areas(client, auth, session_id=wid) == {"report.md": "agent"}


async def test_collect_skips_already_registered_outbox_files(client, auth, new_session, make_git_dir):
    """수집이 끝나고 다시 수집해도(또는 공유 후 수집해도) 같은 파일의 행이 늘지 않는다."""
    from ttakkari.agents.base import ParseState
    from ttakkari.models import Run, Workspace
    from ttakkari.runs.manager import RunManager

    _ws, sess = await new_session(make_git_dir())
    await run_to_end(client, auth, sess["id"], "write report")
    run_id = (await client.get(f"/api/sessions/{sess['id']}/runs", headers=auth)).json()[0]["id"]
    n0 = len((await client.get("/api/artifacts", params={"session_id": sess["id"]}, headers=auth)).json())
    assert n0 == 1
    async with sessionmaker()() as db:
        run = await db.get(Run, uuid.UUID(run_id))
        ws = await db.get(Workspace, run.workspace_id)
        await RunManager()._collect_artifacts(db, run, ws, get_settings().outbox_root / run_id, ParseState())
    n1 = len((await client.get("/api/artifacts", params={"session_id": sess["id"]}, headers=auth)).json())
    assert n1 == n0
    # 공유 후 다시 수집해도 agent 그대로, 행도 늘지 않는다.
    h = _orch(issue_token(sess["id"]))
    outbox = get_settings().outbox_root / run_id
    r = await client.post("/api/agent/share", json={"path": str(outbox / "report.md")}, headers=h)
    assert r.json()["artifact"]["area"] == "agent"
    async with sessionmaker()() as db:
        run = await db.get(Run, uuid.UUID(run_id))
        ws = await db.get(Workspace, run.workspace_id)
        await RunManager()._collect_artifacts(db, run, ws, get_settings().outbox_root / run_id, ParseState())
    assert await _areas(client, auth, session_id=sess["id"]) == {"report.md": "agent"}


async def test_share_links_token_run_not_latest_session_run(client, auth, new_session):
    """대기 중인 다음 Run 이 있어도 share 는 호출한 Run 에 연결하고, 그 Run 의 수집은 같은 경로를 다시 등록하지 않는다."""
    from ttakkari.agents.base import ParseState
    from ttakkari.models import Run, Workspace
    from ttakkari.runs.manager import RunManager

    ws, sess = await new_session()
    sid = uuid.UUID(sess["id"])
    async with sessionmaker()() as db:
        running = Run(session_id=sid, workspace_id=uuid.UUID(ws["id"]), prompt="a", engine="claude", status="running")
        db.add(running)
        await db.flush()
        queued = Run(session_id=sid, workspace_id=uuid.UUID(ws["id"]), prompt="b", engine="claude", status="queued")
        db.add(queued)
        await db.commit()
        running_id, queued_id = running.id, queued.id
    assert queued_id != running_id
    outbox = get_settings().outbox_root / str(running_id)
    outbox.mkdir(parents=True)
    (outbox / "report.md").write_text("# report\n")
    h = _orch(issue_token(sid, running_id))
    r = await client.post("/api/agent/share", json={"path": str(outbox / "report.md")}, headers=h)
    assert r.status_code == 200, r.text
    assert r.json()["artifact"]["run_id"] == str(running_id)
    assert r.json()["artifact"]["area"] == "agent"

    async with sessionmaker()() as db:
        run = await db.get(Run, running_id)
        await RunManager()._collect_artifacts(db, run, await db.get(Workspace, run.workspace_id), outbox, ParseState())
    rows = (await client.get("/api/artifacts", params={"session_id": str(sid)}, headers=auth)).json()
    assert [(a["filename"], a["area"]) for a in rows] == [("report.md", "agent")]


async def test_share_existing_row_keeps_creating_run(client, auth, new_session):
    """이미 수집된 행을 다른 Run 이 공유해도 run_id 는 만든 Run 으로 남고 분류도 agent 로 유지된다."""
    from ttakkari.agents.base import ParseState
    from ttakkari.models import Run, Workspace
    from ttakkari.runs.manager import RunManager

    ws, sess = await new_session()
    sid = uuid.UUID(sess["id"])
    async with sessionmaker()() as db:
        creator = Run(session_id=sid, workspace_id=uuid.UUID(ws["id"]), prompt="a", engine="claude", status="succeeded")
        sharer = Run(session_id=sid, workspace_id=uuid.UUID(ws["id"]), prompt="b", engine="claude", status="running")
        db.add_all([creator, sharer])
        await db.commit()
        creator_id, sharer_id = creator.id, sharer.id
    outbox = get_settings().outbox_root / str(creator_id)
    outbox.mkdir(parents=True)
    (outbox / "out.md").write_text("# out\n")
    async with sessionmaker()() as db:
        run = await db.get(Run, creator_id)
        await RunManager()._collect_artifacts(db, run, await db.get(Workspace, run.workspace_id), outbox, ParseState())
    r = await client.post("/api/agent/share", json={"path": str(outbox / "out.md")},
                          headers=_orch(issue_token(sid, sharer_id)))
    assert r.status_code == 200, r.text
    art = r.json()["artifact"]
    assert art["area"] == "agent" and art["run_id"] == str(creator_id)
    rows = (await client.get("/api/artifacts", params={"session_id": str(sid)}, headers=auth)).json()
    assert [(a["filename"], a["area"]) for a in rows] == [("out.md", "agent")]


async def test_collect_dedup_is_per_session_not_global(client, auth, new_session):
    """같은 경로를 다른 세션이 공유했더라도 이 세션의 수집은 자기 에이전트 파일 행을 만든다."""
    from ttakkari.agents.base import ParseState
    from ttakkari.models import Run, Workspace
    from ttakkari.runs.manager import RunManager

    ws, owner = await new_session()
    _, other = await new_session()
    async with sessionmaker()() as db:
        run = Run(session_id=uuid.UUID(owner["id"]), workspace_id=uuid.UUID(ws["id"]), prompt="a", engine="claude",
                  status="running")
        db.add(run)
        await db.commit()
        run_id = run.id
    outbox = get_settings().outbox_root / str(run_id)
    outbox.mkdir(parents=True)
    (outbox / "shared.md").write_text("# s\n")
    # 다른 세션(오케스트레이터 같은)이 먼저 이 경로를 자기 보관함에 올렸다.
    other_ws = (await client.get(f"/api/sessions/{other['id']}", headers=auth)).json()["workspace_id"]
    assert other_ws != ws["id"]
    h = _orch(issue_token(uuid.UUID(other["id"])))
    r = await client.post("/api/agent/share", json={"path": str(outbox / "shared.md")}, headers=h)
    assert r.status_code == 200, r.text
    async with sessionmaker()() as db:
        run = await db.get(Run, run_id)
        await RunManager()._collect_artifacts(db, run, await db.get(Workspace, run.workspace_id), outbox, ParseState())
    mine = (await client.get("/api/artifacts", params={"session_id": owner["id"]}, headers=auth)).json()
    assert [(a["filename"], a["area"]) for a in mine] == [("shared.md", "agent")]
    theirs = (await client.get("/api/artifacts", params={"session_id": other["id"]}, headers=auth)).json()
    assert [(a["filename"], a["area"]) for a in theirs] == [("shared.md", "agent")]


async def test_concurrent_share_same_path_single_row(client, auth, new_session):
    """같은 세션·원본 경로로 동시에 공유해도 활성 행은 하나다. 각 요청은 별도 DB 세션으로 돈다."""
    ws, sess = await new_session()
    root = Path((await client.get(f"/api/workspaces/{ws['id']}", headers=auth)).json()["root_path"])
    (root / "race.md").write_text("# race\n")
    h = _orch(issue_token(sess["id"]))
    resps = await asyncio.gather(*[
        client.post("/api/agent/share", json={"path": str(root / "race.md")}, headers=h)
        for _ in range(5)
    ])
    assert [r.status_code for r in resps] == [200] * 5
    assert len({r.json()["artifact"]["id"] for r in resps}) == 1
    rows = (await client.get("/api/artifacts", params={"session_id": sess["id"]}, headers=auth)).json()
    assert [(a["filename"], a["area"]) for a in rows] == [("race.md", "agent")]


async def test_removed_from_library_never_resurfaces_via_share_or_collect(client, auth, new_session):
    """사용자가 내 보관함에서 뺀 파일은 이후 자동 수집이나 AI 의 share_file 로 다시 내 보관함으로 유입되지 않는다."""
    from ttakkari.agents.base import ParseState
    from ttakkari.models import Run, Workspace
    from ttakkari.runs.manager import RunManager

    ws, sess = await new_session()
    sid = uuid.UUID(sess["id"])
    async with sessionmaker()() as db:
        run = Run(session_id=sid, workspace_id=uuid.UUID(ws["id"]), prompt="user_fav.md 파일을 보관함에 보관해줘", engine="claude", status="running")
        db.add(run)
        await db.commit()
        run_id = run.id

    outbox = get_settings().outbox_root / str(run_id)
    outbox.mkdir(parents=True)
    target = outbox / "user_fav.md"
    target.write_text("# important user data\n")
    h = _orch(issue_token(sid, run_id))

    # 1. 사용자의 요청으로 보관함에 보관됨
    r_keep = await client.post("/api/agent/keep", json={"path": str(target), "quote": "user_fav.md 파일을 보관함에 보관해줘"}, headers=h)
    assert r_keep.status_code == 200
    art = r_keep.json()["artifact"]
    assert art["area"] == "files" and art["keep_reason"] == "chat_request"

    # 2. 사용자가 화면에서 '내 보관함에서 빼기'를 실행
    r_remove = await client.patch(f"/api/artifacts/{art['id']}", json={"area": "agent"}, headers=auth)
    assert r_remove.status_code == 200
    assert r_remove.json()["area"] == "agent"
    assert r_remove.json()["keep_reason"] is None

    # 3. AI가 다시 share_file 을 호출해도 files 로 올라오지 않고 agent 로 유지
    r_share = await client.post("/api/agent/share", json={"path": str(target)}, headers=h)
    assert r_share.status_code == 200
    assert r_share.json()["artifact"]["area"] == "agent"

    # 4. Run 이 끝나고 outbox 자동 수집이 돌아도 files 로 올라오지 않음
    async with sessionmaker()() as db:
        db_run = await db.get(Run, run_id)
        await RunManager()._collect_artifacts(db, db_run, await db.get(Workspace, db_run.workspace_id), outbox, ParseState())

    # 검증: 내 보관함(files)에는 없고, AI 작업 파일(agent)에만 1개 행 유지
    assert await _areas(client, auth, session_id=str(sid), area="files") == {}
    assert await _areas(client, auth, session_id=str(sid), area="agent") == {"user_fav.md": "agent"}


async def test_move_between_areas(client, auth, new_session):
    ws, _ = await new_session()
    a = (await client.post("/api/uploads", files={"file": ("up.md", b"# up", "text/markdown")},
                           data={"workspace_id": ws["id"]}, headers=auth)).json()
    r = await client.patch(f"/api/artifacts/{a['id']}", json={"area": "agent"}, headers=auth)
    assert r.status_code == 200 and r.json()["area"] == "agent"
    assert await _areas(client, auth, area="files") == {}
    assert await _areas(client, auth, area="agent") == {"up.md": "agent"}
    r = await client.patch(f"/api/artifacts/{a['id']}", json={"area": "files"}, headers=auth)
    assert r.json()["area"] == "files"
    assert (await client.patch(f"/api/artifacts/{a['id']}", json={"area": "bogus"}, headers=auth)).status_code == 422
    assert (await client.patch(f"/api/artifacts/{a['id']}", json={"area": "agent"})).status_code == 401


def _migration():
    p = Path(__file__).parent.parent / "alembic" / "versions" / "0014_artifact_area.py"
    spec = importlib.util.spec_from_file_location("m0014", p)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


async def test_migration_backfill_rules(client, auth, make_dir):
    """마이그레이션의 UPDATE 규칙: 업로드는 반드시 파일, 불확실하면 에이전트."""
    chat = await _session(client, auth, make_dir)
    orch = await _session(client, auth, make_dir, kind="orchestrator")
    worker = await _session(client, auth, make_dir, kind="chat")
    async with sessionmaker()() as db:
        (await db.get(ChatSession, uuid.UUID(worker["id"]))).kind = "worker"
        await db.commit()

    def art(source, sid=None, name="f"):
        return Artifact(workspace_id=None, session_id=sid, filename=name, mime_type="text/plain", kind="other",
                        source=source, original_path="/x", storage_mode="copy", stored_path="/x", size_bytes=1, sha256="0" * 64)

    rows = {
        "upload": art("upload", name="upload"), "reg": art("user_registered", name="reg"),
        "chat_out": art("agent_output", uuid.UUID(chat["id"]), "chat_out"),
        "orch_out": art("agent_output", uuid.UUID(orch["id"]), "orch_out"),
        "worker_out": art("agent_output", uuid.UUID(worker["id"]), "worker_out"),
        "orphan_out": art("agent_output", None, "orphan_out"),
        "modified": art("agent_modified", uuid.UUID(chat["id"]), "modified"),
        "unknown": art("weird", uuid.UUID(chat["id"]), "unknown"),
    }
    async with sessionmaker()() as db:
        db.add_all(rows.values())
        await db.flush()
        await db.execute(update(Artifact).values(area=ArtifactArea.agent))  # 컬럼이 막 생긴 상태(기본 agent)
        stmts: list[str] = []

        class FakeOp:
            def __getattr__(self, name):
                if name == "execute":
                    return stmts.append
                return lambda *a, **k: None

        mod = _migration()
        mod.op = FakeOp()
        mod.upgrade()
        for s in stmts:
            await db.execute(text(s))
        await db.commit()
        got = {n: a for n, a in (await db.execute(select(Artifact.filename, Artifact.area))).all()}
    assert got == {"upload": "files", "reg": "files", "chat_out": "files", "orch_out": "files",
                   "worker_out": "agent", "orphan_out": "agent", "modified": "agent", "unknown": "agent"}


def _migration_0052():
    p = Path(__file__).parent.parent / "alembic" / "versions" / "0052_artifact_area_byproducts.py"
    spec = importlib.util.spec_from_file_location("m0052", p)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


async def test_migration_0052_moves_byproducts_only():
    """0052: 파일 탭의 부산물(로그·증거 JSON 등)만 에이전트로. 결과물·업로드·삭제분은 그대로."""
    from ttakkari.models import utcnow

    outbox = get_settings().outbox_root

    def art(source, name, rel=None, area="files", deleted=False, original=None):
        # 자동 수집 행은 원본 경로가 outbox/<run>/<rel_path> 이다.
        if original is None:
            original = str(outbox / "run1" / rel) if rel else "/x"
        return Artifact(workspace_id=None, session_id=None, filename=name, mime_type="text/plain", kind="other",
                        source=source, area=area, original_path=original, rel_path=rel, storage_mode="copy",
                        stored_path="/x", size_bytes=1, sha256="0" * 64, deleted_at=utcnow() if deleted else None)

    move = [  # files → agent 로 옮겨야 한다(확실한 부산물만)
        art("agent_output", "m52-watch.log", "m52-watch.log"),
        art("agent_output", "m52-a1b2-worker-evidence.json", "m52-a1b2-worker-evidence.json"),
        art("agent_output", "m52-page-2026-10-19.png", "browser/page-2026-10-19.png"),
        art("agent_output", "m52-page-2026-10-19.yml", "browser/page-2026-10-19.yml"),
        art("agent_output", "m52-keychain-selection.json", "evidence/m52-keychain-selection.json"),
        art("agent_modified", "m52-build.log"),
    ]
    stay = [  # 그대로 files (또는 agent)
        art("agent_output", "m52-report.md", "m52-report.md"),
        art("agent_output", "m52-saas-1280-compare.png", "m52-saas-1280-compare.png"),
        art("agent_output", "m52-aip-wbs-evidence-20261015.zip", "m52-aip-wbs-evidence-20261015.zip"),
        art("agent_output", "m52-cleanup-plan.md", "m52-cleanup-plan.md"),
        art("agent_output", "m52-snapshot-analysis.md", "m52-snapshot-analysis.md"),
        # 이름이 부산물처럼 보여도 정상 산출물일 수 있어 옮기지 않는다(애매하면 유지).
        art("agent_output", "m52-snapshot-analysis.json", "m52-snapshot-analysis.json"),
        art("agent_output", "m52-cleanup-plan.json", "m52-cleanup-plan.json"),
        art("agent_output", "m52-final-report.json", "m52-final-report.json"),
        art("agent_output", "m52-check-results.py", "m52-check-results.py"),
        art("agent_output", "m52-membership-bindings.json", "m52-membership-bindings.json"),
        art("agent_output", "m52-worker-evidence-summary.json", "m52-worker-evidence-summary.json"),
        # 폴더 규칙은 outbox 바로 아래 폴더만. 더 깊은 경로나 share 로 올린 행(rel_path 앞에 run 폴더)은 유지한다.
        art("agent_output", "m52-summary.md", "evidence/m52-summary.md"),
        art("agent_output", "m52-page-notes.md", "browser/m52-page-notes.md"),
        art("agent_output", "m52-keychain-selection.txt", "evidence/m52-keychain-selection.txt"),
        art("agent_output", "m52-nested.txt", "docs/evidence/m52-nested.txt"),
        art("agent_output", "m52-shared.log", "run1/m52-shared.log", original=str(outbox / "run1" / "m52-shared.log")),
        art("agent_output", "m52-ws-shared.log", "m52-ws-shared.log", original="/ws/m52-ws-shared.log"),
        art("upload", "m52-up.log"),
        art("user_registered", "m52-reg.log"),
        art("agent_output", "m52-old.log", "m52-old.log", area="agent"),
        art("agent_output", "m52-gone.log", "m52-gone.log", deleted=True),
    ]

    class FakeOp:
        def __init__(self, sink: list):
            self._sink = sink

        def __getattr__(self, name):
            if name == "execute":
                return self._sink.append
            return lambda *a, **k: None

    async with sessionmaker()() as db:
        await db.execute(text("DROP TABLE IF EXISTS artifact_area_backup_0052"))
        await db.commit()
        db.add_all(move + stay)
        await db.commit()
        ids = [r.id for r in move + stay]
        mod = _migration_0052()
        stmts: list[str] = []
        mod.op = FakeOp(stmts)
        mod.upgrade()
        assert len(stmts) == 2  # 백업 표 생성 + UPDATE
        for s in stmts:
            await db.execute(text(s))
        await db.commit()
        # 세션은 expire_on_commit=False라 raw SQL 결과를 ORM이 모른다. 만료 후 실제 DB 상태를 읽는다.
        db.expire_all()
        got = {r.filename: r.area for r in
               (await db.execute(select(Artifact).where(Artifact.id.in_(ids)))).scalars()}
        assert [got[r.filename] for r in move] == ["agent"] * len(move)
        assert [got[r.filename] for r in stay] == ["files"] * (len(stay) - 2) + ["agent", "files"]
        # 백업 표에 영향 행의 원래 분류가 남고 downgrade 로 되돌릴 수 있다.
        n_backup = (await db.execute(text("SELECT COUNT(*) FROM artifact_area_backup_0052"))).scalar()
        assert n_backup == len(move)
        down: list[str] = []
        mod.op = FakeOp(down)
        mod.downgrade()
        for s in down:
            await db.execute(text(s))
        await db.commit()
        db.expire_all()
        back = {r.filename: r.area for r in
                (await db.execute(select(Artifact).where(Artifact.id.in_(ids)))).scalars()}
        assert back["m52-watch.log"] == "files" and back["m52-a1b2-worker-evidence.json"] == "files"
        assert back["m52-old.log"] == "agent"  # 백업에 없던 행은 그대로
        await db.execute(text("DROP TABLE IF EXISTS artifact_area_backup_0052"))
        for r in move + stay:
            await db.delete(r)
        await db.commit()


def _migration_0068():
    p = Path(__file__).parent.parent / "alembic" / "versions" / "0068_library_intent.py"
    spec = importlib.util.spec_from_file_location("m0068", p)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


async def test_migration_0068_library_intent():
    """0068: 업로드만 files 유지, AI 생성·공유 및 불명확 행은 agent 로 이동.
    category, origin, keep_reason 백필 및 백업/롤백 검증."""
    def art(source, name, area="files", mime="text/plain", kind="other"):
        return Artifact(
            workspace_id=None, session_id=None, filename=name, mime_type=mime, kind=kind,
            source=source, area=area, original_path=f"/path/{name}", rel_path=name,
            storage_mode="copy", stored_path=f"/store/{name}", size_bytes=100, sha256="a" * 64,
        )

    items = [
        # 1. 업로드 (files 유지, origin=user, keep_reason=direct_upload)
        art("upload", "doc.pdf", "files", mime="application/pdf", kind="pdf"),
        art("upload", "data.csv", "files", mime="text/csv", kind="sheet"),
        # 2. 사용자 등록 파일 (files 에 있었더라도 0068 에서 agent 로 이동, origin=unknown 백필)
        art("user_registered", "registered.md", "files", mime="text/markdown", kind="markdown"),
        # 3. AI 작업 파일인데 레거시로 files 에 있던 것 (agent 로 이동, origin=agent, keep_reason=None)
        art("agent_output", "report.md", "files", mime="text/markdown", kind="markdown"),
        art("agent_output", "slide.pptx", "files", mime="application/vnd.ms-powerpoint", kind="pptx"),
        art("agent_output", "video.mp4", "files", mime="video/mp4", kind="video"),
        art("agent_output", "audio.mp3", "files", mime="audio/mpeg", kind="audio"),
        art("agent_output", "archive.zip", "files", mime="application/zip", kind="other"),
        art("agent_output", "script.py", "files", mime="text/x-python", kind="code"),
        art("agent_output", "notes.txt", "files", mime="text/plain", kind="code"),
        # 4. 원래 agent 였던 것 (agent 유지)
        art("agent_output", "temp.log", "agent", mime="text/plain", kind="code"),
        art("agent_modified", "main.rs", "agent", mime="text/rust", kind="code"),
    ]

    class FakeOp:
        def __init__(self, sink: list):
            self._sink = sink

        def __getattr__(self, name):
            if name == "execute":
                return self._sink.append
            return lambda *a, **k: None

    async with sessionmaker()() as db:
        await db.execute(text("DROP TABLE IF EXISTS artifact_area_backup_0068"))
        await db.commit()
        db.add_all(items)
        await db.commit()
        ids = [r.id for r in items]

        mod = _migration_0068()
        stmts: list[str] = []
        mod.op = FakeOp(stmts)
        # 테스트 DB 는 모델로 만들어져 새 컬럼이 이미 있다. 0068 이전 스키마였던 것처럼 첫 적용 경로를 탄다.
        mod._existing_columns = lambda: set()
        mod.upgrade()

        for s in stmts:
            await db.execute(text(s))
        await db.commit()
        db.expire_all()

        rows = {r.filename: r for r in (await db.execute(select(Artifact).where(Artifact.id.in_(ids)))).scalars()}

        # 유지/이동 검증
        assert rows["doc.pdf"].area == "files" and rows["doc.pdf"].origin == "user"
        assert rows["doc.pdf"].keep_reason == "direct_upload"
        assert rows["doc.pdf"].category == "document"

        assert rows["data.csv"].area == "files" and rows["data.csv"].origin == "user"
        assert rows["data.csv"].category == "spreadsheet"

        # user_registered 파일은 0068 에서 agent 로 이동하고 origin=unknown 백필
        assert rows["registered.md"].area == "agent" and rows["registered.md"].origin == "unknown"
        assert rows["registered.md"].keep_reason is None

        # AI 결과물은 전부 agent 로 이동!
        assert rows["report.md"].area == "agent" and rows["report.md"].origin == "agent"
        assert rows["report.md"].keep_reason is None
        assert rows["report.md"].category == "document"

        assert rows["slide.pptx"].area == "agent" and rows["slide.pptx"].category == "presentation"
        assert rows["video.mp4"].area == "agent" and rows["video.mp4"].category == "video"
        assert rows["audio.mp3"].area == "agent" and rows["audio.mp3"].category == "audio"
        assert rows["archive.zip"].area == "agent" and rows["archive.zip"].category == "archive"
        assert rows["script.py"].area == "agent" and rows["script.py"].category == "code"
        assert rows["notes.txt"].area == "agent" and rows["notes.txt"].category == "document"

        # 백업 표 검증
        n_backup = (await db.execute(text("SELECT COUNT(*) FROM artifact_area_backup_0068"))).scalar()
        assert n_backup == len(items)

        # 롤백(downgrade) 검증
        down: list[str] = []
        mod.op = FakeOp(down)
        mod._existing_columns = lambda: set()
        mod._has_backup_table = lambda: True
        mod.downgrade()
        for s in down:
            await db.execute(text(s))
        await db.commit()
        db.expire_all()

        back = {r.filename: r for r in (await db.execute(select(Artifact).where(Artifact.id.in_(ids)))).scalars()}
        assert back["report.md"].area == "files"  # 원래대로 복원
        assert back["doc.pdf"].area == "files"

        await db.execute(text("DROP TABLE IF EXISTS artifact_area_backup_0068"))
        for r in items:
            await db.delete(r)
        await db.commit()


async def test_twelve_scenarios_comprehensive(client, auth, new_session):
    """요구사항 12대 필수 시나리오 통합 검증:
    1. 직접 업로드 → 내 보관함
    2. AI 생성 → AI 작업 파일
    3. AI 자발 공유 → 내 보관함 아님
    4. 공유 결과물 대화에서 열기·다운로드 정상
    5. 보관 요청으로 AI 파일 보관 → 내 보관함 + AI 출처 유지 + 근거 기록
    6. 링크 열기/다운로드만으로 미보관
    7. 빼낸 파일이 자동 수집·재공유로 재유입 안 됨
    8. 영상·음성·압축·txt·문서 카테고리 필터
    9. 필터+검색이 페이지네이션 넘어 전체 적용
    10. 기존 미확인 파일 AI 작업 파일로 이동 + 링크·내용 유지
    11. 재시도·중복 호출·마이그레이션 재실행 무오염
    """
    from ttakkari.agents.base import ParseState
    from ttakkari.models import Run, Workspace
    from ttakkari.runs.manager import RunManager

    ws, sess = await new_session()
    sid = uuid.UUID(sess["id"])
    ws_id = uuid.UUID(ws["id"])

    # 시나리오 1: 직접 업로드 → 내 보관함
    up_resp = await client.post("/api/uploads", files={"file": ("my_upload.pdf", b"%PDF-1.4\n...", "application/pdf")},
                               data={"workspace_id": str(ws_id)}, headers=auth)
    assert up_resp.status_code == 201
    up_art = up_resp.json()
    assert up_art["area"] == "files"
    assert up_art["origin"] == "user"
    assert up_art["keep_reason"] == "direct_upload"
    assert up_art["category"] == "document"

    # 시나리오 2: AI 생성 → AI 작업 파일
    async with sessionmaker()() as db:
        run = Run(session_id=sid, workspace_id=ws_id, prompt="엑셀 만들어서 보관함에 넣어줘", engine="claude", status="running")
        db.add(run)
        await db.commit()
        run_id = run.id

    outbox = get_settings().outbox_root / str(run_id)
    outbox.mkdir(parents=True, exist_ok=True)
    ai_file = outbox / "ai_result.xlsx"
    ai_file.write_bytes(b"PK\x03\x04 fake sheet")

    async with sessionmaker()() as db:
        db_run = await db.get(Run, run_id)
        await RunManager()._collect_artifacts(db, db_run, await db.get(Workspace, ws_id), outbox, ParseState())

    ai_art = (await client.get("/api/artifacts", params={"session_id": str(sid), "q": "ai_result"}, headers=auth)).json()[0]
    assert ai_art["area"] == "agent"
    assert ai_art["origin"] == "agent"
    assert ai_art["keep_reason"] is None
    assert ai_art["category"] == "spreadsheet"

    # 시나리오 3: AI 자발 공유 → 내 보관함 아님
    h = _orch(issue_token(sid, run_id))
    sh_resp = await client.post("/api/agent/share", json={"path": str(ai_file)}, headers=h)
    assert sh_resp.status_code == 200
    assert sh_resp.json()["artifact"]["area"] == "agent"

    # 시나리오 4: 공유 결과물 대화에서 열기·다운로드 정상
    content_resp = await client.get(f"/api/artifacts/{ai_art['id']}/content", headers=auth)
    assert content_resp.status_code == 200
    assert content_resp.content == b"PK\x03\x04 fake sheet"

    dl_resp = await client.post(f"/api/artifacts/{ai_art['id']}/download-link", headers=auth)
    assert dl_resp.status_code == 200
    dl_url = dl_resp.json()["url"]
    dl_file_resp = await client.get(dl_url)
    assert dl_file_resp.status_code == 200 and dl_file_resp.content == b"PK\x03\x04 fake sheet"

    # 시나리오 5: 보관 요청으로 AI 파일 보관 → 내 보관함 + AI 출처 유지 + 근거 기록
    keep_resp = await client.post("/api/agent/keep", json={"path": str(ai_file), "quote": "엑셀 만들어서 보관함에 넣어줘"}, headers=h)
    assert keep_resp.status_code == 200
    kept_art = keep_resp.json()["artifact"]
    assert kept_art["area"] == "files"
    assert kept_art["origin"] == "agent"  # 출처는 AI 유지!
    assert kept_art["keep_reason"] == "chat_request"
    assert kept_art["keep_evidence"]["quote"] == "엑셀 만들어서 보관함에 넣어줘"
    assert kept_art["keep_evidence"]["message_id"] == str(run_id)
    assert kept_art["keep_evidence"]["message_source"] == "message"
    assert kept_art["keep_evidence"]["binding"] == "same_run"  # 이 Run 이 만든 파일 + 파일명 없는 문장

    # 시나리오 6: 링크 열기/다운로드만으로 미보관
    ws_root = Path(ws["root_path"])
    sample_txt = ws_root / "sample_link.txt"
    sample_txt.write_text("just browsing this file")
    resolve_resp = await client.post("/api/artifacts/resolve", json={"path": str(sample_txt), "workspace_id": str(ws_id)}, headers=auth)
    assert resolve_resp.status_code == 200
    resolved_art = resolve_resp.json()
    assert resolved_art["area"] == "agent"  # 링크 열람만으로 files 에 안 들어감!
    assert resolved_art["keep_reason"] is None

    # 시나리오 7: 빼낸 파일이 자동 수집·재공유로 재유입 안 됨
    remove_resp = await client.patch(f"/api/artifacts/{kept_art['id']}", json={"area": "agent"}, headers=auth)
    assert remove_resp.status_code == 200 and remove_resp.json()["area"] == "agent"
    # 재공유
    await client.post("/api/agent/share", json={"path": str(ai_file)}, headers=h)
    # 자동 수집 재실행
    async with sessionmaker()() as db:
        db_run = await db.get(Run, run_id)
        await RunManager()._collect_artifacts(db, db_run, await db.get(Workspace, ws_id), outbox, ParseState())
    after_art = (await client.get(f"/api/artifacts/{kept_art['id']}", headers=auth)).json()
    assert after_art["area"] == "agent"  # 여전히 agent 유지!

    # 시나리오 8: 영상·음성·압축·txt·문서 카테고리 필터
    various_files = [
        ("video.mp4", b"mp4", "video/mp4", "video"),
        ("audio.wav", b"wav", "audio/wav", "audio"),
        ("bundle.zip", b"zip", "application/zip", "archive"),
        ("memo.txt", b"txt", "text/plain", "document"),
        ("doc.md", b"md", "text/markdown", "document"),
    ]
    for name, content, mime, expected_cat in various_files:
        p = outbox / name
        p.write_bytes(content)
        r = await client.post("/api/agent/share", json={"path": str(p)}, headers=h)
        assert r.status_code == 200
        cat_got = r.json()["artifact"]["category"]
        assert cat_got == expected_cat, f"{name}: expected {expected_cat}, got {cat_got}"

    # 카테고리 필터 조회 검증
    for cat in ("video", "audio", "archive", "document"):
        cat_list = (await client.get("/api/artifacts", params={"session_id": str(sid), "category": cat}, headers=auth)).json()
        assert len(cat_list) > 0
        assert all(a["category"] == cat for a in cat_list)

    # 시나리오 9: 필터+검색이 페이지네이션 넘어 전체 적용
    paged = (await client.get("/api/artifacts", params={"session_id": str(sid), "category": "document", "q": "memo", "limit": 10}, headers=auth)).json()
    assert len(paged) == 1 and paged[0]["filename"] == "memo.txt"

    # 시나리오 11: 재시도·중복 호출 무오염
    before_count = len((await client.get("/api/artifacts", params={"session_id": str(sid)}, headers=auth)).json())
    for _ in range(3):
        await client.post("/api/agent/share", json={"path": str(ai_file)}, headers=h)
        await client.post("/api/agent/keep", json={"path": str(ai_file), "quote": "엑셀 만들어서 보관함에 넣어줘"}, headers=h)
    after_count = len((await client.get("/api/artifacts", params={"session_id": str(sid)}, headers=auth)).json())
    assert before_count == after_count


async def test_agent_move_artifact_forbidden_and_conflict(client, auth, new_session):
    """에이전트가 PATCH 로 files 로 옮기려 하면 409 거부(keep_artifact 쓰라는 안내).
    또한 사용자가 보관한 파일(keep_reason 있음)을 agent 로 빼려 해도 403 거부."""
    ws, sess = await new_session()
    sid = uuid.UUID(sess["id"])
    h = _orch(issue_token(sid))

    # 1. AI 임시 파일 생성
    async with sessionmaker()() as db:
        art = Artifact(
            session_id=sid, workspace_id=uuid.UUID(ws["id"]), filename="temp.txt",
            mime_type="text/plain", kind="code", source="agent_output", area=ArtifactArea.agent,
            origin="agent", original_path="/temp.txt", stored_path="/temp.txt", storage_mode="reference",
            size_bytes=10, sha256="0" * 64,
        )
        db.add(art)
        await db.commit()
        art_id = art.id

    # 에이전트가 PATCH 로 area=files 승격 시도 -> 409 거부!
    r = await client.patch(f"/api/agent/artifacts/{art_id}", json={"area": "files"}, headers=h)
    assert r.status_code == 409
    assert r.json()["code"] == "use_keep_artifact"

    # 2. 사용자가 보관한 파일(keep_reason 있음) 생성
    async with sessionmaker()() as db:
        kept = Artifact(
            session_id=sid, workspace_id=uuid.UUID(ws["id"]), filename="kept.txt",
            mime_type="text/plain", kind="code", source="agent_output", area=ArtifactArea.files,
            origin="agent", keep_reason="chat_request", original_path="/kept.txt", stored_path="/kept.txt",
            storage_mode="reference", size_bytes=10, sha256="0" * 64,
        )
        db.add(kept)
        await db.commit()
        kept_id = kept.id

    # 에이전트가 files -> agent 빼기 시도 -> 403 거부!
    r2 = await client.patch(f"/api/agent/artifacts/{kept_id}", json={"area": "agent"}, headers=h)
    assert r2.status_code == 403
    assert r2.json()["code"] == "user_kept"


async def _mk_run(ws_id: str, sid: uuid.UUID, prompt: str, *, origin: str = "user", status: str = "succeeded",
                  at: datetime | None = None) -> uuid.UUID:
    from ttakkari.models import Run

    async with sessionmaker()() as db:
        run = Run(session_id=sid, workspace_id=uuid.UUID(ws_id), prompt=prompt, engine="claude", status=status, origin=origin)
        if at is not None:
            run.created_at = at
        db.add(run)
        await db.commit()
        return run.id


async def _keep(client, sid: uuid.UUID, run_id: uuid.UUID | None, path: Path, quote: str):
    return await client.post("/api/agent/keep", json={"path": str(path), "quote": quote},
                             headers=_orch(issue_token(sid, run_id)))


async def _artifact_rows(sid: uuid.UUID) -> list[Artifact]:
    async with sessionmaker()() as db:
        return list(await db.scalars(select(Artifact).where(Artifact.session_id == sid)))


def _outbox_file(run_id: uuid.UUID, name: str, body: str = "# made") -> Path:
    d = get_settings().outbox_root / str(run_id)
    d.mkdir(parents=True, exist_ok=True)
    f = d / name
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(body)
    return f


async def test_keep_without_user_request_is_rejected_and_changes_nothing(client, auth, new_session):
    """보관 요청이 없던 Run 에서 임의 인용으로 keep 하면 422, 행·area·근거 모두 그대로다(r1 결함 1 재현)."""
    ws, sess = await new_session()
    sid = uuid.UUID(sess["id"])
    f = Path(ws["root_path"]) / "summary.md"
    f.write_text("# s")
    run_id = await _mk_run(ws["id"], sid, "이 파일을 요약만 해줘. 보관 요청은 없음.", status="running")

    shared = await client.post("/api/agent/share", json={"path": str(f)}, headers=_orch(issue_token(sid, run_id)))
    assert shared.status_code == 200 and shared.json()["artifact"]["area"] == "agent"
    before = await _artifact_rows(sid)

    for quote in ("사용자가 보관 요청함", "보관함에 넣어줘", "x", "   ", "보관 요청은 없음", "summary.md 를 보관함에 넣어줘"):
        r = await _keep(client, sid, run_id, f, quote)
        assert r.status_code == 422, (quote, r.text)
        assert r.json()["code"] == "quote_not_found", (quote, r.text)

    after = await _artifact_rows(sid)
    assert [(a.id, a.area, a.keep_reason, a.keep_evidence) for a in after] == \
        [(a.id, a.area, a.keep_reason, a.keep_evidence) for a in before]
    assert after[0].area == "agent" and after[0].keep_reason is None

    g = Path(ws["root_path"]) / "fresh.md"
    g.write_text("# fresh")
    assert (await _keep(client, sid, run_id, g, "임의로 지어낸 요청 문장입니다")).status_code == 422
    assert len(await _artifact_rows(sid)) == len(before)


async def test_keep_rejects_short_negative_quote_and_quote_reused_for_another_file(client, new_session):
    """r2 반례: '보관하지 마' 발화에서 '보관' 만 인용, 그리고 다른 파일을 가리키는 문장을 무관한 파일에 재사용."""
    ws, sess = await new_session()
    sid = uuid.UUID(sess["id"])
    root = Path(ws["root_path"])
    t0 = datetime.now(UTC)
    await _mk_run(ws["id"], sid, "이 파일은 보관하지 마. 내용만 확인해줘.", at=t0 - timedelta(minutes=6))
    await _mk_run(ws["id"], sid, "보고서.md를 내 보관함에 보관해줘", at=t0 - timedelta(minutes=5))
    run_id = await _mk_run(ws["id"], sid, "계속", status="running", at=t0)
    neg = root / "neg.md"
    neg.write_text("# n")
    other = root / "other-result.txt"
    other.write_text("other")
    report = root / "보고서.md"
    report.write_text("# 보고서")

    r = await _keep(client, sid, run_id, neg, "보관")
    assert r.status_code == 422 and r.json()["code"] == "quote_not_found"
    r = await _keep(client, sid, run_id, neg, "이 파일은 보관하지 마")
    assert r.status_code == 422 and r.json()["code"] == "quote_negated"
    r = await _keep(client, sid, run_id, other, "보고서.md를 내 보관함에 보관해줘")
    assert r.status_code == 422 and r.json()["code"] == "quote_not_bound"
    assert await _artifact_rows(sid) == []

    r = await _keep(client, sid, run_id, report, "보고서.md를 내 보관함에 보관해줘")
    assert r.status_code == 200, r.text
    ev = r.json()["artifact"]["keep_evidence"]
    assert r.json()["artifact"]["area"] == "files" and ev["binding"] == "filename" and ev["binding_detail"] == "보고서.md"
    assert ev["quote_sentence"] == "보고서.md를 내 보관함에 보관해줘"


async def test_keep_binds_only_to_what_the_requesting_run_made(client, new_session):
    """same_run: 발화를 담은 Run 자체, 또는 그 Run 실행 중에 시작된 자식 작업 세션 Run 의 결과물만. 그 외·부산물은 파일명을 써야 한다."""
    ws, sess = await new_session()
    _, other = await new_session()
    sid, other_sid = uuid.UUID(sess["id"]), uuid.UUID(other["id"])
    t0 = datetime.now(UTC)
    early = await _mk_run(ws["id"], sid, "예전 작업", at=t0 - timedelta(minutes=30))
    msg = await _mk_run(ws["id"], sid, "만들어서 보관함에 넣어줘", at=t0 - timedelta(minutes=10))
    async with sessionmaker()() as db:
        run = await db.get(Run, msg)
        run.started_at, run.finished_at = t0 - timedelta(minutes=10), t0 - timedelta(minutes=4)
        child = ChatSession(workspace_id=uuid.UUID(ws["id"]), title="worker", engine="claude", parent_id=sid)
        db.add(child)
        await db.commit()
        child_id = child.id
    worker_in = await _mk_run(ws["id"], child_id, "작업자 지시", origin="orchestrator", at=t0 - timedelta(minutes=8))
    worker_after = await _mk_run(ws["id"], child_id, "나중 작업자 지시", origin="orchestrator", at=t0 - timedelta(minutes=2))
    next_turn = await _mk_run(ws["id"], sid, "다른 것도 분석해줘", at=t0 - timedelta(minutes=3))
    stranger_run = await _mk_run(other["workspace_id"], other_sid, "다른 대화 작업", at=t0 - timedelta(minutes=9))
    keeper = await _mk_run(ws["id"], sid, "보고 정리", origin="report", status="running", at=t0)

    quote = "만들어서 보관함에 넣어줘"
    # 정상: 발화를 처리한 Run 이 만든 결과, 그 Run 중에 위임한 작업 세션이 만든 결과(보관 Run 은 그 뒤의 보고 Run 이어도 된다)
    r = await _keep(client, sid, keeper, _outbox_file(msg, "result.md"), quote)
    assert r.status_code == 200, r.text
    ev = r.json()["artifact"]["keep_evidence"]
    assert ev["binding"] == "same_run" and ev["binding_detail"] == str(msg) and ev["message_id"] == str(msg)
    r = await _keep(client, sid, keeper, _outbox_file(worker_in, "from-worker.md"), quote)
    assert r.status_code == 200 and r.json()["artifact"]["keep_evidence"]["binding_detail"] == str(worker_in)

    refused = {
        # LATER_UNRELATED: 첫 턴의 보관 요청으로 다음 턴의 무관한 파일은 못 보관한다.
        "next-turn": _outbox_file(next_turn, "unrelated.csv"),
        "early": _outbox_file(early, "old.md"),
        "stranger": _outbox_file(stranger_run, "stranger.md"),
        # 요청 Run 이 끝난 뒤 시작된 자식 Run
        "worker-after": _outbox_file(worker_after, "late-worker.md"),
        # CHILD_BYPRODUCT 와 부산물
        "child-screenshot": _outbox_file(worker_in, "debug-screenshot.png"),
        "run-log": _outbox_file(msg, "run.log"),
        "browser-capture": _outbox_file(msg, "browser/page-1.yml"),
        "evidence-json": _outbox_file(msg, "tmp-evidence.json"),
        "verification-dir": _outbox_file(msg, ".verification/notes.md"),
    }
    stale = _outbox_file(msg, "stale.md")
    os.utime(stale, (t0.timestamp() - 86400, t0.timestamp() - 86400))
    refused["stale-mtime"] = stale
    plain = Path(ws["root_path"]) / "plain.md"
    plain.write_text("# plain")
    refused["workspace-file"] = plain
    for label, f in refused.items():
        r = await _keep(client, sid, keeper, f, quote)
        assert r.status_code == 422 and r.json()["code"] == "quote_not_bound", (label, r.text)
    assert sorted(a.filename for a in await _artifact_rows(sid)) == ["from-worker.md", "result.md"]

    # 제외된 부산물도 사용자 문장에 파일명을 직접 쓰면 보관된다.
    await _mk_run(ws["id"], sid, "debug-screenshot.png 를 내 보관함에 보관해줘", at=t0 - timedelta(seconds=30))
    keeper2 = await _mk_run(ws["id"], sid, "정리", origin="report", status="running", at=t0 + timedelta(seconds=1))
    r = await _keep(client, sid, keeper2, refused["child-screenshot"], "debug-screenshot.png 를 내 보관함에 보관해줘")
    assert r.status_code == 200 and r.json()["artifact"]["keep_evidence"]["binding"] == "filename"


async def test_keep_requires_a_keep_expression_in_the_quoted_sentence(client, new_session):
    """NO_KEEP_INTENT: 보관 표현이 없는 문장은 파일 이름이 같은 단어가 있어도 근거가 못 된다(확장자 뺀 이름 매칭은 없다)."""
    ws, sess = await new_session()
    sid = uuid.UUID(sess["id"])
    f = Path(ws["root_path"]) / "report.md"
    f.write_text("# report")
    await _mk_run(ws["id"], sid, "The report was clear and useful.\nreport.md 내용이 정말 좋네요 감사합니다", at=datetime.now(UTC) - timedelta(minutes=1))
    run_id = await _mk_run(ws["id"], sid, "진행", status="running")
    for quote in ("The report was clear and useful.", "report.md 내용이 정말 좋네요 감사합니다"):
        r = await _keep(client, sid, run_id, f, quote)
        assert r.status_code == 422 and r.json()["code"] == "quote_no_keep_request", (quote, r.text)
    assert await _artifact_rows(sid) == []


NEGATIVE_SENTENCES = [
    "report.md를 내 보관함에 넣는 건 하지 말아줘",  # NEGATION_GAP
    "report.md 는 보관하지 마",
    "report.md 내 보관함에 넣지 마세요",
    "report.md 저장하지 말아줘 제발",
    "report.md 는 보관 말고 요약만 해줘",
    "report.md 보관함에 넣지 말고 읽기만 해줘",
    "report.md 는 보관함에 안 넣어도 돼",
    "report.md 를 보관함에 넣을 필요 없어",
    "report.md 빼고 전부 보관함에 넣어줘",
    "report.md 제외하고 보관해줘",
    "report.md 없이 보관함에 넣어줘",
    "report.md 를 보관해 달라고 한 게 아니야 단지 질문이야",
    "please don't keep report.md in my library",
    "do not save report.md",
    "never store report.md",
    "keep everything without report.md",
]


@pytest.mark.parametrize("sentence", NEGATIVE_SENTENCES)
async def test_keep_rejects_negative_sentences_even_when_they_name_the_file(client, new_session, sentence):
    ws, sess = await new_session()
    sid = uuid.UUID(sess["id"])
    f = Path(ws["root_path"]) / "report.md"
    f.write_text("# neg")
    await _mk_run(ws["id"], sid, sentence, at=datetime.now(UTC) - timedelta(minutes=1))
    run_id = await _mk_run(ws["id"], sid, "진행", status="running")
    r = await _keep(client, sid, run_id, f, sentence)
    assert r.status_code == 422 and r.json()["code"] == "quote_negated", (sentence, r.text)
    assert await _artifact_rows(sid) == []


@pytest.mark.parametrize("sentence", [
    "report.md 보관해줘라는 문장이 자연스러운지 설명해줘",  # r4: 인용된 말
    "archive.md라는 파일 이름의 뜻을 설명해줘",  # r4: 파일명 속 단어
    "report.md 보관 지시문을 요약해줘",  # r4: 명사 수식어
    "archive 폴더 구조를 설명해줘",
    "보관함에 넣는 방법 알려줘",
    "keep.md 내용 보여줘",
    "save_report.py를 리뷰해줘",
    '"보관해줘"라고 쓰면 되나요?',
    "report.md 를 보관함에 넣어줘 그리고 요약해줘",  # 마지막 요청이 보관이 아님
    "Can you explain what archive means in report.md",
])
async def test_keep_rejects_sentences_that_mention_keeping_without_asking_for_it(client, new_session, sentence):
    """보관 표현이 문장 어디에 있든 인정하지 않는다. 파일명·인용·수식어를 뺀 뒤 마지막 서술 요청이 보관 동작이어야 한다."""
    ws, sess = await new_session()
    sid = uuid.UUID(sess["id"])
    root = Path(ws["root_path"])
    files = [root / name for name in ("report.md", "archive.md", "keep.md", "save_report.py")]
    for f in files:
        f.write_text("x")
    await _mk_run(ws["id"], sid, sentence, at=datetime.now(UTC) - timedelta(minutes=1))
    run_id = await _mk_run(ws["id"], sid, "진행", status="running")
    for f in files:
        r = await _keep(client, sid, run_id, f, sentence)
        assert r.status_code == 422 and r.json()["code"] == "quote_no_keep_request", (sentence, f.name, r.text)
    assert await _artifact_rows(sid) == []


@pytest.mark.parametrize(("sentence", "name"), [
    ("보고서.md를 내 보관함에 보관해줘", "보고서.md"),
    ("정리해서 report.md 저장해줘", "report.md"),
    ("Please save report.md to my library", "report.md"),
    ("report.md 내 보관함에 남겨 둬 부탁해요", "report.md"),
    ("이 파일 report.md 를 라이브러리에 넣어 주세요", "report.md"),
])
async def test_keep_accepts_requests_whose_last_predicate_is_keeping(client, new_session, sentence, name):
    ws, sess = await new_session()
    sid = uuid.UUID(sess["id"])
    f = Path(ws["root_path"]) / name
    f.write_text("# x")
    await _mk_run(ws["id"], sid, sentence, at=datetime.now(UTC) - timedelta(minutes=1))
    run_id = await _mk_run(ws["id"], sid, "진행", status="running")
    r = await _keep(client, sid, run_id, f, sentence)
    assert r.status_code == 200, (sentence, r.text)


@pytest.mark.parametrize("sentence", [
    "보고서.md를 내 보관함에 보관해줘",
    "그 보고서.md 파일 내 보관함에 보관해줘",
    "보고서.md는 보관함에 넣어 주세요",
    "이 문서 보고서.md 보관해줘 부탁해요",
    "보고서.md 저장해 줘",
    "Please keep 보고서.md in my library",
])
async def test_keep_accepts_natural_korean_requests_that_name_the_file(client, new_session, sentence):
    ws, sess = await new_session()
    sid = uuid.UUID(sess["id"])
    f = Path(ws["root_path"]) / "보고서.md"
    f.write_text("# 보고서")
    await _mk_run(ws["id"], sid, sentence, at=datetime.now(UTC) - timedelta(minutes=1))
    run_id = await _mk_run(ws["id"], sid, "진행", status="running")
    r = await _keep(client, sid, run_id, f, sentence)
    assert r.status_code == 200, (sentence, r.text)
    ev = r.json()["artifact"]["keep_evidence"]
    assert ev["binding"] == "filename" and ev["binding_detail"] == "보고서.md"


async def test_keep_filename_must_be_the_full_name_and_unambiguous(client, new_session):
    """DUPLICATE_BASENAME: 같은 이름의 다른 경로 파일은 경로로 구분되지 않으면 거부한다. stem 이나 다른 이름의 일부로는 맞추지 않는다."""
    ws, sess = await new_session()
    sid = uuid.UUID(sess["id"])
    root = Path(ws["root_path"])
    (root / "a").mkdir()
    (root / "b").mkdir()
    a_file, b_file = root / "a" / "report.md", root / "b" / "report.md"
    a_file.write_text("# a")
    b_file.write_text("# b")
    await _mk_run(ws["id"], sid, "report.md를 내 보관함에 보관해줘\nb/report.md를 내 보관함에 보관해줘\n보고서 report 를 내 보관함에 보관해줘\nmy.report.md 를 내 보관함에 보관해줘",
                  at=datetime.now(UTC) - timedelta(minutes=1))
    run_id = await _mk_run(ws["id"], sid, "진행", status="running")
    h = _orch(issue_token(sid, run_id))
    # 이 세션에는 a/report.md 만 등록돼 있다.
    assert (await client.post("/api/agent/share", json={"path": str(a_file)}, headers=h)).status_code == 200

    r = await _keep(client, sid, run_id, b_file, "report.md를 내 보관함에 보관해줘")
    assert r.status_code == 422 and r.json()["code"] == "quote_ambiguous_file", r.text
    r = await _keep(client, sid, run_id, a_file, "b/report.md를 내 보관함에 보관해줘")  # 경로 끝이 다르다
    assert r.status_code == 422 and r.json()["code"] == "quote_not_bound", r.text
    for quote in ("보고서 report 를 내 보관함에 보관해줘", "my.report.md 를 내 보관함에 보관해줘"):
        r = await _keep(client, sid, run_id, b_file, quote)
        assert r.status_code == 422 and r.json()["code"] == "quote_not_bound", (quote, r.text)
    assert [a.area for a in await _artifact_rows(sid)] == ["agent"]

    r = await _keep(client, sid, run_id, b_file, "b/report.md를 내 보관함에 보관해줘")
    assert r.status_code == 200, r.text
    assert r.json()["artifact"]["keep_evidence"]["binding_detail"] == "b/report.md"

    # 등록되지 않은 같은 이름의 Run outbox 파일도 후보로 센다.
    ws2, sess2 = await new_session()
    sid2 = uuid.UUID(sess2["id"])
    first = await _mk_run(ws2["id"], sid2, "작업 1", at=datetime.now(UTC) - timedelta(minutes=3))
    second = await _mk_run(ws2["id"], sid2, "작업 2", at=datetime.now(UTC) - timedelta(minutes=2))
    one, two = _outbox_file(first, "summary.md"), _outbox_file(second, "summary.md")
    await _mk_run(ws2["id"], sid2, "summary.md를 내 보관함에 보관해줘", at=datetime.now(UTC) - timedelta(minutes=1))
    keeper = await _mk_run(ws2["id"], sid2, "진행", status="running")
    r = await _keep(client, sid2, keeper, two, "summary.md를 내 보관함에 보관해줘")
    assert r.status_code == 422 and r.json()["code"] == "quote_ambiguous_file", r.text
    assert one.exists()


def test_keep_quote_sentence_rules():
    from ttakkari.artifacts.keep_evidence import (
        has_keep_expression,
        is_byproduct,
        is_keep_request,
        is_negated,
        is_whole_sentence,
        names_file,
    )

    text = "이 파일은 보관하지 마. 내용만 확인해줘.\n보고서.md를 내 보관함에 보관해줘! 고마워요 정말 감사합니다"
    assert is_whole_sentence("이 파일은 보관하지 마", text)
    assert is_whole_sentence("보고서.md를 내 보관함에 보관해줘!", text)  # 문장부호는 무시
    assert is_whole_sentence("  보고서.md를   내 보관함에 보관해줘 ", text)  # 공백 정규화
    assert is_whole_sentence("고마워요 정말 감사합니다", text)  # 한 줄의 끝 문장
    assert not is_whole_sentence("보관", text)  # 단어 일부
    assert not is_whole_sentence("내 보관함에 보관해줘", text)  # 문장의 뒷부분만
    assert not is_whole_sentence("내용만 확인해줘", text)  # 8자 미만
    assert not is_whole_sentence("내용만 확인해줘. 보고서.md를 내 보관함에 보관해줘", text)  # 줄을 넘어 이어 붙임

    for yes in ("내 보관함에 넣어줘", "저장해 줘", "간직해줘", "남겨 줘", "Keep it", "please archive this"):
        assert has_keep_expression(yes), yes
        assert is_keep_request(yes), yes
    for no in ("The report was clear and useful.", "내용이 좋네요", "저장소를 확인해줘", "report.md 열어줘"):
        assert not has_keep_expression(no), no
        assert not is_keep_request(no), no
    # 보관 단어가 있어도 요청 형식이 아닌 문장
    for mention in ("report.md 보관해줘라는 문장을 설명해줘", "archive.md라는 파일 이름의 뜻을 알려줘", "report.md 보관 지시문을 요약해줘",
                    '"보관해줘"라고 쓰면 되나요?', "보관함에 넣는 방법 알려줘", "keep.md 내용 보여줘", "만들어줘"):
        assert not is_keep_request(mention), mention
    assert is_keep_request("만들어서 보관함에 넣어줘") and is_keep_request("정리하고 저장해줘")
    assert is_keep_request("보관해 주세요") and is_keep_request("보관해줄래?") and is_keep_request("내 보관함에 넣어둬")
    assert is_keep_request("Can you please store it") and not is_keep_request("Please explain how to save it")
    assert not is_negated('"하지 마"라고 쓴 문장을 보관해줘')  # 따옴표 안 텍스트는 판정에서 뺀다
    assert is_negated("보관하지 마") and is_negated("넣는 건 하지 말아줘") and is_negated("Don't save it") and is_negated("keep it without logs")
    assert not is_negated("보고서.md를 내 보관함에 보관해줘") and not is_negated("만들어서 보관함에 넣어줘")

    p = Path("/w/docs/Report Q3.md")
    assert names_file("report q3.md 보관해줘", p).path_token is None
    assert names_file("docs/report q3.md 를 보관함에", p).path_token == "docs/report q3.md"
    assert names_file("/w/docs/report q3.md 를 보관함에", p).path_token == "/w/docs/report q3.md"
    assert names_file("other/report q3.md 를 보관함에", p) is None  # 적힌 경로가 대상과 다르다
    assert names_file("Report Q3 보관해줘", p) is None  # 확장자 뺀 이름으로는 맞추지 않는다
    assert names_file("my.report q3.md 보관해줘", p) is None  # 다른 이름의 일부
    assert names_file("report q3.md.bak 보관해줘", p) is None
    assert names_file("보고서를 보관해줘", p) is None
    assert names_file("a.md 보관해줘", Path("/w/a.md")).token == "a.md"

    for byproduct in ("/o/run/debug-screenshot.png", "/o/run/x.log", "/o/run/browser/page-1.yml", "/o/run/.verification/n.md",
                      "/o/run/evidence/a.txt", "/o/run/tmp-data.json", "/o/run/capture1.mp4", "/o/run/a.worker-evidence.json"):
        assert is_byproduct(Path(byproduct)), byproduct
    for result in ("/o/run/result.md", "/o/run/report.pdf", "/o/run/data.csv", "/o/run/final-summary.json"):
        assert not is_byproduct(Path(result)), result


async def test_keep_with_real_user_message_quote_records_the_actual_message(client, auth, new_session):
    """실제 사용자 메시지의 문장(공백이 달라도)을 인용하고 파일명이 문장에 있으면 files 로 보관되고 근거는 그 메시지를 가리킨다."""
    ws, sess = await new_session()
    sid = uuid.UUID(sess["id"])
    f = Path(ws["root_path"]) / "report.md"
    f.write_text("# report")
    t0 = datetime.now(UTC)
    msg_id = await _mk_run(ws["id"], sid, "보고서 만들고\n그 report.md 파일 내 보관함에   보관해줘\n고마워", at=t0 - timedelta(minutes=5))
    run_id = await _mk_run(ws["id"], sid, "계속 진행해", status="running", at=t0)

    r = await _keep(client, sid, run_id, f, "그 report.md 파일 내 보관함에 보관해줘")
    assert r.status_code == 200, r.text
    a = r.json()["artifact"]
    assert a["area"] == "files" and a["origin"] == "agent" and a["keep_reason"] == "chat_request"
    ev = a["keep_evidence"]
    assert ev["message_id"] == str(msg_id) and ev["message_id"] != ev["run_id"]
    assert ev["run_id"] == str(run_id) and ev["session_id"] == str(sid)
    assert ev["message_source"] == "message" and ev["message_origin"] == "user"
    assert ev["quote"] == "그 report.md 파일 내 보관함에 보관해줘"
    assert ev["binding"] == "filename" and ev["binding_detail"] == "report.md"
    assert "고마워" in ev["user_message"]
    assert datetime.fromisoformat(ev["message_at"]) == t0 - timedelta(minutes=5)
    assert "kept_at" in ev
    listed = await client.get("/api/artifacts", params={"area": "files"}, headers=auth)
    assert [x["filename"] for x in listed.json()] == ["report.md"]


async def test_keep_accepts_desk_delegated_request_but_not_history_or_assistant_lines(client, new_session):
    """접수→기획 위임 Run 에서는 '[접수에서 넘긴 사용자 요청]' 본문만 사용자 발화다. 참고용 최근 대화·따까리 응답은 근거가 못 된다."""
    ws, sess = await new_session()
    sid = uuid.UUID(sess["id"])
    f = Path(ws["root_path"]) / "plan.md"
    f.write_text("# plan")
    prompt = ("[접수에서 넘긴 사용자 요청]\n이 plan.md 기획서 내 보관함에 넣어줘\n\n[최근 대화 — 참고만]\n"
              "사용자: plan.md 는 보관함에 저장해줘 예전 요청\n따까리: plan.md 알겠어요, 보관했어요")
    run_id = await _mk_run(ws["id"], sid, prompt, origin="desk", status="running")

    for quote in ("plan.md 는 보관함에 저장해줘 예전 요청", "plan.md 알겠어요, 보관했어요", "[최근 대화 — 참고만]"):
        r = await _keep(client, sid, run_id, f, quote)
        assert r.status_code == 422 and r.json()["code"] == "quote_not_found", (quote, r.text)
    assert await _artifact_rows(sid) == []

    r = await _keep(client, sid, run_id, f, "이 plan.md 기획서 내 보관함에 넣어줘")
    assert r.status_code == 200, r.text
    ev = r.json()["artifact"]["keep_evidence"]
    assert ev["message_source"] == "run_prompt" and ev["message_origin"] == "desk" and ev["binding"] == "filename"
    assert ev["message_id"] == str(run_id)
    assert "[접수에서 넘긴" not in ev["user_message"] and "예전 요청" not in ev["user_message"]


async def test_keep_rejects_planner_written_worker_prompt_and_other_session_messages(client, new_session):
    """기획이 작업 세션에 쓴 지시(orchestrator)·보고(report)는 사용자 발화가 아니다. 다른 세션 메시지나 이후에 생긴 메시지도 근거가 못 된다."""
    ws, sess = await new_session()
    _, other = await new_session()
    sid, other_sid = uuid.UUID(sess["id"]), uuid.UUID(other["id"])
    f = Path(ws["root_path"]) / "w.md"
    f.write_text("# w")
    t0 = datetime.now(UTC)
    await _mk_run(ws["id"], sid, "사용자가 보관을 요청했으니 w.md 를 내 보관함에 보관해줘", origin="orchestrator",
                  at=t0 - timedelta(minutes=3))
    await _mk_run(ws["id"], sid, "작업 완료. w.md 내 보관함에 보관해줘 라고 하셨습니다", origin="report", at=t0 - timedelta(minutes=2))
    await _mk_run(other["workspace_id"], other_sid, "다른 대화에서: w.md 내 보관함에 보관해줘", at=t0 - timedelta(minutes=1))
    run_id = await _mk_run(ws["id"], sid, "작업 진행", origin="orchestrator", status="running", at=t0)
    await _mk_run(ws["id"], sid, "나중에 한 말: w.md 내 보관함에 보관해줘", at=t0 + timedelta(minutes=1))

    for quote in ("사용자가 보관을 요청했으니 w.md 를 내 보관함에 보관해줘", "작업 완료. w.md 내 보관함에 보관해줘 라고 하셨습니다",
                  "다른 대화에서: w.md 내 보관함에 보관해줘", "나중에 한 말: w.md 내 보관함에 보관해줘"):
        r = await _keep(client, sid, run_id, f, quote)
        assert r.status_code == 422 and r.json()["code"] == "quote_not_found", (quote, r.text)
    assert await _artifact_rows(sid) == []


async def test_keep_twice_keeps_one_row_and_never_downgrades_user_kept_provenance(client, new_session):
    ws, sess = await new_session()
    sid = uuid.UUID(sess["id"])
    f = Path(ws["root_path"]) / "twice.md"
    f.write_text("# twice")
    await _mk_run(ws["id"], sid, "twice.md 보관함에 보관해줘")
    run_id = await _mk_run(ws["id"], sid, "진행", status="running")

    first = await _keep(client, sid, run_id, f, "twice.md 보관함에 보관해줘")
    second = await _keep(client, sid, run_id, f, "twice.md 보관함에 보관해줘!")
    assert first.status_code == second.status_code == 200
    assert first.json()["artifact"]["id"] == second.json()["artifact"]["id"]
    rows = await _artifact_rows(sid)
    assert len(rows) == 1
    assert rows[0].keep_evidence["quote"] == "twice.md 보관함에 보관해줘"  # 처음의 근거가 유지된다

    # 사용자가 직접 올린 보관 파일은 chat_request 로 바뀌지 않는다.
    g = Path(ws["root_path"]) / "mine.md"
    g.write_text("# mine")
    await _mk_run(ws["id"], sid, "mine.md 보관함에 보관해줘")
    run_id = await _mk_run(ws["id"], sid, "다음 진행", status="running")
    async with sessionmaker()() as db:
        db.add(Artifact(session_id=sid, workspace_id=uuid.UUID(ws["id"]), filename="mine.md", mime_type="text/markdown",
                        kind="markdown", source="upload", area=ArtifactArea.files, origin="user", keep_reason="direct_upload",
                        original_path=str(g.resolve()), stored_path=str(g.resolve()), storage_mode="reference",
                        size_bytes=6, sha256="0" * 64))
        await db.commit()
    r = await _keep(client, sid, run_id, g, "mine.md 보관함에 보관해줘")
    assert r.status_code == 200
    assert r.json()["artifact"]["keep_reason"] == "direct_upload" and r.json()["artifact"]["keep_evidence"] is None


async def test_keep_after_user_removed_requires_a_newer_request(client, auth, new_session):
    """사용자가 내 보관함에서 뺀 뒤에는 그 이전 발화로 다시 보관할 수 없다. 새 요청이 있으면 가능하다."""
    ws, sess = await new_session()
    sid = uuid.UUID(sess["id"])
    f = Path(ws["root_path"]) / "back.md"
    f.write_text("# back")
    t0 = datetime.now(UTC)
    await _mk_run(ws["id"], sid, "back.md 내 보관함에 보관해줘", at=t0 - timedelta(minutes=10))
    run1 = await _mk_run(ws["id"], sid, "진행", status="running", at=t0 - timedelta(minutes=9))
    kept = (await _keep(client, sid, run1, f, "back.md 내 보관함에 보관해줘")).json()["artifact"]
    assert kept["area"] == "files"

    out = await client.patch(f"/api/artifacts/{kept['id']}", json={"area": "agent"}, headers=auth)
    assert out.status_code == 200 and out.json()["area"] == "agent" and out.json()["keep_reason"] is None

    run2 = await _mk_run(ws["id"], sid, "다시 진행", status="running", at=datetime.now(UTC) + timedelta(seconds=1))
    r = await _keep(client, sid, run2, f, "back.md 내 보관함에 보관해줘")
    assert r.status_code == 409 and r.json()["code"] == "removed_after_request"
    assert (await _artifact_rows(sid))[0].area == "agent"

    await _mk_run(ws["id"], sid, "아 back.md 다시 내 보관함에 넣어줘", at=datetime.now(UTC) + timedelta(seconds=2))
    run3 = await _mk_run(ws["id"], sid, "또 진행", status="running", at=datetime.now(UTC) + timedelta(seconds=3))
    r = await _keep(client, sid, run3, f, "아 back.md 다시 내 보관함에 넣어줘")
    assert r.status_code == 200 and r.json()["artifact"]["area"] == "files"


async def test_keep_requires_resolvable_run_and_path_scope(client, new_session):
    ws, sess = await new_session()
    sid = uuid.UUID(sess["id"])
    f = Path(ws["root_path"]) / "x.md"
    f.write_text("# x")
    # Run 이 없는 세션의 토큰은 근거를 남길 Run 을 특정하지 못해 409.
    r = await _keep(client, sid, None, f, "x.md 를 보관함에 보관해줘")
    assert r.status_code == 409 and r.json()["code"] == "run_not_found"
    run_id = await _mk_run(ws["id"], sid, "x.md 를 보관함에 보관해줘", status="running")
    outside = Path(ws["root_path"]).parent / "outside-keep.md"
    outside.write_text("# o")
    r = await _keep(client, sid, run_id, outside, "x.md 를 보관함에 보관해줘")
    assert r.status_code == 403 and r.json()["code"] == "outside_root"
    # 빈 인용은 스키마에서 거부된다.
    assert (await _keep(client, sid, run_id, f, "")).status_code == 422


async def test_resolve_and_register_origin_is_unknown(client, auth, new_session):
    """비-outbox 링크 resolve 시 origin=unknown, area=agent, keep_reason=None.
    사용자 직접 등록(POST /api/artifacts) 시 origin=unknown, area=files, keep_reason=direct_register."""
    ws, _ = await new_session()
    ws_root = Path(ws["root_path"])
    p = ws_root / "sample_code.py"
    p.write_text("print('hello')")

    # 1. resolve_artifact_link (비-outbox)
    r_res = await client.post("/api/artifacts/resolve", json={"path": str(p), "workspace_id": ws["id"]}, headers=auth)
    assert r_res.status_code == 200
    res_art = r_res.json()
    assert res_art["origin"] == "unknown"  # 사용자가 작성했다는 보장이 없으므로 unknown
    assert res_art["area"] == "agent"
    assert res_art["keep_reason"] is None

    # 2. register_artifact (사용자 직접 등록)
    p2 = ws_root / "manual_reg.py"
    p2.write_text("print('reg')")
    r_reg = await client.post("/api/artifacts", json={"workspace_id": ws["id"], "rel_path": "manual_reg.py"}, headers=auth)
    assert r_reg.status_code == 201
    reg_art = r_reg.json()
    assert reg_art["origin"] == "unknown"  # 직접 등록도 보관 의도일 뿐 제작 증거가 아니므로 unknown
    assert reg_art["area"] == "files"
    assert reg_art["keep_reason"] == "direct_register"


async def test_migration_0068_runs_for_real_and_is_safe_to_repeat():
    """실제 Alembic 연산으로 0068 을 격리 스키마에서 upgrade→재실행→downgrade→재실행→upgrade 한다(롤백으로 정리).
    버전 표 밖에서 같은 migration 을 다시 돌려도 사용자가 보관한 파일을 agent 로 내리지 않는다."""
    from alembic.migration import MigrationContext
    from alembic.operations import Operations

    mod = _migration_0068()

    def _rows(c):
        return {r[0]: tuple(r[1:]) for r in c.execute(text(
            "SELECT filename, area, origin, keep_reason, category FROM artifacts ORDER BY filename"))}

    def _cols(c):
        return {r[0] for r in c.execute(text(
            "SELECT column_name FROM information_schema.columns WHERE table_schema = 'mig0068' AND table_name = 'artifacts'"))}

    def scenario(c):
        c.execute(text("CREATE SCHEMA mig0068"))
        c.execute(text("SET LOCAL search_path TO mig0068"))
        c.execute(text("CREATE TABLE artifacts (id uuid PRIMARY KEY DEFAULT gen_random_uuid(), filename varchar(500), "
                       "mime_type varchar(200), kind varchar(20), source varchar(30), area varchar(10) NOT NULL DEFAULT 'agent')"))
        for name, mime, kind, source, area in [
            ("up.txt", "text/plain", "code", "upload", "files"),
            ("reg.md", "text/markdown", "markdown", "user_registered", "files"),
            ("ai.mp4", "video/mp4", "video", "agent_output", "files"),
            ("bundle.zip", "application/zip", "other", "agent_output", "agent"),
        ]:
            c.execute(text("INSERT INTO artifacts (filename, mime_type, kind, source, area) VALUES (:a, :b, :c, :d, :e)"),
                      {"a": name, "b": mime, "c": kind, "d": source, "e": area})
        ctx = MigrationContext.configure(c)
        with Operations.context(ctx):
            mod.upgrade()
            first = _rows(c)
            assert first == {
                "up.txt": ("files", "user", "direct_upload", "document"),
                "reg.md": ("agent", "unknown", None, "document"),
                "ai.mp4": ("agent", "agent", None, "video"),
                "bundle.zip": ("agent", "agent", None, "archive"),
            }
            assert c.execute(text("SELECT count(*) FROM artifact_area_backup_0068")).scalar() == 4

            # 이후 사용자가 AI 파일을 보관한 상태에서 같은 migration 을 다시 실행한다.
            c.execute(text("UPDATE artifacts SET area = 'files', keep_reason = 'chat_request' WHERE filename = 'ai.mp4'"))
            mod.upgrade()
            again = _rows(c)
            assert again["ai.mp4"] == ("files", "agent", "chat_request", "video")
            assert {k: v for k, v in again.items() if k != "ai.mp4"} == {k: v for k, v in first.items() if k != "ai.mp4"}
            assert c.execute(text("SELECT count(*) FROM artifact_area_backup_0068")).scalar() == 4

            mod.downgrade()
            assert {"category", "origin", "keep_reason", "keep_evidence"}.isdisjoint(_cols(c))
            assert c.execute(text("SELECT to_regclass('artifact_area_backup_0068')")).scalar() is None
            assert {r[0]: r[1] for r in c.execute(text("SELECT filename, area FROM artifacts"))} == {
                "up.txt": "files", "reg.md": "files", "ai.mp4": "files", "bundle.zip": "agent"}
            mod.downgrade()  # 이미 내려간 상태에서 다시 내려도 오류가 없다.

            mod.upgrade()
            assert _rows(c) == first | {"ai.mp4": ("agent", "agent", None, "video")}

    async with sessionmaker()() as db:
        conn = await db.connection()
        try:
            await conn.run_sync(scenario)
        finally:
            await db.rollback()


async def test_category_sql_matches_python_detect_category():
    """SQL category 백필 CASE 식과 Python detect_category 가 대표 확장자 표에 대해 동일한 결과를 내는지 검증.
    대표 확장자: .ts .txt .log .json .html .csv .zip .tar.gz .mp4 .mov .mp3 .m4a .pdf .docx .pptx .png .unknown"""
    from ttakkari.artifacts.kinds import detect_category

    ext_cases = [
        ("file.ts", "text/plain", "code", "code"),
        ("file.txt", "text/plain", "code", "document"),
        ("file.log", "text/plain", "code", "code"),
        ("file.json", "application/json", "code", "code"),
        ("file.html", "text/html", "html", "code"),
        ("file.csv", "text/csv", "sheet", "spreadsheet"),
        ("file.zip", "application/zip", "other", "archive"),
        ("file.tar.gz", "application/gzip", "other", "archive"),
        ("file.mp4", "video/mp4", "video", "video"),
        ("file.mov", "video/quicktime", "video", "video"),
        ("file.mp3", "audio/mpeg", "audio", "audio"),
        ("file.m4a", "audio/mp4", "audio", "audio"),
        ("file.pdf", "application/pdf", "pdf", "document"),
        ("file.docx", "application/vnd.openxmlformats-officedocument.wordprocessingml.document", "docx", "document"),
        ("file.pptx", "application/vnd.openxmlformats-officedocument.presentationml.presentation", "pptx", "presentation"),
        ("file.png", "image/png", "image", "image"),
        ("file.unknown", "application/octet-stream", "other", "other"),
    ]

    mod = _migration_0068()
    sql = f"""
        SELECT (
            CASE
                WHEN {mod._DOCUMENT_COND} THEN 'document'
                WHEN {mod._SPREADSHEET_COND} THEN 'spreadsheet'
                WHEN {mod._PRESENTATION_COND} THEN 'presentation'
                WHEN {mod._IMAGE_COND} THEN 'image'
                WHEN {mod._VIDEO_COND} THEN 'video'
                WHEN {mod._AUDIO_COND} THEN 'audio'
                WHEN {mod._ARCHIVE_COND} THEN 'archive'
                WHEN {mod._CODE_COND} THEN 'code'
                ELSE 'other'
            END
        ) AS cat
        FROM (SELECT :filename AS filename, :mime_type AS mime_type, :kind AS kind) AS t
    """

    async with sessionmaker()() as db:
        for filename, mime, kind, expected in ext_cases:
            # 1. Python detect_category 검증
            py_cat = detect_category(Path(filename), mime, kind)
            assert py_cat == expected, f"Python detect_category mismatch for {filename}: got {py_cat}, expected {expected}"

            # 2. SQL CASE WHEN 검증
            sql_cat = (await db.execute(text(sql), {"filename": filename, "mime_type": mime, "kind": kind})).scalar()
            assert sql_cat == expected, f"SQL category mismatch for {filename}: got {sql_cat}, expected {expected}"
            assert py_cat == sql_cat, f"Python vs SQL mismatch for {filename}: {py_cat} != {sql_cat}"


async def test_files_tab_hides_rows_without_a_keep_reason_and_agent_list_filters_category(client, auth, new_session):
    """area=files 이어도 보관 사유가 없는 행(배포 전환 중 이전 세대가 승격한 행 등)은 내 보관함 목록에 나오지 않는다."""
    ws, sess = await new_session()
    sid = uuid.UUID(sess["id"])
    async with sessionmaker()() as db:
        for name, reason in (("legit.md", "user_button"), ("leaked.md", None)):
            db.add(Artifact(session_id=sid, workspace_id=uuid.UUID(ws["id"]), filename=name, mime_type="text/markdown",
                            kind="markdown", category="document", source="agent_output", area=ArtifactArea.files,
                            origin="agent", keep_reason=reason, original_path=f"/{name}", stored_path=f"/{name}",
                            storage_mode="reference", size_bytes=1, sha256="0" * 64))
        db.add(Artifact(session_id=sid, workspace_id=uuid.UUID(ws["id"]), filename="clip.mp4", mime_type="video/mp4",
                        kind="video", category="video", source="agent_output", area=ArtifactArea.agent, origin="agent",
                        original_path="/clip.mp4", stored_path="/clip.mp4", storage_mode="reference", size_bytes=1, sha256="0" * 64))
        await db.commit()
    assert await _areas(client, auth, area="files") == {"legit.md": "files"}
    h = _orch(issue_token(sid))
    agent_files = (await client.get("/api/agent/artifacts", params={"area": "files"}, headers=h)).json()["artifacts"]
    assert [a["filename"] for a in agent_files] == ["legit.md"]
    videos = (await client.get("/api/agent/artifacts", params={"category": "video"}, headers=h)).json()["artifacts"]
    assert [a["filename"] for a in videos] == ["clip.mp4"]
