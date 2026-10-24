"""보관함 분류: 파일(files)과 에이전트 파일(agent)."""
from __future__ import annotations

import asyncio
import importlib.util
import uuid
from pathlib import Path

from helpers import run_to_end, wait_run
from sqlalchemy import select, text, update
from test_orchestrator import _orch, _session

from ttakkari.config import get_settings
from ttakkari.db import sessionmaker
from ttakkari.models import Artifact, ArtifactArea, ChatSession, default_artifact_area
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
    await run_to_end(client, auth, sess["id"], "MODIFY")
    assert await _areas(client, auth, session_id=sess["id"]) == {
        "report.md": "agent", "README.md": "agent", "new_file.txt": "agent"}
    assert await _areas(client, auth, session_id=sess["id"], area="files") == {}

    # 에이전트가 share_file 로 넘겨도 내 보관함(files)이 아닌 AI 작업 파일(agent)에 남는다.
    run_id = (await client.get(f"/api/sessions/{sess['id']}/runs", headers=auth)).json()[0]["id"]
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
    r_keep = await client.post("/api/agent/keep", json={"path": str(outbox / "report.md"), "instruction": "만들어서 보관함에 넣어줘"}, headers=h)
    assert r_keep.status_code == 200
    kept = r_keep.json()["artifact"]
    assert kept["id"] == before_id
    assert kept["area"] == "files"
    assert kept["origin"] == "agent"  # AI 파일 출처 유지!
    assert kept["keep_reason"] == "chat_request"
    assert kept["keep_evidence"]["instruction"] == "만들어서 보관함에 넣어줘"
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
        run = Run(session_id=sid, workspace_id=uuid.UUID(ws["id"]), prompt="keep file", engine="claude", status="running")
        db.add(run)
        await db.commit()
        run_id = run.id

    outbox = get_settings().outbox_root / str(run_id)
    outbox.mkdir(parents=True)
    target = outbox / "user_fav.md"
    target.write_text("# important user data\n")
    h = _orch(issue_token(sid, run_id))

    # 1. 사용자의 요청으로 보관함에 보관됨
    r_keep = await client.post("/api/agent/keep", json={"path": str(target), "instruction": "보관함에 보관해줘"}, headers=h)
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
        # 2. AI 작업 파일인데 레거시로 files 에 있던 것 (agent 로 이동, origin=agent, keep_reason=None)
        art("agent_output", "report.md", "files", mime="text/markdown", kind="markdown"),
        art("agent_output", "slide.pptx", "files", mime="application/vnd.ms-powerpoint", kind="pptx"),
        art("agent_output", "video.mp4", "files", mime="video/mp4", kind="video"),
        art("agent_output", "audio.mp3", "files", mime="audio/mpeg", kind="audio"),
        art("agent_output", "archive.zip", "files", mime="application/zip", kind="other"),
        art("agent_output", "script.py", "files", mime="text/x-python", kind="code"),
        art("agent_output", "notes.txt", "files", mime="text/plain", kind="code"),
        # 3. 원래 agent 였던 것 (agent 유지)
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
        run = Run(session_id=sid, workspace_id=ws_id, prompt="generate files", engine="claude", status="running")
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
    keep_resp = await client.post("/api/agent/keep", json={"path": str(ai_file), "instruction": "엑셀 만들어서 보관함에 넣어줘"}, headers=h)
    assert keep_resp.status_code == 200
    kept_art = keep_resp.json()["artifact"]
    assert kept_art["area"] == "files"
    assert kept_art["origin"] == "agent"  # 출처는 AI 유지!
    assert kept_art["keep_reason"] == "chat_request"
    assert kept_art["keep_evidence"]["instruction"] == "엑셀 만들어서 보관함에 넣어줘"

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
        await client.post("/api/agent/keep", json={"path": str(ai_file), "instruction": "다시 보관"}, headers=h)
    after_count = len((await client.get("/api/artifacts", params={"session_id": str(sid)}, headers=auth)).json())
    assert before_count == after_count
