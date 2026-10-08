from __future__ import annotations

import asyncio
import time
import uuid
from datetime import UTC, datetime, timedelta
from urllib.parse import quote

import jwt
import pytest
from conftest import ALLOWED
from helpers import run_events, run_to_end

from ttakkari.artifacts.preview import soffice_path
from ttakkari.config import get_settings
from ttakkari.security.auth import issue_download_token


async def _run_artifacts(client, auth, new_session, prompt="make files", **kw):
    ws, sess = await new_session(**kw)
    run = await run_to_end(client, auth, sess["id"], prompt)
    assert run["status"] == "succeeded", run
    r = await client.get("/api/artifacts", params={"run_id": run["id"]}, headers=auth)
    assert r.status_code == 200
    return ws, sess, run, {a["filename"]: a for a in r.json()}


async def _wait_preview(client, auth, art_id, timeout=15.0):
    """변환 상태가 pending/processing 을 벗어나길 기다린다.

    """
    deadline = time.monotonic() + timeout
    while True:
        a = (await client.get(f"/api/artifacts/{art_id}", headers=auth)).json()
        if a["preview_status"] not in ("pending", "processing"):
            return a
        assert time.monotonic() < deadline, f"preview_status 가 안 바뀜: {a['preview_status']}"
        await asyncio.sleep(0.1)


async def test_content_inline_with_csp_sandbox(client, auth, new_session):
    _, _, _run, arts = await _run_artifacts(client, auth, new_session)
    a = arts["report.md"]
    assert a["kind"] == "markdown" and a["source"] == "agent_output" and a["downloadable"] is True
    assert a["storage_mode"] == "copy" and a["export_policy"] == "allow"
    assert "stored_path" not in a and "original_path" not in a  # 서버 내부 경로는 노출 금지
    r = await client.get(f"/api/artifacts/{a['id']}/content", headers=auth)
    assert r.status_code == 200 and r.text == "# report\n"
    assert r.headers["content-security-policy"].startswith("sandbox;")
    assert "default-src 'none'" in r.headers["content-security-policy"]
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["content-disposition"].startswith("inline;")
    assert r.headers["cache-control"] == "private, no-store"


async def test_content_requires_auth(client, auth, new_session):
    _, _, _, arts = await _run_artifacts(client, auth, new_session)
    assert (await client.get(f"/api/artifacts/{arts['report.md']['id']}/content")).status_code == 401


async def test_content_range_206(client, auth, new_session):
    _, _, _, arts = await _run_artifacts(client, auth, new_session)
    r = await client.get(f"/api/artifacts/{arts['report.md']['id']}/content", headers={**auth, "Range": "bytes=0-3"})
    assert r.status_code == 206
    assert r.text == "# re" and r.headers["content-range"] == "bytes 0-3/9"
    assert r.headers["content-security-policy"].startswith("sandbox;")


async def test_download_link_then_dl_attachment(client, auth, new_session):
    _, _, _, arts = await _run_artifacts(client, auth, new_session)
    a = arts["report.md"]
    r = await client.post(f"/api/artifacts/{a['id']}/download-link", headers=auth)
    assert r.status_code == 200
    body = r.json()
    assert body["url"].startswith("/api/dl/")
    exp = datetime.fromisoformat(body["expires_at"])
    assert timedelta(0) < exp - datetime.now(UTC) <= timedelta(seconds=get_settings().download_token_ttl_seconds + 2)
    # 링크는 Authorization 헤더 없이도 열린다.
    dl = await client.get(body["url"])
    assert dl.status_code == 200 and dl.text == "# report\n"
    assert dl.headers["content-disposition"] == "attachment; filename*=UTF-8''report.md"
    assert dl.headers["content-security-policy"] == "sandbox"
    assert dl.headers["x-content-type-options"] == "nosniff"


async def test_download_link_requires_auth(client, auth, new_session):
    _, _, _, arts = await _run_artifacts(client, auth, new_session)
    assert (await client.post(f"/api/artifacts/{arts['report.md']['id']}/download-link")).status_code == 401


def _token(art_id: str, *, secret=None, aud="download", exp=None, iss="ttakkari", var="original") -> str:
    payload = {"iss": iss, "aud": aud, "sub": art_id, "var": var, "jti": uuid.uuid4().hex,
               "exp": exp or datetime.now(UTC) + timedelta(minutes=5)}
    return jwt.encode(payload, secret or get_settings().jwt_secret, algorithm="HS256")


async def test_dl_rejects_expired_forged_and_wrong_audience(client, auth, new_session):
    _, _, _, arts = await _run_artifacts(client, auth, new_session)
    aid = arts["report.md"]["id"]
    assert (await client.get(f"/api/dl/{_token(aid)}")).status_code == 200  # 대조군: 올바른 토큰은 통과

    expired = _token(aid, exp=datetime.now(UTC) - timedelta(seconds=5))
    forged = _token(aid, secret="x" * 40)
    api_aud = _token(aid, aud="api")
    bad_iss = _token(aid, iss="evil")
    for tok in (expired, forged, api_aud, bad_iss, "garbage", "a.b.c"):
        r = await client.get(f"/api/dl/{tok}")
        assert r.status_code == 403, tok
        assert r.json()["code"] == "forbidden"


async def test_dl_rejects_alg_none_token(client, auth, new_session):
    _, _, _, arts = await _run_artifacts(client, auth, new_session)
    tok = jwt.encode({"iss": "ttakkari", "aud": "download", "sub": arts["report.md"]["id"], "var": "original",
                      "exp": datetime.now(UTC) + timedelta(minutes=5)}, key=None, algorithm="none")
    assert (await client.get(f"/api/dl/{tok}")).status_code == 403


async def test_dl_api_access_token_cannot_download(client, auth, new_session):
    from ttakkari.security.auth import issue_access_token
    tok, _ = issue_access_token()
    assert (await client.get(f"/api/dl/{tok}")).status_code == 403


async def test_dl_denied_attempts_are_audited(client, auth, new_session):
    from sqlalchemy import select

    from ttakkari.db import sessionmaker
    from ttakkari.models import AuditLog
    await client.get("/api/dl/garbage")
    async with sessionmaker()() as db:
        rows = list(await db.scalars(select(AuditLog).where(AuditLog.action == "artifact.download")))
    assert [(r.outcome, r.detail["reason"]) for r in rows] == [("denied", "DecodeError")]


async def test_dl_unknown_artifact_404(client):
    assert (await client.get(f"/api/dl/{_token(str(uuid.uuid4()))}")).status_code == 404


async def test_sensitive_output_is_blocked_everywhere(client, auth, new_session):
    _, _, _, arts = await _run_artifacts(client, auth, new_session, "SENSITIVE")
    env = arts[".env"]
    assert env["export_policy"] == "sensitive" and env["downloadable"] is False
    assert arts["report.md"]["export_policy"] == "allow"

    r = await client.get(f"/api/artifacts/{env['id']}/content", headers=auth)
    assert r.status_code == 403 and r.json()["code"] == "sensitive"
    r = await client.post(f"/api/artifacts/{env['id']}/download-link", headers=auth)
    assert r.status_code == 403 and r.json()["code"] == "sensitive"
    # 정책 확인은 링크 소비 시점에도 다시 한다: 직접 서명한 유효 토큰이어도 막힌다.
    tok, _ = issue_download_token(uuid.UUID(env["id"]), "original")
    assert (await client.get(f"/api/dl/{tok}")).status_code == 403


async def test_docx_preview_leaves_pending(client, auth, new_session):
    _, _sess, run, arts = await _run_artifacts(client, auth, new_session, "DOCX")
    docx = arts["a.docx"]
    assert docx["kind"] == "docx"
    # run 이벤트에 실린 스냅샷은 변환 전이라 pending
    created = [e["payload"]["artifact"] for e in await run_events(client, auth, run["id"])
               if e["type"] == "artifact.created" and e["payload"]["artifact"]["filename"] == "a.docx"]
    assert created[0]["preview_status"] == "pending"

    final = await _wait_preview(client, auth, docx["id"])
    if soffice_path() is None:
        assert final["preview_status"] == "unavailable" and "LibreOffice" in final["preview_error"]
    else:
        assert final["preview_status"] in ("ready", "failed")
    assert arts["report.md"]["preview_status"] == "not_required"


# 회귀: Run 이 만든 docx 는 commit 이후에 변환 큐에 들어가야 한다(commit 지연을 주입해 결정적으로 재현).
async def test_docx_from_run_gets_preview_status_even_if_commit_is_slow(client, auth, new_session, monkeypatch):
    from sqlalchemy.ext.asyncio import AsyncSession

    from ttakkari.models import Artifact
    real_commit = AsyncSession.commit

    async def slow_commit(self):
        if any(isinstance(o, Artifact) for o in self.identity_map.values()):
            await asyncio.sleep(0.5)
        return await real_commit(self)

    monkeypatch.setattr(AsyncSession, "commit", slow_commit)
    _ws, sess = await new_session()
    run = await run_to_end(client, auth, sess["id"], "DOCX")
    arts = {a["filename"]: a for a in (await client.get("/api/artifacts", params={"run_id": run["id"]}, headers=auth)).json()}
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        a = (await client.get(f"/api/artifacts/{arts['a.docx']['id']}", headers=auth)).json()
        if a["preview_status"] != "pending":
            break
        await asyncio.sleep(0.1)
    assert a["preview_status"] != "pending"


async def test_docx_preview_variant_404_when_unavailable(client, auth, new_session):
    _, _, _, arts = await _run_artifacts(client, auth, new_session, "DOCX")
    final = await _wait_preview(client, auth, arts["a.docx"]["id"])
    if final["preview_status"] == "ready":
        pytest.skip("LibreOffice 변환 성공 환경")
    r = await client.get(f"/api/artifacts/{final['id']}/content", params={"variant": "preview"}, headers=auth)
    assert r.status_code == 404 and r.json()["code"] == "no_preview"
    # 원본은 변환 여부와 무관하게 열린다.
    r = await client.get(f"/api/artifacts/{final['id']}/content", headers=auth)
    assert r.status_code == 200 and r.content == b"not really a docx"


async def test_preview_retry(client, auth, new_session):
    _, _, _, arts = await _run_artifacts(client, auth, new_session, "DOCX")
    final = await _wait_preview(client, auth, arts["a.docx"]["id"])
    if final["preview_status"] == "ready":
        pytest.skip("LibreOffice 변환 성공 환경")
    r = await client.post(f"/api/artifacts/{final['id']}/preview/retry", headers=auth)
    assert r.status_code == 200 and r.json()["preview_status"] == "pending"
    again = await _wait_preview(client, auth, final["id"])
    assert again["preview_status"] == final["preview_status"]
    # 서버 미리보기가 필요 없는 형식은 400
    r = await client.post(f"/api/artifacts/{arts['report.md']['id']}/preview/retry", headers=auth)
    assert r.status_code == 400


# ---- 수동 등록

async def _ws(client, auth, new_session, root=None, **kw):
    ws, sess = await new_session(root)
    return ws, sess


async def test_register_workspace_file(client, auth, new_session, make_dir):
    root = make_dir()
    (root / "docs").mkdir()
    (root / "docs" / "한글 문서.md").write_text("# 안녕\n")
    ws, sess = await new_session(root)
    r = await client.post("/api/artifacts", headers=auth, json={
        "workspace_id": ws["id"], "rel_path": "docs/한글 문서.md", "session_id": sess["id"]})
    assert r.status_code == 201
    a = r.json()
    assert a["source"] == "user_registered" and a["rel_path"] == "docs/한글 문서.md" and a["kind"] == "markdown"
    assert a["session_id"] == sess["id"] and a["run_id"] is None and a["size_bytes"] == len("# 안녕\n".encode())
    # 관리 영역으로 복사됐으므로 원본을 지워도 열린다.
    (root / "docs" / "한글 문서.md").unlink()
    assert (await client.get(f"/api/artifacts/{a['id']}/content", headers=auth)).text == "# 안녕\n"
    link = (await client.post(f"/api/artifacts/{a['id']}/download-link", headers=auth)).json()["url"]
    dl = await client.get(link)
    assert dl.headers["content-disposition"] == f"attachment; filename*=UTF-8''{quote('한글 문서.md')}"


@pytest.mark.parametrize("bad", ["..", "../x.txt", "docs/../../x.txt", "/etc/passwd"])
async def test_register_rejects_traversal(client, auth, new_session, make_dir, bad):
    ws, _ = await new_session(make_dir())
    r = await client.post("/api/artifacts", headers=auth, json={"workspace_id": ws["id"], "rel_path": bad})
    assert r.status_code == 400 and r.json()["code"] == "bad_path"
    assert (await client.get("/api/artifacts", headers=auth)).json() == []


async def test_register_nul_in_path_is_400_not_500(client_500, auth, new_session, make_dir):
    ws, _ = await new_session(make_dir())
    r = await client_500.post("/api/artifacts", headers=auth, json={"workspace_id": ws["id"], "rel_path": "a\x00b"})
    assert r.status_code == 400


async def test_create_workspace_nul_in_path_is_4xx(client_500, auth):
    r = await client_500.post("/api/workspaces", headers=auth, json={"name": "n", "root_path": str(ALLOWED) + "/a\x00b"})
    assert 400 <= r.status_code < 500


async def test_register_rejects_symlink_escape_missing_and_dir(client, auth, new_session, make_dir, outside_dir):
    root = make_dir()
    (outside_dir / "o.txt").write_text("outside")
    (root / "link.txt").symlink_to(outside_dir / "o.txt")
    (root / "sub").mkdir()
    ws, _ = await new_session(root)

    async def reg(p):
        return await client.post("/api/artifacts", headers=auth, json={"workspace_id": ws["id"], "rel_path": p})

    r = await reg("link.txt")
    assert r.status_code == 403 and r.json()["code"] == "outside_root"
    r = await reg("missing.txt")
    assert r.status_code == 404
    r = await reg("sub")
    assert r.status_code == 400 and r.json()["code"] == "not_a_file"
    assert (await client.post("/api/artifacts", headers=auth,
                              json={"workspace_id": str(uuid.uuid4()), "rel_path": "x"})).status_code == 404


async def test_register_sensitive_and_export_denied_policy(client, auth, new_session, make_dir):
    root = make_dir()
    (root / ".env").write_text("A=1")
    (root / "plain.txt").write_text("p")
    ws, _ = await new_session(root)
    r = await client.post("/api/artifacts", headers=auth, json={"workspace_id": ws["id"], "rel_path": ".env"})
    assert r.status_code == 201 and r.json()["export_policy"] == "sensitive"
    assert (await client.get(f"/api/artifacts/{r.json()['id']}/content", headers=auth)).status_code == 403

    await client.patch(f"/api/workspaces/{ws['id']}", json={"export_allowed": False}, headers=auth)
    r = await client.post("/api/artifacts", headers=auth, json={"workspace_id": ws["id"], "rel_path": "plain.txt"})
    a = r.json()
    assert a["export_policy"] == "deny" and a["downloadable"] is False
    # deny 는 원격 열람은 되지만 반출(다운로드 링크)은 막힌다.
    assert (await client.get(f"/api/artifacts/{a['id']}/content", headers=auth)).status_code == 200
    r = await client.post(f"/api/artifacts/{a['id']}/download-link", headers=auth)
    assert r.status_code == 403 and r.json()["code"] == "deny"


async def test_register_docx_enqueues_preview(client, auth, new_session, make_dir):
    root = make_dir()
    (root / "x.docx").write_bytes(b"zz")
    ws, _ = await new_session(root)
    a = (await client.post("/api/artifacts", headers=auth, json={"workspace_id": ws["id"], "rel_path": "x.docx"})).json()
    assert a["preview_status"] == "pending"
    assert (await _wait_preview(client, auth, a["id"]))["preview_status"] in ("unavailable", "ready", "failed")


# ---- 삭제·목록

async def test_delete_then_404_everywhere(client, auth, new_session):
    _, _, _, arts = await _run_artifacts(client, auth, new_session)
    a = arts["report.md"]
    store_dir = get_settings().artifact_store / a["id"]
    assert store_dir.exists()
    link = (await client.post(f"/api/artifacts/{a['id']}/download-link", headers=auth)).json()["url"]
    assert (await client.delete(f"/api/artifacts/{a['id']}", headers=auth)).status_code == 204
    assert not store_dir.exists()
    for method, path in [("GET", f"/api/artifacts/{a['id']}"), ("GET", f"/api/artifacts/{a['id']}/content"),
                         ("POST", f"/api/artifacts/{a['id']}/download-link"), ("DELETE", f"/api/artifacts/{a['id']}"),
                         ("GET", link)]:
        r = await client.request(method, path, headers=auth)
        assert r.status_code == 404, (method, path)
    assert all(x["id"] != a["id"] for x in (await client.get("/api/artifacts", headers=auth)).json())


async def test_list_filters(client, auth, new_session, make_dir):
    root = make_dir()
    (root / "100%_done.md").write_text("a")
    (root / "1000 done.md").write_text("b")
    (root / "sheet.csv").write_text("a,b")
    ws, sess = await new_session(root)
    for p in ("100%_done.md", "1000 done.md", "sheet.csv"):
        await client.post("/api/artifacts", headers=auth, json={"workspace_id": ws["id"], "rel_path": p})
    run = await run_to_end(client, auth, sess["id"], "outputs")
    assert run["status"] == "succeeded"

    async def ls(**params):
        r = await client.get("/api/artifacts", params=params, headers=auth)
        assert r.status_code == 200
        return [a["filename"] for a in r.json()]

    assert sorted(await ls(run_id=run["id"])) == ["report.md"]
    assert sorted(await ls(workspace_id=ws["id"])) == ["100%_done.md", "1000 done.md", "report.md", "sheet.csv"]
    assert await ls(workspace_id=str(uuid.uuid4())) == []
    assert sorted(await ls(session_id=sess["id"])) == ["report.md"]
    assert await ls(kind="sheet") == ["sheet.csv"]
    assert sorted(await ls(q="DONE")) == ["100%_done.md", "1000 done.md"]  # 대소문자 무시
    assert await ls(q="100%") == ["100%_done.md"]  # % 는 와일드카드가 아니라 문자
    assert await ls(q="1_0") == []  # _ 도 마찬가지
    assert len(await ls(limit=2)) == 2 and len(await ls(limit=2, offset=3)) == 1
    assert (await client.get("/api/artifacts", params={"limit": 201}, headers=auth)).status_code == 422


async def test_list_orders_newest_first(client, auth, new_session, make_dir):
    root = make_dir()
    ws, _ = await new_session(root)
    for n in ("a.txt", "b.txt", "c.txt"):
        (root / n).write_text(n)
        await client.post("/api/artifacts", headers=auth, json={"workspace_id": ws["id"], "rel_path": n})
    r = await client.get("/api/artifacts", headers=auth)
    assert [a["filename"] for a in r.json()] == ["c.txt", "b.txt", "a.txt"]


# ---- 참조 모드(큰 파일): 열람 시점 재검증

@pytest.fixture
def reference_mode(monkeypatch):
    monkeypatch.setattr(get_settings(), "copy_max_bytes", 1)


async def _ref_art(client, auth, new_session, make_dir):
    root = make_dir()
    (root / "big.txt").write_text("original content")
    ws, _ = await new_session(root)
    a = (await client.post("/api/artifacts", headers=auth, json={"workspace_id": ws["id"], "rel_path": "big.txt"})).json()
    assert a["storage_mode"] == "reference"
    return root, a


async def test_reference_mode_serves_original_and_detects_symlink_swap(client, auth, new_session, make_dir, outside_dir, reference_mode):
    root, a = await _ref_art(client, auth, new_session, make_dir)
    assert (await client.get(f"/api/artifacts/{a['id']}/content", headers=auth)).text == "original content"
    (outside_dir / "evil.txt").write_text("evil")
    (root / "big.txt").unlink()
    (root / "big.txt").symlink_to(outside_dir / "evil.txt")
    r = await client.get(f"/api/artifacts/{a['id']}/content", headers=auth)
    assert r.status_code == 400 and r.json()["code"] == "bad_path"
    assert (await client.post(f"/api/artifacts/{a['id']}/download-link", headers=auth)).status_code == 400


async def test_reference_mode_gone_is_410(client, auth, new_session, make_dir, reference_mode):
    root, a = await _ref_art(client, auth, new_session, make_dir)
    (root / "big.txt").unlink()
    r = await client.get(f"/api/artifacts/{a['id']}/content", headers=auth)
    assert r.status_code == 410 and r.json()["code"] == "gone"


async def test_inline_view_link_serves_inline_with_sandbox(client, auth, new_session):
    _, _, _, arts = await _run_artifacts(client, auth, new_session)
    a = arts["report.md"]
    link = (await client.post(f"/api/artifacts/{a['id']}/download-link", params={"inline": "true"}, headers=auth)).json()
    assert link["url"].startswith("/api/view/")
    r = await client.get(link["url"])
    assert r.status_code == 200
    assert r.headers["content-disposition"].startswith("inline")
    assert "sandbox" in r.headers["content-security-policy"]
