"""ArtCraft 작업 플랫폼(ADR 0039): 카탈로그, 작업 생성·라운드·갤러리, 작업 폴더·MCP 연결, 가드. 가짜 claude 와 가짜 bin/artcraft 를 쓴다."""
from __future__ import annotations

import fcntl
import json
import multiprocessing
import os
import stat
import uuid
from pathlib import Path

import pytest
from conftest import agent_calls
from helpers import wait_run

from ttakkari import artcraft_catalog as catalog
from ttakkari.artifacts.kinds import detect
from ttakkari.config import get_settings
from ttakkari.db import sessionmaker
from ttakkari.models import ChatSession, EngineAccount
from ttakkari.runs import profiles
from ttakkari.security.guard import GuardConfig, evaluate

REAL_REGISTRY = Path.home() / "development/projects/artcraft/registry.json"
EXACT_ENGLISH_LOOKUP_PROMPT = (
    "Read the latest version of this document and tell me its exact title, the total budget for two people, "
    "and the budget per person. This is a read-only lookup only: do not create, edit, modify, convert, "
    "or export any file. Reply in English and include only those three values."
)


def _compete_for_file_lock(path: str, contended, acquired) -> None:
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            contended.set()
            fcntl.flock(fd, fcntl.LOCK_EX)
        acquired.set()
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


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


async def test_account_id_is_persisted_and_used_for_all_artcraft_rounds(client, auth, platform):
    accounts = await client.get("/api/accounts", headers=auth)
    assert accounts.status_code == 200, accounts.text
    first = await client.post("/api/accounts", headers=auth,
                              json={"name": "preferred codex", "engine": "codex"})
    second = await client.post("/api/accounts", headers=auth,
                               json={"name": "higher priority codex", "engine": "codex"})
    assert first.status_code == second.status_code == 201
    preferred_id = first.json()["id"]
    competitor_id = second.json()["id"]
    async with sessionmaker()() as db:
        preferred = await db.get(EngineAccount, uuid.UUID(preferred_id))
        competitor = await db.get(EngineAccount, uuid.UUID(competitor_id))
        preferred.priority = 10
        competitor.priority = 0
        await db.commit()

    created = await _task(client, auth, account_id=preferred_id)
    assert created.status_code == 201, created.text
    task = created.json()
    async with sessionmaker()() as db:
        session = await db.get(ChatSession, uuid.UUID(task["session_id"]))
        assert session.account_id == uuid.UUID(preferred_id)
    first_run = await wait_run(client, auth, task["rounds"][0]["id"])
    assert first_run["profile"] == "preferred codex"

    mismatch = await client.post(f"/api/artcraft/tasks/{task['id']}/runs", headers=auth,
                                 json={"prompt": "continue", "account_id": competitor_id})
    assert mismatch.status_code == 409 and mismatch.json()["code"] == "account_pin_immutable"
    followup = await client.post(f"/api/artcraft/tasks/{task['id']}/runs", headers=auth,
                                 json={"prompt": "continue"})
    assert followup.status_code == 202, followup.text
    detail = (await client.get(f"/api/artcraft/tasks/{task['id']}", headers=auth)).json()
    async with sessionmaker()() as db:
        session = await db.get(ChatSession, uuid.UUID(task["session_id"]))
        assert session.account_id == uuid.UUID(preferred_id)
    assert len(detail["rounds"]) == 2
    second_run = await wait_run(client, auth, detail["rounds"][1]["id"])
    assert second_run["profile"] == "preferred codex"


async def test_invalid_account_id_fails_closed(client, auth, platform):
    response = await _task(client, auth, account_id="00000000-0000-0000-0000-000000000000")
    assert response.status_code == 503, response.text
    assert response.json()["code"] == "account_unavailable"


async def test_work_account_can_be_pinned_to_personal_artcraft_task(client, auth, platform):
    response = await client.post("/api/accounts", headers=auth,
                                 json={"name": "work codex for personal task", "engine": "codex", "pool": "work",
                                       "enabled": True})
    assert response.status_code == 201, response.text

    created = await _task(client, auth, account_id=response.json()["id"])
    assert created.status_code == 201, created.text
    run = await wait_run(client, auth, created.json()["rounds"][0]["id"])
    assert run["profile"] == "work codex for personal task"


async def test_personal_account_cannot_be_pinned_to_work_session(client, auth, platform):
    account = await client.post("/api/accounts", headers=auth,
                                json={"name": "personal codex", "engine": "codex", "pool": "personal"})
    assert account.status_code == 201, account.text
    created = await _task(client, auth, account_id=account.json()["id"])
    assert created.status_code == 201, created.text

    from ttakkari.models import Run
    from ttakkari.runs.manager import _eligible_profiles

    async with sessionmaker()() as db:
        session = await db.get(ChatSession, uuid.UUID(created.json()["session_id"]))
        run = await db.get(Run, uuid.UUID(created.json()["rounds"][0]["id"]))
        session.pool = "work"
        await db.commit()
        free, blocked = await _eligible_profiles(db, session, run, False)
    assert free == [] and blocked == []


async def test_deleted_pinned_account_does_not_fall_back_to_peer(client, auth, platform):
    pinned = await client.post("/api/accounts", headers=auth,
                               json={"name": "deleted pinned codex", "engine": "codex"})
    peer = await client.post("/api/accounts", headers=auth,
                             json={"name": "available peer codex", "engine": "codex"})
    assert pinned.status_code == peer.status_code == 201
    created = await _task(client, auth, account_id=pinned.json()["id"])
    assert created.status_code == 201, created.text

    from ttakkari.models import Run
    from ttakkari.runs.manager import _eligible_profiles

    async with sessionmaker()() as db:
        session = await db.get(ChatSession, uuid.UUID(created.json()["session_id"]))
        run = await db.get(Run, uuid.UUID(created.json()["rounds"][0]["id"]))
        await db.delete(await db.get(EngineAccount, uuid.UUID(pinned.json()["id"])))
        await db.commit()
        free, blocked = await _eligible_profiles(db, session, run, False)
    assert free == [] and blocked == []


async def test_requested_account_must_be_enabled_and_ready(client, auth, platform, monkeypatch):
    disabled = await client.post("/api/accounts", headers=auth,
                                 json={"name": "disabled account", "engine": "codex", "enabled": False})
    limited = await client.post("/api/accounts", headers=auth,
                                json={"name": "limited account", "engine": "codex"})
    unready = await client.post("/api/accounts", headers=auth,
                                json={"name": "unready account", "engine": "codex"})
    assert all(r.status_code == 201 for r in (disabled, limited, unready))
    profiles.cool_down(uuid.UUID(limited.json()["id"]), 600)
    original_ready = profiles.execution_ready
    monkeypatch.setattr(profiles, "execution_ready",
                        lambda profile: False if profile.id == uuid.UUID(unready.json()["id"])
                        else original_ready(profile))

    for account_id in (disabled.json()["id"], limited.json()["id"], unready.json()["id"]):
        response = await _task(client, auth, account_id=account_id)
        assert response.status_code == 503, response.text
        assert response.json()["code"] == "account_unavailable"
    assert (await client.get("/api/artcraft/tasks", headers=auth)).json()["items"] == []


async def test_limited_pinned_account_waits_without_account_or_emergency_fallback(client, auth, platform):
    import asyncio
    import time

    first = await client.post("/api/accounts", headers=auth,
                              json={"name": "pinned quota account", "engine": "codex"})
    second = await client.post("/api/accounts", headers=auth,
                               json={"name": "fallback quota account", "engine": "codex"})
    assert first.status_code == second.status_code == 201
    pinned_id = first.json()["id"]
    async with sessionmaker()() as db:
        pinned = await db.get(EngineAccount, uuid.UUID(pinned_id))
        alternative = await db.get(EngineAccount, uuid.UUID(second.json()["id"]))
        pinned.priority = 10
        alternative.priority = 0
        await db.commit()

    created = await _task(client, auth, account_id=pinned_id, prompt="CODEX_LIMIT")
    assert created.status_code == 201, created.text
    run_id = created.json()["rounds"][0]["id"]
    deadline = time.monotonic() + 10
    events = []
    while time.monotonic() < deadline:
        from helpers import run_events
        events = await run_events(client, auth, run_id)
        if any(event["type"] == "run.waiting" for event in events):
            break
        await asyncio.sleep(0.05)
    assert any(event["type"] == "account.limited" for event in events), events
    assert any(event["type"] == "run.waiting" for event in events), events
    assert not any(event["type"] == "engine.fallback" for event in events), events
    assert not any(event["type"] == "worker.routing" and event.get("data", {}).get("trigger") == "emergency"
                   for event in events), events
    from ttakkari.models import Run
    from ttakkari.runs.manager import _eligible_profiles
    async with sessionmaker()() as db:
        session = await db.get(ChatSession, uuid.UUID(created.json()["session_id"]))
        run = await db.get(Run, uuid.UUID(run_id))
        free, blocked = await _eligible_profiles(db, session, run, False)
        assert free == []
        assert {profile.id for profile, _until in blocked} == {uuid.UUID(pinned_id)}
        assert run.usage["artcraft"]["account_id"] == pinned_id


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


async def test_report_lookup_without_output_has_no_missing_file_error(client, auth, platform):
    from sqlalchemy import delete

    from ttakkari.db import sessionmaker
    from ttakkari.models import Artifact

    task = await _finish(client, auth, (await _task(
        client, auth, category="doc", template_id="write-report", prompt="Create a report and save it as DOCX."
    )).json())
    response = await client.post(
        f"/api/artcraft/tasks/{task['id']}/runs",
        json={"prompt": EXACT_ENGLISH_LOOKUP_PROMPT, "use_previous": True}, headers=auth
    )
    assert response.status_code == 202, response.text
    detail = await _finish(client, auth, response.json())
    assert detail["rounds"][-1]["use_previous"] is True
    lookup_run_id = detail["rounds"][-1]["id"]

    # The fake agent normally emits files for every prompt; remove only this test Run's artifacts to model a text-only answer.
    async with sessionmaker()() as db:
        await db.execute(delete(Artifact).where(
            Artifact.run_id == lookup_run_id, Artifact.source == "agent_output"
        ))
        await db.commit()

    summary = (await client.get("/api/artcraft/tasks", headers=auth)).json()["items"]
    summary = next(item for item in summary if item["id"] == task["id"])
    task_detail = (await client.get(f"/api/artcraft/tasks/{task['id']}", headers=auth)).json()
    assert summary["state"] == "done" and summary["error"] is None
    assert task_detail["rounds"][-1]["error"] is None


async def test_file_request_without_output_keeps_missing_file_error(client, auth, platform):
    from sqlalchemy import delete

    from ttakkari.db import sessionmaker
    from ttakkari.models import Artifact

    task = await _finish(client, auth, (await _task(
        client, auth, category="doc", template_id="write-report", prompt="Create a report and save it as DOCX."
    )).json())
    run_id = task["rounds"][-1]["id"]
    async with sessionmaker()() as db:
        await db.execute(delete(Artifact).where(Artifact.run_id == run_id, Artifact.source == "agent_output"))
        await db.commit()

    summary = (await client.get("/api/artcraft/tasks", headers=auth)).json()["items"]
    summary = next(item for item in summary if item["id"] == task["id"])
    task_detail = (await client.get(f"/api/artcraft/tasks/{task['id']}", headers=auth)).json()
    expected = "결과 파일 없음 (산출물이 생성되지 않았습니다)"
    assert summary["state"] == "done" and summary["error"] == expected
    assert task_detail["rounds"][-1]["error"] == expected


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


async def test_update_api_queries_real_db_and_rejects_active_creation_run(client, auth, platform):
    from ttakkari import artcraft

    artcraft.update_state(lambda state: state.pop("updates", None))
    task_response = await _task(client, auth, category="photo", prompt="SLEEP 제작 중", apps=["photocraft"])
    assert task_response.status_code == 201, task_response.text

    response = await client.post("/api/artcraft/apps/photocraft/update", json={"confirm": True}, headers=auth)

    assert response.status_code == 409, response.text
    assert response.json()["code"] == "busy"


async def test_create_commit_wins_same_app_update_race(client, auth, platform, monkeypatch):
    import asyncio

    from ttakkari import artcraft, artcraft_tasks

    artcraft.update_state(lambda state: state.pop("updates", None))
    entered_create = asyncio.Event()
    real_create_task = artcraft_tasks.create_task

    async def hold_create_admission(db, **kwargs):
        entered_create.set()
        return await real_create_task(db, **kwargs)

    monkeypatch.setattr(artcraft_tasks, "create_task", hold_create_admission)
    create_request = asyncio.create_task(
        _task(client, auth, category="photo", prompt="작업이 먼저 DB에 등록", apps=["photocraft"])
    )
    await asyncio.wait_for(entered_create.wait(), timeout=2)
    update_request = asyncio.create_task(
        client.post("/api/artcraft/apps/photocraft/update", json={"confirm": True}, headers=auth)
    )

    create_response, update_response = await asyncio.gather(create_request, update_request)

    assert create_response.status_code == 201, create_response.text
    assert update_response.status_code == 409, update_response.text
    assert update_response.json()["code"] == "busy"
    await client.post(f"/api/runs/{create_response.json()['rounds'][0]['id']}/cancel", headers=auth)


async def test_add_round_commit_wins_same_app_update_race(client, auth, platform, monkeypatch):
    import asyncio

    from ttakkari import artcraft, artcraft_tasks

    artcraft.update_state(lambda state: state.pop("updates", None))
    task = await _finish(
        client, auth, (await _task(client, auth, category="photo", prompt="기존 작업 완료")).json()
    )
    entered_round = asyncio.Event()
    release_round = asyncio.Event()
    real_add_round = artcraft_tasks.add_round

    async def hold_add_round(db, *args, **kwargs):
        entered_round.set()
        await release_round.wait()
        return await real_add_round(db, *args, **kwargs)

    monkeypatch.setattr(artcraft_tasks, "add_round", hold_add_round)
    round_request = asyncio.create_task(
        client.post(f"/api/artcraft/tasks/{task['id']}/runs", json={"prompt": "이어 만들기"}, headers=auth)
    )
    await asyncio.wait_for(entered_round.wait(), timeout=2)
    update_request = asyncio.create_task(
        client.post("/api/artcraft/apps/photocraft/update", json={"confirm": True}, headers=auth)
    )
    await asyncio.sleep(0.05)
    release_round.set()

    round_response, update_response = await asyncio.gather(round_request, update_request)

    assert round_response.status_code == 202, round_response.text
    assert update_response.status_code == 409, update_response.text
    assert update_response.json()["code"] == "busy"
    await client.post(f"/api/runs/{round_response.json()['rounds'][-1]['id']}/cancel", headers=auth)


async def test_update_admission_rechecks_persisted_state_after_waiting_for_lock(monkeypatch):
    import contextlib

    from ttakkari import artcraft

    artcraft.update_state(lambda state: state.pop("updates", None))

    lock_attempts = 0

    @contextlib.asynccontextmanager
    async def queued_update_wins(apps, *, wait=True):
        nonlocal lock_attempts
        lock_attempts += 1
        if lock_attempts == 1:
            raise artcraft.CoordinationLockBusy
        if lock_attempts == 2:
            artcraft.update_state(lambda state: state.setdefault("updates", {}).update({
                "photocraft": {"state": "queued", "current_job_id": "update-first"}
            }))
        raise artcraft.CoordinationLockBusy
        yield

    monkeypatch.setattr(artcraft, "coordination_lock", queued_update_wins)
    admitted = False

    with pytest.raises(artcraft.ArtcraftError) as exc:
        async with artcraft.task_admission_lock(["photocraft"]):
            admitted = True
    assert exc.value.status == 409 and exc.value.code == "app_updating"
    assert not admitted
    assert lock_attempts == 2
    artcraft.update_state(lambda state: state.pop("updates", None))


async def test_update_api_commit_wins_same_app_task_race(client, auth, platform, monkeypatch):
    import asyncio

    from ttakkari import artcraft, artcraft_update

    artcraft.update_state(lambda state: state.pop("updates", None))
    entered_update_check = asyncio.Event()
    release_update_check = asyncio.Event()
    entered_apply = asyncio.Event()
    release_apply = asyncio.Event()
    original_check_busy = artcraft_update.check_busy
    first_check = True

    async def pause_first_check(app, exclude_job_id=None, db=None):
        nonlocal first_check
        if first_check:
            first_check = False
            entered_update_check.set()
            await release_update_check.wait()
        return await original_check_busy(app, exclude_job_id=exclude_job_id, db=db)

    async def pause_apply(app, tag=None, confirm=False, current_job_id=None, db=None):
        entered_apply.set()
        await release_apply.wait()
        return {"version": "0.10.0", "verified_output": "verified"}

    async def ready_status(force=False):
        return ([{"app": "photocraft", "gui": {"running": False, "listening": False}}], None, "")

    monkeypatch.setattr(artcraft_update, "check_busy", pause_first_check)
    monkeypatch.setattr(artcraft_update, "apply_update", pause_apply)
    monkeypatch.setattr(artcraft, "raw_status", ready_status)

    update_request = asyncio.create_task(
        client.post("/api/artcraft/apps/photocraft/update", json={"confirm": True}, headers=auth)
    )
    await asyncio.wait_for(entered_update_check.wait(), timeout=2)
    task_request = asyncio.create_task(
        _task(client, auth, category="photo", prompt="업데이트 등록과 경합", apps=["photocraft"])
    )
    await asyncio.sleep(0.05)
    release_update_check.set()

    update_response = await asyncio.wait_for(update_request, timeout=3)
    assert update_response.status_code == 202, update_response.text
    job = artcraft.JOBS[update_response.json()["id"]]
    try:
        await asyncio.wait_for(entered_apply.wait(), timeout=3)
        task_response = await asyncio.wait_for(task_request, timeout=3)
        assert task_response.status_code == 409, task_response.text
        assert task_response.json()["code"] == "app_updating"
    finally:
        release_apply.set()
    await asyncio.wait_for(job.task, timeout=3)
    assert job.state == "succeeded"


async def test_update_and_rollback_api_jobs_use_fresh_db_sessions_and_hold_admission_lock(
    client, auth, platform, monkeypatch
):
    import asyncio
    import io
    import zipfile

    from ttakkari import artcraft, artcraft_tasks, artcraft_update

    artcraft.update_state(lambda state: state.pop("updates", None))
    task = await _finish(client, auth, (await _task(client, auth, category="photo", prompt="ART_OUT 완료")).json())

    old_bin = platform.base / "dist" / "cli" / "photocraft" / "0.9.0" / "photocraft-cli"
    old_bin.parent.mkdir(parents=True)
    old_bin.write_bytes(b"original-binary")
    old_bin.chmod(0o755)
    (platform.base / "bin" / "photocraft-cli").symlink_to(old_bin)

    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("photocraft-cli", b"updated-binary")
    entered_fetch = asyncio.Event()
    release_fetch = asyncio.Event()

    async def fetch_release(app, tag=None):
        entered_fetch.set()
        await release_fetch.wait()
        return {"version": "0.10.0", "zip_bytes": archive.getvalue(), "asset_name": "photocraft-cli-0.10.0-macos-universal.zip"}

    async def ready_status(force=False):
        return ([{"app": "photocraft", "gui": {"running": False, "listening": False}}], None, "")

    monkeypatch.setattr(artcraft_update, "fetch_release_assets", fetch_release)
    monkeypatch.setattr(artcraft_update, "verify_installation", lambda app, path: asyncio.sleep(0, result=(True, "verified")))
    monkeypatch.setattr(artcraft, "raw_status", ready_status)
    real_active_task_apps = artcraft_tasks.active_task_apps
    checked_sessions = []

    async def track_active_task_apps(db):
        checked_sessions.append(db)
        return await real_active_task_apps(db)

    monkeypatch.setattr(artcraft_tasks, "active_task_apps", track_active_task_apps)

    update_response = await client.post(
        "/api/artcraft/apps/photocraft/update", json={"confirm": True, "tag": "v0.10.0"}, headers=auth
    )
    assert update_response.status_code == 202, update_response.text
    update_job = artcraft.JOBS[update_response.json()["id"]]
    try:
        await asyncio.wait_for(entered_fetch.wait(), timeout=3)
    except TimeoutError:
        release_fetch.set()
        if update_job.task:
            update_job.task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await update_job.task
        pytest.fail(f"update runner stopped before fetch: state={update_job.state} summary={update_job.summary}")
    try:
        create_response = await _task(client, auth, category="photo", prompt="업데이트 중 신규 제작")
        round_response = await client.post(
            f"/api/artcraft/tasks/{task['id']}/runs", json={"prompt": "업데이트 중 추가 라운드"}, headers=auth
        )
        other_app_response = await _task(
            client, auth, category="photo", prompt="다른 앱 제작은 허용", apps=["lightcraft"]
        )
        assert create_response.status_code == 409 and create_response.json()["code"] == "app_updating"
        assert round_response.status_code == 409 and round_response.json()["code"] == "app_updating"
        assert other_app_response.status_code == 201, other_app_response.text
    finally:
        release_fetch.set()
    await asyncio.wait_for(update_job.task, timeout=5)
    assert update_job.state == "succeeded"

    rollback_response = await client.post(
        "/api/artcraft/apps/photocraft/update/rollback", json={"confirm": True}, headers=auth
    )
    assert rollback_response.status_code == 202, rollback_response.text
    rollback_job = artcraft.JOBS[rollback_response.json()["id"]]
    await asyncio.wait_for(rollback_job.task, timeout=5)
    assert rollback_job.state == "succeeded"
    assert len(checked_sessions) >= 4
    assert checked_sessions[0] is not checked_sessions[1]
    assert checked_sessions[2] is not checked_sessions[3]


async def test_foreign_generation_persisted_update_blocks_create_and_add_round(client, auth, platform):
    import os

    from ttakkari import artcraft

    artcraft.update_state(lambda state: state.pop("updates", None))
    task = await _finish(client, auth, (await _task(client, auth, category="photo", prompt="ART_OUT 제작 완료")).json())
    artcraft.update_state(lambda state: state.setdefault("updates", {}).update({
        "photocraft": {
            "state": "running",
            "current_job_id": "other-generation-job",
            "owner_pid": os.getpid(),
            "owner_generation": "other-generation",
        }
    }))

    try:
        create_response = await _task(client, auth, category="photo", prompt="새 제작")
        round_response = await client.post(
            f"/api/artcraft/tasks/{task['id']}/runs", json={"prompt": "추가 라운드"}, headers=auth
        )

        assert create_response.status_code == 409, create_response.text
        assert create_response.json()["code"] == "app_updating"
        assert round_response.status_code == 409, round_response.text
        assert round_response.json()["code"] == "app_updating"
    finally:
        artcraft.update_state(lambda state: state.pop("updates", None))


async def test_coordination_lock_serializes_and_releases_after_runner_cancellation():
    import asyncio

    from ttakkari.artcraft import coordination_lock

    holder_entered = asyncio.Event()
    waiter_started = asyncio.Event()
    waiter_entered = asyncio.Event()

    async def hold_until_cancelled():
        async with coordination_lock():
            holder_entered.set()
            await asyncio.Event().wait()

    async def wait_for_lock():
        waiter_started.set()
        async with coordination_lock():
            waiter_entered.set()

    holder = asyncio.create_task(hold_until_cancelled())
    await holder_entered.wait()
    waiter = asyncio.create_task(wait_for_lock())
    await waiter_started.wait()
    assert not waiter_entered.is_set()

    holder.cancel()
    with pytest.raises(asyncio.CancelledError):
        await holder
    await asyncio.wait_for(waiter_entered.wait(), timeout=1)
    await waiter


async def test_coordination_lock_is_shared_between_process_generations():
    import asyncio

    from ttakkari import artcraft

    context = multiprocessing.get_context("spawn")
    contended = context.Event()
    acquired = context.Event()
    lock_path = str(artcraft._state_path().with_name("coordination-photocraft.lock"))
    process = context.Process(target=_compete_for_file_lock, args=(lock_path, contended, acquired))
    async with artcraft.coordination_lock(["photocraft"]):
        process.start()
        assert await asyncio.wait_for(asyncio.to_thread(contended.wait, 3), timeout=4)
        assert not acquired.is_set()
    assert await asyncio.wait_for(asyncio.to_thread(acquired.wait, 3), timeout=4)
    await asyncio.to_thread(process.join, 3)
    assert process.exitcode == 0


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


def test_sweep_outputs_filters_zero_byte_empty_and_sensitive_files(tmp_path: Path):
    """0바이트 빈 파일과 민감 파일(.env, .git 등)은 결과물 outbox 로 복사되지 않는다."""
    from ttakkari import artcraft_tasks
    from ttakkari.artcraft_tasks import RunPlan
    from ttakkari.models import Run, utcnow

    out_dir = tmp_path / "out"
    out_dir.mkdir()
    outbox = tmp_path / "outbox"
    outbox.mkdir()
    run = Run(started_at=utcnow(), created_at=utcnow())

    (out_dir / "empty.txt").write_bytes(b"")
    (out_dir / ".env").write_text("SECRET=1")
    (out_dir / ".DS_Store").write_bytes(b"\x00")
    (out_dir / "valid.png").write_bytes(b"\x89PNG real image")

    plan = RunPlan(prompt="", note="", mcp_servers={}, out_dir=out_dir)

    copied = artcraft_tasks.sweep_outputs(plan, run, outbox)
    assert copied == 1
    assert (outbox / "valid.png").exists()
    assert not (outbox / "empty.txt").exists()
    assert not (outbox / ".env").exists()
    assert not (outbox / ".DS_Store").exists()


def test_catalog_cli_recipes_are_all_fully_qualified():
    """모든 카테고리의 cli 요령 내 실행 명령어가 단독 파편 없이 온전한 artcraft run <app> 형태여야 한다."""
    import re
    cmd_pattern = re.compile(r"`([^`]+)`")
    for cat in catalog.CATEGORIES:
        for line in cat.cli:
            commands = cmd_pattern.findall(line)
            for cmd in commands:
                cmd_strip = cmd.strip()
                if any(cmd_strip.startswith(p) for p in ("run ", "commands", "info ", "convert ", "render ", "cat ", "eval ", "sample ", "combine ", "extract ", "split ", "edit ")):
                    pytest.fail(f"카테고리 {cat.id}에 단독 명령어 파편 존재: `{cmd_strip}` in {line}")


def test_check_app_not_updating_rejects_active_updating_job(monkeypatch):
    """활성 업데이트/롤백 Job 이 있는 앱은 작업 생성 시 409 app_updating 으로 차단된다."""
    from ttakkari import artcraft, artcraft_tasks
    from ttakkari.artcraft import Job

    job = Job(id="up1", kind="update", action="update photocraft", apps=["photocraft"], state="running")
    monkeypatch.setattr(artcraft, "JOBS", {"up1": job})

    with pytest.raises(artcraft.ArtcraftError) as exc_info:
        artcraft_tasks.check_app_not_updating(["photocraft"])
    assert exc_info.value.status == 409 and exc_info.value.code == "app_updating"


@pytest.mark.parametrize("template_id", [None, "write-report"])
def test_follow_up_report_lookup_does_not_require_a_file(template_id):
    from ttakkari.artcraft_tasks import _expects_output_file
    from ttakkari.models import ArtcraftTask, Run

    task = ArtcraftTask(template_id=template_id)
    run = Run(prompt=("Without editing or creating a file, summarize the revised report's verification limits "
                      "and next steps in English. Use only the latest saved report and explicitly say when a "
                      "value is unmeasured."))

    assert _expects_output_file(task, run) is False


@pytest.mark.parametrize("template_id", [None, "write-report"])
@pytest.mark.parametrize("prompt, expected", [
    ("Without creating a file, summarize the saved report.", False),
    ("Do not create a file; explain the verification limits.", False),
    ("Do not create a file for the lookup, but create a revised report and save as PDF.", True),
    ("Create a report and summarize its verification limits.", True),
    ("Edit the report and save a new version.", True),
    ("Export the report as a PDF file.", True),
    ("보고서의 검증 한계를 알려 주세요.", False),
    ("보고서를 수정하고 새 파일로 저장해 주세요.", True),
    ("수정본의 검증 한계를 알려 주세요.", False),
    ("수정된 보고서의 검증 한계를 알려 주세요.", False),
    ("편집본의 변경 사항을 알려 주세요.", False),
    ("편집된 보고서의 요약을 알려 주세요.", False),
    ("보고서를 수정하고 변경 사항을 알려 주세요.", True),
    ("보고서를 편집하고 변경 사항을 알려 주세요.", True),
    ("Do not create or save a file; summarize the report's verification limits.", False),
    ("Do not create or save a file for this lookup; summarize the report's verification limits, but save the revised report as PDF.", True),
    (EXACT_ENGLISH_LOOKUP_PROMPT, False),
    ("Do not create, edit, modify, convert, or export any file; read the saved file and tell me the amount.", False),
    ("Do not create, edit, modify, convert, or export any file, but create an edited version after the lookup.", True),
    ("파일을 만들지 않고 보고서의 검증 한계를 요약해 주세요.", False),
])
def test_output_requirement_distinguishes_lookup_from_file_creation(template_id, prompt, expected):
    from ttakkari.artcraft_tasks import _expects_output_file
    from ttakkari.models import ArtcraftTask, Run

    assert _expects_output_file(ArtcraftTask(template_id=template_id), Run(prompt=prompt)) is expected


def test_korean_negative_file_request_remains_a_lookup():
    from ttakkari.artcraft_tasks import _expects_output_file
    from ttakkari.models import ArtcraftTask, Run

    assert _expects_output_file(ArtcraftTask(template_id="write-report"), Run(
        prompt="파일을 저장하지 말고 보고서의 검증 한계를 알려 주세요."
    )) is False
