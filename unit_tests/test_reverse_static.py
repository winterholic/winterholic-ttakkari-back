"""리버스 엔지니어링 순수 함수 테스트(DB 불필요, 샌드박스 실행용).

API 종단 테스트는 tests/test_reverse.py가 맡는다. 여기는 DB 없이 도는 것만 둔다.
"""
import json
from datetime import timedelta
from pathlib import Path

import pytest

from ttakkari import reverse
from ttakkari.config import get_settings


@pytest.fixture
def allowed_root(tmp_path, monkeypatch):
    root = tmp_path / "allowed"
    root.mkdir()
    (tmp_path / "corp").mkdir()
    monkeypatch.setenv("TTAKKARI_NO_ENV_FILE", "1")
    monkeypatch.setenv("TTAKKARI_ALLOWED_ROOTS", json.dumps([str(root), str(tmp_path / "corp")]))
    monkeypatch.setenv("TTAKKARI_COMPANY_ROOTS", json.dumps([str(tmp_path / "corp")]))
    get_settings.cache_clear()
    yield root
    get_settings.cache_clear()


def _repo(root: Path) -> None:
    (root / "README.md").write_text("# Sample\n\n샘플 저장소다.\n\n## Quick start\n\n- a\n")
    (root / "package.json").write_text(json.dumps(
        {"name": "sample", "version": "0.1.0", "bin": {"sample": "./bin/cli.js"}}))
    (root / "bin").mkdir()
    (root / "bin" / "cli.js").write_text("import { run } from '../src/app.js';\nrun();\n")
    (root / "src").mkdir()
    (root / "src" / "app.js").write_text(
        "import { helper } from './util.js';\nexport function run() { helper(); }\n")
    (root / "src" / "util.js").write_text("export function helper() {}\n")
    (root / ".env").write_text("KEY=SECRET-SENTINEL-do-not-leak\n")


def test_normalize_github_forms():
    assert reverse.normalize_target("morluto/rea") == {
        "kind": "git_url", "url": "https://github.com/morluto/rea.git", "label": "morluto/rea"}


def test_normalize_rejects_unsafe(allowed_root):
    for bad in ["", "git@github.com:x/y.git", "http://github.com/x/y.git",
                "file:///etc/passwd", "https://x@github.com/x/y.git",
                "https://127.0.0.1/x/y.git", "https://localhost/x/y.git",
                "https://10.0.0.1/x/y.git", "https://169.254.169.254/x/y.git",
                "https://gitlab.com/x/y.git", "https://github.com.evil.com/x/y.git",
                "https://github.com:8443/morluto/rea.git",
                "morluto/re\x00a", "morluto/rea\n",
                "/", "~", str(Path.home() / ".ssh"), "/etc", "/nonexistent-dir-xyz"]:
        with pytest.raises(reverse.ReverseError):
            reverse.normalize_target(bad)
    assert reverse.normalize_target(str(allowed_root))["kind"] == "local_path"
    assert reverse.normalize_target("https://github.com:443/morluto/rea.git")["kind"] == "git_url"


def test_company_path_refused(tmp_path, monkeypatch):
    root = tmp_path / "allowed"
    corp = tmp_path / "corp"
    root.mkdir()
    corp.mkdir()
    monkeypatch.setenv("TTAKKARI_NO_ENV_FILE", "1")
    monkeypatch.setenv("TTAKKARI_ALLOWED_ROOTS", json.dumps([str(root), str(corp)]))
    monkeypatch.setenv("TTAKKARI_COMPANY_ROOTS", json.dumps([str(corp)]))
    get_settings.cache_clear()
    try:
        with pytest.raises(reverse.ReverseError) as exc:
            reverse.normalize_target(str(corp))
        assert exc.value.code == "company_path"
        assert reverse.normalize_target(str(root))["kind"] == "local_path"
    finally:
        get_settings.cache_clear()


def test_secrets_never_read(tmp_path):
    (tmp_path / ".env").write_text("KEY=SECRET-SENTINEL-do-not-leak\n")
    (tmp_path / "a.py").write_text("x = 1\n")
    inv = reverse.inventory(tmp_path)
    assert inv["secrets"] == 1
    assert "SECRET-SENTINEL" not in json.dumps(inv)


def test_walk_cap_prunes(tmp_path, monkeypatch):
    (tmp_path / "node_modules").mkdir()
    for i in range(30):
        (tmp_path / "node_modules" / f"lib{i}.js").write_text("x\n")
    (tmp_path / "src").mkdir()
    for i in range(10):
        (tmp_path / "src" / f"a{i}.py").write_text("x = 1\n")
    (tmp_path / "linked").symlink_to(tmp_path / "src", target_is_directory=True)
    monkeypatch.setattr(reverse, "WALK_CAP", 5)
    inv = reverse.inventory(tmp_path)
    assert inv["truncated"] is True
    assert all("node_modules" not in f["path"] and "linked" not in f["path"]
               for f in inv["files"])


def test_structure_entries_and_symlinks(tmp_path):
    _repo(tmp_path)
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "main.py").write_text("x = 1\n")
    (tmp_path / "linked").symlink_to(tmp_path / "src", target_is_directory=True)
    inv = reverse.inventory(tmp_path)
    struct = reverse.structure(tmp_path, inv)
    assert all(d["dir"] != "linked" for d in struct["topdirs"])
    assert all(e["path"] != "tests/main.py" for e in struct["entries"])
    flow = reverse.analyze_flows(tmp_path, inv, struct)
    assert flow["hubs"]
    result = reverse.build_result({"label": "t", "kind": "local_path"}, "abc123",
                                  inv, struct, flow)
    assert result["ideas"] == []
    assert any("정적 관찰만" in line for line in result["limitations"])
    assert "SECRET-SENTINEL" not in json.dumps(result)


def test_validate_synthesis_drops_unevidenced():
    known = {"a.py", "b.py"}
    texts = {"a.py": {3: "def run(): helper()"}, "b.py": {1: "import app"}}
    data = {"overview": {"text": "a.py의 run이 핵심이다.", "evidence": ["a.py:3"]},
            "architecture": [{"title": "구조", "body": "run 설명.", "evidence": ["a.py:3"]},
                             {"title": "허위", "body": "근거 없음.", "evidence": []}],
            "modules": [{"path": "a.py", "role": "run 역할.", "evidence": ["ghost.py:1"]},
                        {"path": "ghost.py", "role": "없는 경로.", "evidence": ["a.py:3"]}],
            "flows": [{"title": "흐름", "steps": ["a.py"], "evidence": ["b.py:99"]}],
            "ideas": [{"title": "아이디어", "body": "app 구조 차용.", "evidence": ["b.py:1"]}],
            "limitations": ["확인 필요: 런타임"]}
    synth, dropped = reverse._validate_synthesis(data, known, {"a.py": 10, "b.py": 5}, texts)
    assert dropped == 3
    assert [a["title"] for a in synth["architecture"]] == ["구조"]
    assert synth["modules"] == []
    assert synth["flows"][0]["evidence"] == ["b.py"]
    assert synth["ideas"][0]["evidence"] == ["b.py:1"]
    assert synth["overview"] == {"text": "a.py의 run이 핵심이다.", "evidence": ["a.py:3"]}
    assert reverse._synth_usable(synth) is True
    assert reverse._synth_usable({"overview": {"text": "개요", "evidence": []}}) is False


def test_strip_rel_keeps_dot_paths():
    assert reverse._strip_rel("./a.py") == "a.py"
    assert reverse._strip_rel(".agents/skills/x.md") == ".agents/skills/x.md"
    assert reverse._normalize_evidence(".agents/x.md:1", {".agents/x.md"}, {}) == ".agents/x.md:1"


def test_excerpt_masks_secrets(tmp_path):
    (tmp_path / "cfg.py").write_text('api_key = "[REDACTED]"\npassword: hunter2\nok = 1\n')
    text, _count, masked = reverse._excerpt(tmp_path, "cfg.py")
    assert masked == 2
    assert "[REDACTED]" not in text and "hunter2" not in text
    assert "***" in text


def test_quote_relevance_check():
    line = {1: "export class EvidenceMcpServer extends McpServer {"}
    assert reverse._line_relevant("도구 등록의 중심 허브", "src/server/EvidenceMcpServer.ts",
                                  1, {"src/server/EvidenceMcpServer.ts": line}) is True
    assert reverse._line_relevant("완전히 다른 이야기 짓기", "a.py", 1, {"a.py": line}) is False


def test_parse_model_json_tolerates_fence():
    data = reverse._parse_model_json('```json\n{"read": ["a.py"], "notes": []}\n```')
    assert data == {"read": ["a.py"], "notes": []}
    with pytest.raises(reverse.ReverseError):
        reverse._parse_model_json("모델이 JSON을 안 줌")


def test_excerpt_never_reads_secrets(tmp_path):
    (tmp_path / ".env").write_text("KEY=x\n")
    assert reverse._excerpt(tmp_path, ".env") is None
    (tmp_path / "a.py").write_text("x = 1\ny = 2\n")
    text, count, masked = reverse._excerpt(tmp_path, "a.py")
    assert count == 2 and masked == 0 and text.startswith("1: x = 1")


def test_stale_and_fail_stages():
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


def test_survey_excludes_unknown_files(tmp_path):
    _repo(tmp_path)
    inv = reverse.inventory(tmp_path)
    known = {f["path"] for f in inv["files"]}
    assert "nope/missing.js" not in known
    assert reverse._normalize_evidence("nope/missing.js", known, {}) is None
    assert reverse._normalize_evidence("src/app.js:2", known, {"src/app.js": 2}) == "src/app.js:2"
    assert reverse._normalize_evidence("src/app.js:99", known, {"src/app.js": 2}) == "src/app.js"


def test_repo_map_is_bounded(tmp_path):
    _repo(tmp_path)
    inv = reverse.inventory(tmp_path)
    struct = reverse.structure(tmp_path, inv)
    flow = reverse.analyze_flows(tmp_path, inv, struct)
    doc = reverse._repo_map("sample", "abc", inv, struct, flow)
    assert "src/app.js" in doc
    assert len(doc) <= reverse.MAX_MODEL_PROMPT_CHARS + 200
