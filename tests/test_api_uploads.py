from __future__ import annotations

from pathlib import Path

from conftest import agent_calls
from helpers import wait_run

from ttakkari.config import get_settings


async def _upload(client, auth, ws_id, name="memo.md", data=b"# memo\n", session_id=None):
    form = {"workspace_id": ws_id}
    if session_id:
        form["session_id"] = session_id
    return await client.post("/api/uploads", files={"file": (name, data, "text/markdown")}, data=form, headers=auth)


async def test_upload_registers_in_place_and_is_viewable(client, auth, new_session):
    ws, sess = await new_session()
    r = await _upload(client, auth, ws["id"], session_id=sess["id"])
    assert r.status_code == 201, r.text
    a = r.json()
    assert a["source"] == "upload" and a["storage_mode"] == "reference" and a["kind"] == "markdown"
    c = await client.get(f"/api/artifacts/{a['id']}/content", headers=auth)
    assert c.status_code == 200 and c.text == "# memo\n"


async def test_upload_path_reaches_agent_prompt_and_is_guard_writable(client, auth, new_session):
    ws, sess = await new_session()
    a = (await _upload(client, auth, ws["id"])).json()
    run = (await client.post(f"/api/sessions/{sess['id']}/runs", json={"prompt": "읽어줘", "context_artifact_ids": [a["id"]]}, headers=auth)).json()
    await wait_run(client, auth, run["id"])
    call = agent_calls()[-1]
    prompt = " ".join(call["argv"])
    assert "memo.md" in prompt and str(get_settings().uploads_root) in prompt


async def test_upload_too_large_is_413_and_leaves_nothing(client, auth, new_session, monkeypatch):
    ws, _ = await new_session()
    monkeypatch.setattr(get_settings(), "upload_max_bytes", 10)
    before = set(get_settings().uploads_root.iterdir())
    r = await _upload(client, auth, ws["id"], data=b"x" * 2_000_000)
    assert r.status_code == 413 and r.json()["code"] == "too_large"
    assert set(get_settings().uploads_root.iterdir()) == before


async def test_upload_hidden_name_and_traversal_name_are_neutralized(client, auth, new_session):
    ws, _ = await new_session()
    a = (await _upload(client, auth, ws["id"], name="../../.env")).json()
    assert "/" not in a["filename"] and not a["filename"].startswith(".")
    stored = Path(get_settings().uploads_root)
    assert all(p.parent.parent == stored.resolve() or p.parent == stored for p in stored.rglob("*") if p.is_file())


async def test_delete_upload_removes_file(client, auth, new_session):
    ws, _ = await new_session()
    a = (await _upload(client, auth, ws["id"])).json()
    dirs_before = {p for p in get_settings().uploads_root.iterdir()}
    assert (await client.delete(f"/api/artifacts/{a['id']}", headers=auth)).status_code == 204
    assert len(dirs_before - set(get_settings().uploads_root.iterdir())) == 1


async def test_upload_requires_auth_and_workspace(client, auth):
    assert (await client.post("/api/uploads", files={"file": ("a.txt", b"a")}, data={"workspace_id": "00000000-0000-0000-0000-000000000000"})).status_code == 401
    r = await client.post("/api/uploads", files={"file": ("a.txt", b"a")}, data={"workspace_id": "00000000-0000-0000-0000-000000000000"}, headers=auth)
    assert r.status_code == 404
