"""보관함 분류: 파일(files)과 에이전트 파일(agent)."""
from __future__ import annotations

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


async def test_direct_chat_outbox_is_agent_until_shared(client, auth, new_session, make_git_dir):
    _ws, sess = await new_session(make_git_dir())
    await run_to_end(client, auth, sess["id"], "MODIFY")
    assert await _areas(client, auth, session_id=sess["id"]) == {
        "report.md": "agent", "README.md": "agent", "new_file.txt": "agent"}
    assert await _areas(client, auth, session_id=sess["id"], area="files") == {}

    # 에이전트가 share_file 로 넘긴 결과물만 파일이 된다. 자동 수집된 행을 옮기므로 중복 행이 생기지 않는다.
    run_id = (await client.get(f"/api/sessions/{sess['id']}/runs", headers=auth)).json()[0]["id"]
    outbox = get_settings().outbox_root / run_id
    h = _orch(issue_token(sess["id"]))
    before = (await client.get("/api/artifacts", params={"session_id": sess["id"]}, headers=auth)).json()
    before_id = next(a["id"] for a in before if a["filename"] == "report.md")
    r = await client.post("/api/agent/share", json={"path": str(outbox / "report.md")}, headers=h)
    assert r.status_code == 200 and r.json()["artifact"]["area"] == "files"
    assert r.json()["artifact"]["id"] == before_id
    assert await _areas(client, auth, session_id=sess["id"]) == {
        "report.md": "files", "README.md": "agent", "new_file.txt": "agent"}
    # 다시 공유해도 같은 행이다.
    r2 = await client.post("/api/agent/share", json={"path": str(outbox / "report.md")}, headers=h)
    assert r2.json()["artifact"]["id"] == before_id


async def test_worker_outbox_is_agent_until_shared_by_orchestrator(client, auth, make_dir):
    orch = await _session(client, auth, make_dir, kind="orchestrator")
    h = _orch(issue_token(orch["id"]))
    wid = (await client.post("/api/orchestrator/workers", json={"title": "w", "prompt": "write report", "engine": "claude"},
                             headers=h)).json()["worker_id"]
    wrun = (await client.get(f"/api/sessions/{wid}/runs", headers=auth)).json()[0]
    await wait_run(client, auth, wrun["id"])
    assert await _areas(client, auth, session_id=wid) == {"report.md": "agent"}
    assert await _areas(client, auth, session_id=wid, area="files") == {}

    # 오케스트레이터가 사용자에게 넘기려고 share_file 로 올리면 파일이 된다.
    outbox = get_settings().outbox_root / wrun["id"]
    r = await client.post("/api/agent/share", json={"path": str(outbox / "report.md")}, headers=h)
    assert r.status_code == 200 and r.json()["artifact"]["area"] == "files"
    assert r.json()["artifact"]["session_id"] == orch["id"]
    # 작업 세션 쪽 원본은 에이전트 파일로 남는다.
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

    def art(source, name, original="/x", area="files", deleted=False):
        return Artifact(workspace_id=None, session_id=None, filename=name, mime_type="text/plain", kind="other",
                        source=source, area=area, original_path=original, storage_mode="copy", stored_path="/x",
                        size_bytes=1, sha256="0" * 64, deleted_at=utcnow() if deleted else None)

    move = [  # files → agent 로 옮겨야 한다
        art("agent_output", "m52-watch.log"),
        art("agent_output", "m52-a1b2-worker-evidence.json"),
        art("agent_output", "m52-pms-final-snapshot.json"),
        art("agent_output", "m52-resource-cleanup.json"),
        art("agent_output", "m52-membership-bindings.json"),
        art("agent_output", "final-m52-main-ci.json"),
        art("agent_output", "check-m52-mutations.py"),
        art("agent_output", "m52-page-2026-10-19.png",
            str(outbox / "run1" / "browser" / "page-2026-10-19.png")),
        art("agent_modified", "m52-build.log"),
        art("agent_output", "m52-replay-routing-snapshot.py"),
        art("agent_output", "m52-cleanup-red.xml"),
        art("agent_output", "m52-keychain-selection.txt",
            str(outbox / "run1" / "evidence" / "m52-keychain-selection.txt")),
    ]
    stay = [  # 그대로 files (또는 agent)
        art("agent_output", "m52-report.md"),
        art("agent_output", "m52-saas-1280-compare.png"),
        art("agent_output", "m52-aip-wbs-evidence-20261015.zip"),  # 압축은 결과물일 수 있어 둔다
        art("upload", "m52-up.log"),
        art("user_registered", "m52-reg.log"),
        art("agent_output", "m52-old.log", area="agent"),
        art("agent_output", "m52-gone.log", deleted=True),
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
        got = {r.filename: r.area for r in
               (await db.execute(select(Artifact).where(Artifact.id.in_(ids)))).scalars()}
        assert [got[r.filename] for r in move] == ["agent"] * len(move)
        assert [got[r.filename] for r in stay] == ["files", "files", "files", "files", "files", "agent", "files"]
        # 백업 표에 영향 행의 원래 분류가 남고 downgrade 로 되돌릴 수 있다.
        n_backup = (await db.execute(text("SELECT COUNT(*) FROM artifact_area_backup_0052"))).scalar()
        assert n_backup == len(move)
        down: list[str] = []
        mod.op = FakeOp(down)
        mod.downgrade()
        for s in down:
            await db.execute(text(s))
        await db.commit()
        back = {r.filename: r.area for r in
                (await db.execute(select(Artifact).where(Artifact.id.in_(ids)))).scalars()}
        assert back["m52-watch.log"] == "files" and back["m52-a1b2-worker-evidence.json"] == "files"
        assert back["m52-old.log"] == "agent"  # 백업에 없던 행은 그대로
        await db.execute(text("DROP TABLE IF EXISTS artifact_area_backup_0052"))
        for r in move + stay:
            await db.delete(r)
        await db.commit()
