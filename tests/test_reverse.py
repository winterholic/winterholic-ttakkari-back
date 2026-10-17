"""리버스 엔지니어링: 검증·정규화·로컬 저장소 종단 분석. 네트워크 clone은 건드리지 않는다."""
import asyncio
import json
import uuid
from datetime import timedelta
from pathlib import Path

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


async def _wait_done(client, auth, job_id, timeout=60, prefix="/api/reverse"):
    for _ in range(int(timeout * 2)):
        r = await client.get(f"{prefix}/{job_id}", headers=auth)
        assert r.status_code == 200, r.text
        body = r.json()
        if body["status"] in ("done", "failed"):
            return body
        await asyncio.sleep(0.5)
    raise AssertionError("리버스 파이프라인이 제한 시간 안에 끝나지 않음")


FAKE_SYNTH = {
    "overview": "샘플 저장소는 bin/cli.js에서 시작해 src/app.js의 run()을 호출한다.",
    "architecture": [{"title": "진입-실행 2층", "body": "bin이 진입하고 src가 실행한다.",
                      "evidence": ["bin/cli.js:1", "src/app.js:2"]}],
    "modules": [{"path": "src/app.js", "role": "run() 실행부.",
                 "evidence": ["src/app.js:2"]},
                {"path": "ghost.js", "role": "없는 파일 주장.",
                 "evidence": ["ghost.js:1"]}],
    "flows": [{"title": "cli.js 흐름", "steps": ["bin/cli.js", "src/app.js"],
               "evidence": ["bin/cli.js:1"]}],
    "ideas": [{"title": "테스트용 아이디어", "body": "모델이 낸 것만 아이디어가 된다.",
               "evidence": ["src/app.js:2"]}],
    "limitations": ["확인 필요: 동적 로딩 여부"],
}


def _install_fake_review(monkeypatch):
    async def _fake(system, prompt):
        if system == reverse.SURVEY_SYSTEM:
            return json.dumps({"read": ["bin/cli.js", "src/app.js", "nope/missing.js"],
                                       "evidence": "잡음",
                                       "notes": ["확인 필요: 런타임 주입 여부"]})
        assert system == reverse.SYNTH_SYSTEM
        return json.dumps(FAKE_SYNTH)

    monkeypatch.setattr(reverse, "MODEL_REVIEW", _fake)


async def test_local_repo_end_to_end(client, auth, make_git_dir, monkeypatch):
    _install_fake_review(monkeypatch)
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
                                                 "structure", "flows", "survey",
                                                 "synthesize", "summarize"]
    assert all(s["status"] == "done" for s in body["stages"])
    assert all(s["evidence"] for s in body["stages"])
    survey = next(s for s in body["stages"] if s["key"] == "survey")
    assert any("읽음: bin/cli.js" in e for e in survey["evidence"])
    assert any("확인 필요" in n for n in survey["notes"])
    result = body["result"]
    assert "bin/cli.js에서 시작" in result["overview"]
    assert result["architecture"] and result["modules"] and result["limitations"]
    assert all(m["path"] != "ghost.js" for m in result["modules"])
    assert any("근거 없는 모델 주장" in limit for limit in result["limitations"])
    assert [i["title"] for i in result["ideas"]] == ["테스트용 아이디어"]
    assert result["stats"]["model"]["files_read"] == 2
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
    body = await _wait_done(client, tokens["personal"], job_id, prefix="/api/agent/reverse")
    assert body["status"] == "done", body.get("error")
    assert (await client.get("/api/agent/reverse", headers=tokens["personal"])).status_code == 200
    assert (await client.get("/api/agent/reverse", headers=tokens["work"])).status_code == 403
    assert (await client.get(f"/api/agent/reverse/{job_id}", headers=tokens["personal"])).status_code == 200
    assert (await client.get("/api/agent/design-systems", headers=tokens["personal"])).status_code != 404


def test_normalize_target_rejects_private_hosts_and_local_escapes(make_dir):
    for bad in ["https://127.0.0.1/x/y.git", "https://localhost/x/y.git",
                "https://10.0.0.1/x/y.git", "https://169.254.169.254/x/y.git",
                "https://gitlab.com/x/y.git", "https://github.com.evil.com/x/y.git",
                "/", "~", str(Path.home() / ".ssh"), "/etc"]:
        with pytest.raises(reverse.ReverseError):
            reverse.normalize_target(bad)
    allowed = make_dir()
    assert reverse.normalize_target(str(allowed))["kind"] == "local_path"


def test_walk_cap_prunes_skip_dirs(tmp_path, monkeypatch):
    (tmp_path / "node_modules").mkdir()
    for i in range(30):
        (tmp_path / "node_modules" / f"lib{i}.js").write_text("x\n")
    (tmp_path / "src").mkdir()
    for i in range(10):
        (tmp_path / "src" / f"a{i}.py").write_text("x = 1\n")
    link_target = tmp_path / "real"
    link_target.mkdir()
    (link_target / "b.py").write_text("y = 2\n")
    (tmp_path / "linked").symlink_to(link_target, target_is_directory=True)
    monkeypatch.setattr(reverse, "WALK_CAP", 5)
    inv = reverse.inventory(tmp_path)
    assert inv["truncated"] is True
    assert all("node_modules" not in f["path"] for f in inv["files"])
    assert all("linked" not in f["path"] for f in inv["files"])


def test_structure_skips_symlink_dirs_and_support_entries(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "main.py").write_text("x = 1\n")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "main.py").write_text("x = 1\n")
    (tmp_path / "real").mkdir()
    (tmp_path / "linked").symlink_to(tmp_path / "real", target_is_directory=True)
    inv = reverse.inventory(tmp_path)
    struct = reverse.structure(tmp_path, inv)
    assert all(d["dir"] != "linked" for d in struct["topdirs"])
    assert all(e["path"] != "tests/main.py" for e in struct["entries"])


def test_validate_synthesis_drops_unevidenced():
    known = {"a.py", "b.py"}
    data = {"overview": "개요다.",
            "architecture": [{"title": "구조", "body": "설명.", "evidence": ["a.py:3"]},
                             {"title": "허위", "body": "근거 없음.", "evidence": []}],
            "modules": [{"path": "a.py", "role": "역할.", "evidence": ["ghost.py:1"]}],
            "flows": [{"title": "흐름", "steps": ["a.py"], "evidence": ["b.py:99"]}],
            "ideas": [{"title": "아이디어", "body": "내용.", "evidence": ["a.py"]}],
            "limitations": ["확인 필요: 런타임"]}
    synth, dropped = reverse._validate_synthesis(data, known, {"a.py": 10, "b.py": 5})
    assert dropped == 2
    assert [a["title"] for a in synth["architecture"]] == ["구조"]
    assert synth["modules"] == []
    assert synth["flows"] and synth["flows"][0]["evidence"] == ["b.py"]
    assert synth["ideas"] and synth["ideas"][0]["evidence"] == ["a.py"]


def test_build_result_has_no_template_ideas(tmp_path):
    (tmp_path / "mcp-server").mkdir()
    (tmp_path / "mcp-server" / "mcp.py").write_text("x = 1\n")
    inv = reverse.inventory(tmp_path)
    struct = reverse.structure(tmp_path, inv)
    flow = reverse.analyze_flows(tmp_path, inv, struct)
    result = reverse.build_result({"label": "t", "kind": "local_path"}, None, inv, struct, flow)
    assert result["ideas"] == []
    assert any("정적 관찰만" in limit for limit in result["limitations"])


async def test_model_failure_falls_back_to_static(client, auth, make_git_dir, monkeypatch):
    async def _boom(system, prompt):
        raise reverse.ReverseError(503, "model_failed", "모델 호출 실패(테스트)")

    monkeypatch.setattr(reverse, "MODEL_REVIEW", _boom)
    root = make_git_dir()
    _fixture_repo(root)
    r = await client.post("/api/reverse", json={"target": str(root)}, headers=auth)
    assert r.status_code == 202, r.text
    body = await _wait_done(client, auth, r.json()["id"])
    assert body["status"] == "done", body.get("error")
    by_key = {s["key"]: s for s in body["stages"]}
    assert by_key["survey"]["status"] == "failed"
    assert by_key["synthesize"]["status"] == "failed"
    assert by_key["summarize"]["status"] == "done"
    assert by_key["summarize"]["evidence"]
    assert body["result"]["ideas"] == []
    assert any("정적 관찰만" in limit for limit in body["result"]["limitations"])


def test_fail_stages_and_stale_marking():
    stages = [{"key": "collect", "status": "running", "started_at": "t",
               "finished_at": None, "evidence": [], "notes": []}]
    out = reverse._fail_stages(stages)
    assert out[0]["status"] == "failed" and out[0]["finished_at"]
    assert stages[0]["status"] == "running"

    class _Job:
        status = "running"
        updated_at = reverse.utcnow()

    assert reverse._stale(_Job()) is False
    _Job.updated_at = reverse.utcnow() - timedelta(minutes=reverse.STALE_MINUTES + 1)
    assert reverse._stale(_Job()) is True
