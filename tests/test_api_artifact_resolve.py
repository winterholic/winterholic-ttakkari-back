from __future__ import annotations

from pathlib import Path
from urllib.parse import quote

from helpers import run_to_end

from ttakkari.config import get_settings


async def _resolve(client, auth, path, **kw):
    return await client.post("/api/artifacts/resolve", json={"path": path, **kw}, headers=auth)


async def _outbox_artifact(client, auth, new_session):
    ws, sess = await new_session()
    run = await run_to_end(client, auth, sess["id"], "make files")
    assert run["status"] == "succeeded", run
    arts = {a["filename"]: a for a in (await client.get("/api/artifacts", params={"run_id": run["id"]}, headers=auth)).json()}
    path = get_settings().outbox_root / run["id"] / "report.md"
    return ws, sess, arts["report.md"], path


async def test_registered_outbox_path_opens_existing_artifact_in_every_link_form(client, auth, new_session):
    _, sess, art, path = await _outbox_artifact(client, auth, new_session)
    forms = [str(path), f"{path}:12", f"{path}:12:3", f"{path}#L5", f"<{path}>", f"file://{quote(str(path))}"]
    for form in forms:
        r = await _resolve(client, auth, form, session_id=sess["id"])
        assert r.status_code == 200, (form, r.text)
        assert r.json()["id"] == art["id"], form
    r = await _resolve(client, auth, "report.md", session_id=sess["id"])  # 이름만 적은 outbox 파일
    assert r.status_code == 200 and r.json()["id"] == art["id"]
    c = await client.get(f"/api/artifacts/{art['id']}/content", headers=auth)  # 해석된 결과물은 뷰어가 그대로 읽는다
    assert c.status_code == 200 and c.text == "# report\n"


async def test_unregistered_workspace_file_is_registered_once_without_polluting_files_tab(client, auth, new_session):
    ws, _ = await new_session()
    root = Path(ws["root_path"])
    (root / "src").mkdir()
    (root / "src" / "한글 파일.py").write_text("print(1)\n")
    abs_path = str((root / "src" / "한글 파일.py").resolve())
    r = await _resolve(client, auth, f"{abs_path}:3", workspace_id=ws["id"])
    assert r.status_code == 200, r.text
    a = r.json()
    assert a["kind"] == "code" and a["source"] == "user_registered" and a["area"] == "agent"
    again = await _resolve(client, auth, abs_path)
    assert again.json()["id"] == a["id"]
    rel = await _resolve(client, auth, "./src/한글 파일.py:9", workspace_id=ws["id"])
    assert rel.json()["id"] == a["id"]
    c = await client.get(f"/api/artifacts/{a['id']}/content", headers=auth)
    assert c.status_code == 200 and c.text == "print(1)\n"


async def test_relative_path_needs_workspace_and_cannot_climb(client, auth, new_session):
    ws, _ = await new_session()
    assert (await _resolve(client, auth, "a.md")).status_code == 400
    r = await _resolve(client, auth, "../escape.md", workspace_id=ws["id"])
    assert r.status_code == 400 and r.json()["code"] == "bad_path"
    assert (await _resolve(client, auth, "nope.md", workspace_id=ws["id"])).status_code == 404


async def test_tilde_path_expands_home(client, auth, new_session, monkeypatch):
    ws, _ = await new_session()
    (Path(ws["root_path"]) / "note.md").write_text("# n\n")
    monkeypatch.setenv("HOME", str(Path(ws["root_path"]).resolve()))
    r = await _resolve(client, auth, "~/note.md")
    assert r.status_code == 200 and r.json()["filename"] == "note.md"


async def test_paths_outside_allowed_dirs_are_refused_and_not_registered(client, auth, new_session, outside_dir):
    ws, _ = await new_session()
    secret = outside_dir / "secret-notes.txt"
    secret.write_text("x")
    before = len((await client.get("/api/artifacts", params={"limit": 200}, headers=auth)).json())
    for p in (str(secret), f"file://{secret}", "/etc/passwd", "/etc/hosts:3", str(outside_dir / "missing.txt"),
              f"{ws['root_path']}/../{outside_dir.name}/x.txt"):
        r = await _resolve(client, auth, p, workspace_id=ws["id"])
        assert r.status_code == 403 and r.json()["code"] == "outside_root", (p, r.status_code, r.text)
    after = len((await client.get("/api/artifacts", params={"limit": 200}, headers=auth)).json())
    assert after == before


async def test_symlink_leaving_workspace_is_refused(client, auth, new_session, outside_dir):
    ws, _ = await new_session()
    target = outside_dir / "target.txt"
    target.write_text("x")
    link = Path(ws["root_path"]) / "link.txt"
    link.symlink_to(target)
    for p in (str(link), "link.txt"):
        r = await _resolve(client, auth, p, workspace_id=ws["id"])
        assert r.status_code == 403 and r.json()["code"] == "outside_root", (p, r.text)


async def test_sensitive_file_and_directory_are_not_opened(client, auth, new_session):
    ws, _ = await new_session()
    root = Path(ws["root_path"])
    (root / ".env").write_text("TOKEN=1\n")
    (root / "sub").mkdir()
    r = await _resolve(client, auth, str(root / ".env"))
    assert r.status_code == 403 and r.json()["code"] == "sensitive"
    r = await _resolve(client, auth, "sub", workspace_id=ws["id"])
    assert r.status_code == 400 and r.json()["code"] == "not_a_file"


async def test_resolve_requires_auth(client):
    assert (await client.post("/api/artifacts/resolve", json={"path": "/etc/passwd"})).status_code == 401
