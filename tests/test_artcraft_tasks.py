"""ArtCraft 작업 플랫폼(ADR 0039): 카탈로그, 작업 생성·라운드·갤러리, 작업 폴더·MCP 연결, 가드. 가짜 claude 와 가짜 bin/artcraft 를 쓴다."""
from __future__ import annotations

import json
import stat
from pathlib import Path

import pytest
from conftest import agent_calls
from helpers import wait_run

from ttakkari import artcraft_catalog as catalog
from ttakkari.artifacts.kinds import detect
from ttakkari.config import get_settings
from ttakkari.security.guard import GuardConfig, evaluate

REAL_REGISTRY = Path.home() / "development/projects/artcraft/registry.json"


@pytest.fixture
def platform(tmp_path, make_dir, monkeypatch):
    """가짜 ArtCraft 저장소(bin/artcraft + registry.json)와 allowed_roots 안의 작업 워크스페이스 루트."""
    base = tmp_path / "artcraft"
    (base / "bin").mkdir(parents=True)
    script = base / "bin" / "artcraft"
    script.write_text("#!/bin/sh\nexit 0\n")
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    apps = {app: {"title": app, "domain": app, "site_status": "x", "control_port": None, "control_auth": "none-documented",
                  "env": {}, "mcp_args": [], "cli_modes": []}
            for cat in catalog.CATEGORIES for app in catalog.app_ids(cat)}
    (base / "registry.json").write_text(json.dumps({"apps": apps}))
    root = make_dir()
    s = get_settings()
    monkeypatch.setattr(s, "artcraft_root", base)
    monkeypatch.setattr(s, "artcraft_workspace_root", root)
    return type("P", (), {"base": base, "root": root})()


async def _catalog(client, auth) -> dict:
    r = await client.get("/api/artcraft/catalog", headers=auth)
    assert r.status_code == 200, r.text
    return r.json()


async def _upload(client, auth, ws_id, name, data=b"%PDF-1.4 fake", mime="application/pdf"):
    r = await client.post("/api/uploads", files={"file": (name, data, mime)}, data={"workspace_id": ws_id}, headers=auth)
    assert r.status_code == 201, r.text
    return r.json()


async def _task(client, auth, **body):
    body.setdefault("category", "pdf")
    body.setdefault("prompt", "ART_OUT 합쳐 주세요")
    r = await client.post("/api/artcraft/tasks", json=body, headers=auth)
    return r


async def _finish(client, auth, task: dict) -> dict:
    for rnd in task["rounds"]:
        await wait_run(client, auth, rnd["id"])
    return (await client.get(f"/api/artcraft/tasks/{task['id']}", headers=auth)).json()


def _mcp(call: dict) -> dict:
    # SEC2-AGT-01: 파일 전달이므로 가짜 엔진이 실행 중에 기록한 redacted 상세를 쓴다(env 는 키 목록).
    meta = call.get("mcp_config") or {}
    if meta.get("mode") in ("file", "inline"):
        return meta["details"]
    argv = call["argv"]
    return json.loads(argv[argv.index("--mcp-config") + 1])["mcpServers"]


# ---- 카탈로그

def test_catalog_is_self_consistent():
    registry = {app for cat in catalog.CATEGORIES for app in catalog.app_ids(cat)}
    assert catalog.validate(registry) == []
    assert len(catalog.CATEGORIES) == 12 and len({c.id for c in catalog.CATEGORIES}) == 12
    assert catalog.validate(registry - {"pdfcraft"})  # registry 에 없는 앱은 문제로 드러난다


@pytest.mark.skipif(not REAL_REGISTRY.exists(), reason="ArtCraft 저장소가 설치되지 않은 환경")
def test_catalog_apps_exist_in_real_registry_and_cover_all_13():
    apps = set(json.loads(REAL_REGISTRY.read_text())["apps"])
    assert catalog.validate(apps) == []
    assert {app for cat in catalog.CATEGORIES for app in catalog.app_ids(cat)} == apps


async def test_catalog_endpoint_lists_sections_categories_and_workspace(client, auth, platform):
    data = await _catalog(client, auth)
    assert [s["id"] for s in data["sections"]] == ["image", "av", "doc", "cad"]
    pdf = next(c for c in data["categories"] if c["id"] == "pdf")
    assert pdf["title"] == "PDF 편집" and pdf["maturity"]["level"] == "ready" and pdf["can"] and pdf["cannot"]
    assert pdf["apps"][0]["app"] == "pdfcraft" and pdf["apps"][0]["installed"] is False
    assert any(t["id"] == "merge" and t["min_files"] == 2 for t in pdf["templates"])
    assert data["workspace_id"] and data["workspace_error"] is None
    # 작업 워크스페이스는 한 번만 만들고 재사용한다.
    assert (await _catalog(client, auth))["workspace_id"] == data["workspace_id"]


async def test_catalog_reports_unavailable_repository(client, auth, make_dir, monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "artcraft_root", make_dir())
    monkeypatch.setattr(s, "artcraft_workspace_root", make_dir())
    data = await _catalog(client, auth)
    assert data["available"] is False and data["unavailable_reason"]
    assert len(data["categories"]) == 12  # 안내는 그대로 보인다


# ---- 작업 생성과 실행

async def test_task_runs_end_to_end_with_mcp_note_and_outputs(client, auth, platform):
    ws_id = (await _catalog(client, auth))["workspace_id"]
    a = await _upload(client, auth, ws_id, "a.pdf")
    b = await _upload(client, auth, ws_id, "b.pdf")
    r = await _task(client, auth, template_id="merge", input_artifact_ids=[a["id"], b["id"]])
    assert r.status_code == 201, r.text
    task = r.json()
    assert task["category"] == "pdf" and task["apps"] == ["pdfcraft"] and task["state"] in ("queued", "running")
    done = await _finish(client, auth, task)
    assert done["state"] == "done" and done["output_count"] == 2 and done["round_count"] == 1
    rnd = done["rounds"][0]
    assert {o["filename"] for o in rnd["outputs"]} == {"report.md", "art-result-1.txt"}  # outbox + out/ 수집
    assert [i["filename"] for i in rnd["inputs"]] == ["a.pdf", "b.pdf"]
    assert rnd["messages"] == ["done"] and rnd["result_text"] == "done"  # 라운드의 마지막 답을 함께 준다

    call = agent_calls()[-1]
    servers = _mcp(call)
    spec = servers["artcraft_pdfcraft"]
    task_dir = platform.root / task["id"]
    assert spec["command"] == str(platform.base / "bin" / "artcraft")
    assert spec["args"] == ["mcp", "pdfcraft", "--root", str(task_dir), "--compact"]
    assert "ttakkari" in servers
    argv = call["argv"]
    note = argv[argv.index("--append-system-prompt") + 1]
    assert str(task_dir) in note and "ArtCraft 작업 지침" in note and "pdfcraft" in note
    prompt = argv[argv.index("-p") + 1]
    assert f"{task_dir}/in/r1/a.pdf" in prompt and "[사용자 지시]" in prompt and "ART_OUT" in prompt
    assert (task_dir / "in" / "r1" / "b.pdf").read_bytes() == b"%PDF-1.4 fake"
    assert (task_dir / "out" / "art-result-1.txt").exists()


async def test_task_session_stays_out_of_session_lists(client, auth, platform):
    task = (await _task(client, auth)).json()
    await _finish(client, auth, task)
    sessions = (await client.get("/api/sessions", headers=auth)).json()
    assert task["session_id"] not in {s["id"] for s in sessions}
    # 세션 자체는 열 수 있다(대화 기록 열기 링크)
    assert (await client.get(f"/api/sessions/{task['session_id']}", headers=auth)).status_code == 200


async def test_task_create_validates_inputs(client, auth, platform):
    ws_id = (await _catalog(client, auth))["workspace_id"]
    exe = await _upload(client, auth, ws_id, "run.exe", b"MZ", "application/octet-stream")
    pdf = await _upload(client, auth, ws_id, "one.pdf")
    cases = [
        ({"category": "nope"}, 404, "unknown_category"),
        ({"apps": ["photocraft"]}, 422, "unknown_app"),
        ({"template_id": "nope"}, 422, "bad_input"),
        ({"template_id": "merge", "input_artifact_ids": [pdf["id"]]}, 422, "bad_input"),  # 2개 필요
        ({"input_artifact_ids": [exe["id"]]}, 422, "bad_input"),  # 받지 않는 확장자
        ({"input_artifact_ids": ["00000000-0000-0000-0000-000000000000"]}, 422, "bad_input"),
        ({"category": "photo", "template_id": "resize", "options": {"format": "exe"}, "input_artifact_ids": []}, 422, "bad_input"),
    ]
    for body, status, code in cases:
        r = await _task(client, auth, **body)
        assert r.status_code == status, (body, r.text)
        assert r.json()["code"] == code, (body, r.text)
    assert (await client.post("/api/artcraft/tasks", json={"category": "pdf", "prompt": ""}, headers=auth)).status_code == 422
    assert (await client.get("/api/artcraft/tasks", headers=auth)).json()["items"] == []  # 실패한 요청은 작업을 남기지 않는다


async def test_photo_task_uses_template_apps_and_options(client, auth, platform):
    ws_id = (await _catalog(client, auth))["workspace_id"]
    img = await _upload(client, auth, ws_id, "p.png", b"\x89PNG fake", "image/png")
    r = await _task(client, auth, category="photo", template_id="resize", prompt="ART_OUT 줄여줘",
                    input_artifact_ids=[img["id"]], options={"format": "webp"}, speed="fast")
    assert r.status_code == 201, r.text
    task = await _finish(client, auth, r.json())
    assert task["apps"] == ["photocraft"] and task["options"] == {"format": "webp"}
    call = agent_calls()[-1]
    spec = _mcp(call)["artcraft_photocraft"]
    task_dir = str(platform.root / task["id"])
    assert spec["args"] == ["mcp", "photocraft", "--automation-read-root", task_dir, "--automation-write-root", task_dir]
    prompt = call["argv"][call["argv"].index("-p") + 1]
    assert "저장 형식=WEBP" in prompt
    assert call["argv"][call["argv"].index("--effort") + 1] == "low"  # speed=fast


async def test_unconfined_app_gets_no_root_flags_and_all_category_apps_by_default(client, auth, platform):
    ws_id = (await _catalog(client, auth))["workspace_id"]
    img = await _upload(client, auth, ws_id, "p.jpg", b"jpg", "image/jpeg")
    task = await _finish(client, auth, (await _task(client, auth, category="photo", prompt="ART_OUT 보정",
                                                    input_artifact_ids=[img["id"]])).json())
    assert task["apps"] == ["photocraft", "lightcraft"]
    servers = _mcp(agent_calls()[-1])
    assert servers["artcraft_lightcraft"]["args"] == ["mcp", "lightcraft", "--compact"]
    assert servers["artcraft_photocraft"]["args"][:2] == ["mcp", "photocraft"]


async def test_follow_up_round_gets_previous_outputs_and_sessions_resume(client, auth, platform):
    task = await _finish(client, auth, (await _task(client, auth)).json())
    first_session = agent_calls()[-1]["argv"]
    r = await client.post(f"/api/artcraft/tasks/{task['id']}/runs", json={"prompt": "ART_OUT 더 줄여 주세요"}, headers=auth)
    assert r.status_code == 202, r.text
    detail = await _finish(client, auth, r.json())
    assert detail["round_count"] == 2 and detail["state"] == "done"
    second = agent_calls()[-1]["argv"]
    prompt = second[second.index("-p") + 1]
    task_dir = platform.root / task["id"]
    assert "라운드 2" in prompt and f"{task_dir}/prev/r2/" in prompt
    assert (task_dir / "prev" / "r2" / "art-result-1.txt").exists()
    assert "--resume" in second and "--resume" not in first_session  # 같은 세션으로 이어 간다
    assert [o["filename"] for o in detail["rounds"][1]["outputs"]] == ["art-result-2.txt", "report.md"]


async def test_follow_up_can_skip_previous_and_attach_a_chosen_output(client, auth, platform):
    task = await _finish(client, auth, (await _task(client, auth)).json())
    chosen = next(o for o in task["rounds"][0]["outputs"] if o["filename"] == "art-result-1.txt")
    r = await client.post(f"/api/artcraft/tasks/{task['id']}/runs",
                          json={"prompt": "ART_OUT 이어서", "input_artifact_ids": [chosen["id"]], "use_previous": False}, headers=auth)
    assert r.status_code == 202, r.text
    detail = await _finish(client, auth, r.json())
    call = agent_calls()[-1]["argv"]
    prompt = call[call.index("-p") + 1]
    assert f"{platform.root / task['id']}/in/r2/art-result-1.txt" in prompt and "이전 결과 파일" not in prompt
    assert [i["filename"] for i in detail["rounds"][1]["inputs"]] == ["art-result-1.txt"]
    # 받지 않는 형식(PDF 범주의 xlsx)은 이어서 지시에서도 거절된다
    ws_id = (await _catalog(client, auth))["workspace_id"]
    xl = await _upload(client, auth, ws_id, "t.xlsx", b"PK", "application/octet-stream")
    bad = await client.post(f"/api/artcraft/tasks/{task['id']}/runs", json={"prompt": "x", "input_artifact_ids": [xl["id"]]}, headers=auth)
    assert bad.status_code == 422 and bad.json()["code"] == "bad_input"


async def test_failed_run_is_reported_with_error(client, auth, platform):
    task = await _finish(client, auth, (await _task(client, auth, prompt="FAIL 실패해라")).json())
    assert task["state"] == "failed" and task["error"] and task["rounds"][0]["error"]


# ---- 목록·갤러리·보관

async def test_list_filters_by_category_state_and_archive(client, auth, platform):
    pdf = await _finish(client, auth, (await _task(client, auth)).json())
    doc = await _finish(client, auth, (await _task(client, auth, category="doc", prompt="ART_OUT 문서")).json())
    items = (await client.get("/api/artcraft/tasks", params={"category": "doc"}, headers=auth)).json()["items"]
    assert [i["id"] for i in items] == [doc["id"]] and items[0]["output_count"] == 2
    assert (await client.get("/api/artcraft/tasks", params={"state": "active"}, headers=auth)).json()["items"] == []
    r = await client.patch(f"/api/artcraft/tasks/{pdf['id']}", json={"archived": True, "title": "보관할 작업"}, headers=auth)
    assert r.status_code == 200 and r.json()["title"] == "보관할 작업" and r.json()["archived_at"]
    ids = [i["id"] for i in (await client.get("/api/artcraft/tasks", headers=auth)).json()["items"]]
    assert ids == [doc["id"]]
    blocked = await client.post(f"/api/artcraft/tasks/{pdf['id']}/runs", json={"prompt": "다시"}, headers=auth)
    assert blocked.status_code == 409


async def test_task_can_be_found_from_its_session_even_when_archived(client, auth, platform):
    task = await _finish(client, auth, (await _task(client, auth)).json())
    await client.patch(f"/api/artcraft/tasks/{task['id']}", json={"archived": True}, headers=auth)
    items = (await client.get("/api/artcraft/tasks", params={"session_id": task["session_id"]}, headers=auth)).json()["items"]
    assert [i["id"] for i in items] == [task["id"]]
    other = (await client.get("/api/artcraft/tasks", params={"session_id": "00000000-0000-0000-0000-000000000000"}, headers=auth)).json()
    assert other["items"] == []


async def test_active_task_is_listed_as_active(client, auth, platform):
    task = (await _task(client, auth, prompt="SLEEP 오래 걸려요")).json()
    items = (await client.get("/api/artcraft/tasks", params={"state": "active"}, headers=auth)).json()["items"]
    assert [i["id"] for i in items] == [task["id"]] and items[0]["state"] in ("queued", "running")
    await client.post(f"/api/runs/{task['rounds'][0]['id']}/cancel", headers=auth)
    done = await _finish(client, auth, task)
    assert done["state"] == "cancelled"


async def test_outputs_gallery_filters_and_pages(client, auth, platform):
    await _finish(client, auth, (await _task(client, auth)).json())
    await _finish(client, auth, (await _task(client, auth, category="doc", prompt="ART_OUT 문서")).json())
    allout = (await client.get("/api/artcraft/outputs", headers=auth)).json()
    assert len(allout["items"]) == 4 and allout["has_more"] is False
    assert {i["category"] for i in allout["items"]} == {"pdf", "doc"}
    docs = (await client.get("/api/artcraft/outputs", params={"category": "doc", "q": "art-result"}, headers=auth)).json()
    assert [i["artifact"]["filename"] for i in docs["items"]] == ["art-result-1.txt"]
    page = (await client.get("/api/artcraft/outputs", params={"limit": 3}, headers=auth)).json()
    assert len(page["items"]) == 3 and page["has_more"] is True
    assert (await client.get("/api/artcraft/outputs", params={"q": "%"}, headers=auth)).json()["items"] == []  # 와일드카드 이스케이프


async def test_endpoints_require_login(client):
    for method, path in (("get", "/api/artcraft/catalog"), ("get", "/api/artcraft/tasks"), ("get", "/api/artcraft/outputs"),
                         ("post", "/api/artcraft/tasks")):
        assert (await getattr(client, method)(path)).status_code == 401


# ---- 보조 규칙

def test_audio_and_video_have_their_own_artifact_kinds():
    assert detect(Path("tone.wav")) == ("audio/wav", "audio")
    assert detect(Path("song.flac"))[1] == "audio" and detect(Path("a.m4a"))[1] == "audio"
    assert detect(Path("clip.mp4"))[1] == "video" and detect(Path("clip.mov")) == ("video/quicktime", "video")
    assert detect(Path("x.png"))[1] == "image" and detect(Path("a.dxf"))[1] == "other"


def test_guard_blocks_protected_and_sensitive_paths_in_artcraft_mcp_calls(tmp_path: Path):
    protected = tmp_path / "ttk-home"
    protected.mkdir()
    work = tmp_path / "work"
    work.mkdir()
    cfg = GuardConfig(write_roots=[work], protected=[protected])
    tool = "mcp__artcraft_gridcraft__open_workbook"
    assert evaluate(tool, {"path": str(work / "a.xlsx")}, str(work), cfg) is None
    assert evaluate(tool, {"path": str(protected / "data.xlsx")}, str(work), cfg)
    assert evaluate(tool, {"steps": [{"args": {"file": str(protected / "x")}}]}, str(work), cfg)  # 중첩 인자도 본다
    assert evaluate(tool, {"path": str(work / ".env")}, str(work), cfg)  # 민감 파일
    assert evaluate("mcp__ttakkari__recall", {"query": str(protected)}, str(work), cfg) is None  # 다른 MCP 는 건드리지 않는다
