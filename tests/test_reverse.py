"""리버스 엔지니어링: 검증·정규화·로컬 저장소 종단 분석. 네트워크 clone은 건드리지 않는다."""
import asyncio
import json
import uuid

import pytest
from conftest import git

from ttakkari import reverse


def test_normalize_target_accepts_github_forms():
    assert reverse.normalize_target("morluto/rea") == {
        "kind": "git_url", "url": "https://github.com/morluto/rea.git", "label": "morluto/rea"}
    assert reverse.normalize_target("https://github.com/morluto/rea")["url"] == \
        "https://github.com/morluto/rea.git"
    assert reverse.normalize_target("https://github.com/morluto/rea.git")["label"] == "morluto/rea"


def test_normalize_target_rejects_unsafe_inputs(tmp_path):
    for bad in ["", "git@github.com:x/y.git", "http://github.com/x/y.git",
                "file:///etc/passwd", "https://x@github.com/x/y.git", "/nonexistent-dir-xyz"]:
        with pytest.raises(reverse.ReverseError):
            reverse.normalize_target(bad)
    with pytest.raises(reverse.ReverseError):
        reverse.normalize_target(str(tmp_path / "nope"))


def test_secrets_are_never_read(tmp_path):
    (tmp_path / ".env").write_text("KEY=SECRET-SENTINEL-do-not-leak\n")
    (tmp_path / "a.py").write_text("x = 1\n")
    inv = reverse.inventory(tmp_path)
    assert inv["secrets"] == 1
    assert "SECRET-SENTINEL" not in json.dumps(inv)


def _fixture_repo(root):
    (root / "README.md").write_text("# Sample\n\n샘플 저장소다.\n\n## Quick start\n\n- a\n")
    (root / "package.json").write_text(json.dumps(
        {"name": "sample", "version": "0.1.0", "bin": {"sample": "./bin/cli.js"},
         "scripts": {"start": "node bin/cli.js"}}))
    (root / "bin").mkdir()
    (root / "bin" / "cli.js").write_text(
        "import { run } from '../src/app.js';\nrun();\n")
    (root / "src").mkdir()
    (root / "src" / "app.js").write_text(
        "import { helper } from './util.js';\nexport function run() { helper(); }\n")
    (root / "src" / "util.js").write_text("export function helper() {}\n")
    (root / ".env").write_text("KEY=SECRET-SENTINEL-do-not-leak\n")
    # 실행되면 흔적을 남기는 파일: 파이프라인은 읽거나 clone만 하므로 생기지 않아야 한다.
    (root / "evil.py").write_text("from pathlib import Path\nPath('PWNED').write_text('x')\n")
    git(root, "add", "-A")
    git(root, "commit", "-qm", "fixture")


async def _wait_done(client, auth, job_id, timeout=60):
    for _ in range(int(timeout * 2)):
        r = await client.get(f"/api/reverse/{job_id}", headers=auth)
        assert r.status_code == 200, r.text
        body = r.json()
        if body["status"] in ("done", "failed"):
            return body
        await asyncio.sleep(0.5)
    raise AssertionError("리버스 파이프라인이 제한 시간 안에 끝나지 않음")


async def test_local_repo_end_to_end(client, auth, make_git_dir):
    root = make_git_dir()
    _fixture_repo(root)
    r = await client.post("/api/reverse", json={"target": str(root)}, headers=auth)
    assert r.status_code == 202, r.text
    job_id = r.json()["id"]
    listing = await client.get("/api/reverse", headers=auth)
    assert listing.status_code == 200 and any(i["id"] == job_id for i in listing.json()["items"])
    body = await _wait_done(client, auth, job_id)
    assert body["status"] == "done", body.get("error")
    assert [s["key"] for s in body["stages"]] == ["request", "collect", "inventory",
                                                 "structure", "flows", "summarize"]
    assert all(s["status"] == "done" for s in body["stages"])
    assert all(s["evidence"] for s in body["stages"])
    result = body["result"]
    assert "샘플 저장소" in result["overview"]
    assert result["architecture"] and result["modules"] and result["limitations"]
    assert any("cli.js" in " ".join(c.get("steps", [])) or "cli.js" in c.get("title", "")
               for c in result["flows"])
    assert "SECRET-SENTINEL" not in json.dumps(result)
    assert not (root / "PWNED").exists()
    assert (await client.get("/api/reverse/nope", headers=auth)).status_code == 404
    assert (await client.post("/api/reverse", json={"target": "git@x:y.git"}, headers=auth)).status_code == 422
    assert (await client.post("/api/reverse", json={"target": "/nonexistent-xyz"}, headers=auth)).status_code == 422
    assert (await client.post("/api/reverse", headers=auth)).status_code == 422
    assert (await client.get("/api/reverse", headers={"Authorization": "Bearer x"})).status_code == 401
    assert (await client.delete(f"/api/reverse/{job_id}", headers=auth)).status_code == 200
    assert (await client.get(f"/api/reverse/{job_id}", headers=auth)).status_code == 404


async def test_agent_routes_are_personal_only(client, auth, make_git_dir):
    from ttakkari.orchestrator.runtime import issue_token
    root = make_git_dir()
    _fixture_repo(root)
    ws = (await client.post("/api/workspaces", json={"name": "reverse", "root_path": str(root)},
                            headers=auth)).json()
    tokens = {}
    for pool in ("personal", "work"):
        r = await client.post("/api/sessions", json={"workspace_id": ws["id"], "pool": pool}, headers=auth)
        assert r.status_code == 201, r.text
        tokens[pool] = {"Authorization": "Bearer " + issue_token(uuid.UUID(r.json()["id"]))}
    assert (await client.post("/api/agent/reverse/start", json={"target": str(root)},
                              headers=tokens["work"])).status_code == 403
    r = await client.post("/api/agent/reverse/start", json={"target": str(root)}, headers=tokens["personal"])
    assert r.status_code == 202, r.text
    job_id = r.json()["id"]
    body = await _wait_done(client, tokens["personal"], job_id)
    assert body["status"] == "done", body.get("error")
    assert (await client.get("/api/agent/reverse", headers=tokens["personal"])).status_code == 200
    assert (await client.get("/api/agent/reverse", headers=tokens["work"])).status_code == 403
    assert (await client.get(f"/api/agent/reverse/{job_id}", headers=tokens["personal"])).status_code == 200
    assert (await client.get("/api/agent/design-systems", headers=tokens["personal"])).status_code != 404
