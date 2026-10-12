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


async def test_direct_chat_outbox_is_files_but_modified_files_are_agent(client, auth, new_session, make_git_dir):
    _ws, sess = await new_session(make_git_dir())
    await run_to_end(client, auth, sess["id"], "MODIFY")
    assert await _areas(client, auth, session_id=sess["id"]) == {
        "report.md": "files", "README.md": "agent", "new_file.txt": "agent"}
    assert set(await _areas(client, auth, session_id=sess["id"], area="files")) == {"report.md"}
    assert set(await _areas(client, auth, session_id=sess["id"], area="agent")) == {"README.md", "new_file.txt"}


async def test_worker_outbox_is_agent_until_shared_by_orchestrator(client, auth, make_dir):
    orch = await _session(client, auth, make_dir, kind="orchestrator")
    h = _orch(issue_token(orch["id"]))
    wid = (await client.post("/api/orchestrator/workers", json={"title": "w", "prompt": "write report"},
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
