"""ADR 0006: 엔진 프로필 폴백, 오케스트레이터 도구 API, 결과 보고 루프, MCP 서버."""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import time
from uuid import UUID

import pytest
from conftest import AGENT_LOG, agent_calls
from helpers import run_events, run_to_end, start_run, wait_event, wait_run
from sqlalchemy import select

from ttakkari.db import sessionmaker
from ttakkari.models import AppSetting
from ttakkari.orchestrator.runtime import issue_token
from ttakkari.runs import profiles
from ttakkari.runs.manager import manager


async def _session(client, auth, make_dir, **body) -> dict:
    root = make_dir()
    ws = (await client.post("/api/workspaces", json={"name": root.name, "root_path": str(root)}, headers=auth)).json()
    r = await client.post("/api/sessions", json={"workspace_id": ws["id"], **body}, headers=auth)
    assert r.status_code == 201, r.text
    return r.json()


def _orch(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _wait(pred, timeout: float = 20.0, what: str = "조건"):
    deadline = time.monotonic() + timeout
    while True:
        v = await pred()
        if v:
            return v
        assert time.monotonic() < deadline, f"{what} 이(가) {timeout}초 안에 충족되지 않음"
        await asyncio.sleep(0.1)


async def _wait_status(client, auth, run_id: str, status: str, timeout: float = 20.0) -> dict:
    async def find():
        r = (await client.get(f"/api/runs/{run_id}", headers=auth)).json()
        return r if r["status"] == status else None

    return await _wait(find, timeout, f"상태 {status}")


@pytest.fixture
def push_calls(monkeypatch):
    calls: list[tuple] = []

    async def fake_notify(kind, title, body="", **kw):
        calls.append((kind, title, body, kw))
        return {"sent": 0, "removed": 0, "failed": 0}

    monkeypatch.setattr("ttakkari.runs.manager.notify", fake_notify)
    return calls


@pytest.fixture
def limit_lock():
    """락 파일이 있는 동안 가짜 엔진이 한도를 낸다(LIMIT_LOCK 지시). 내용은 초기화 시각."""
    lock = AGENT_LOG.with_name(AGENT_LOG.name + ".lock")
    lock.write_text(str(int(time.time()) + 3))
    yield lock
    lock.unlink(missing_ok=True)


# ---- 엔진 프로필 폴백

async def test_auto_engine_falls_back_to_codex_on_usage_limit_and_keeps_using_it(client, auth, make_dir):
    sess = await _session(client, auth, make_dir, kind="orchestrator")
    assert sess["engine"] == "auto" and sess["kind"] == "orchestrator"
    run = await run_to_end(client, auth, sess["id"], "first CLAUDE_LIMIT")
    assert run["status"] == "succeeded", run
    assert run["engine"] == "codex" and run["profile"] == "codex" and run["result_text"] == "codex done"
    fb = [e for e in await run_events(client, auth, run["id"]) if e["type"] == "engine.fallback"]
    assert len(fb) == 1 and fb[0]["payload"]["from"] == "claude" and fb[0]["payload"]["to"] == "codex"
    assert fb[0]["payload"]["reason"] == "usage_limit"

    # claude 프로필이 쉬는 동안 다음 지시는 곧장 codex 로, 같은 codex 대화를 이어간다.
    before = len(agent_calls())
    second = await run_to_end(client, auth, sess["id"], "second")
    assert second["status"] == "succeeded" and second["profile"] == "codex"
    calls = agent_calls()[before:]
    assert [c["engine"] for c in calls] == ["codex"]
    assert calls[0]["argv"][1] == "resume"


async def test_real_claude_session_limit_falls_back_to_codex_with_handoff(client, auth, make_dir):
    sess = await _session(client, auth, make_dir, kind="orchestrator")
    await client.post("/api/assistant/memories", json={"content": "project alpha deploy requires a manual review"}, headers=auth)
    await run_to_end(client, auth, sess["id"], "remember project alpha")
    before = len(agent_calls())
    run = await run_to_end(client, auth, sess["id"], "continue project alpha CLAUDE_SESSION_LIMIT")
    assert run["status"] == "succeeded" and run["profile"] == "codex"
    calls = agent_calls()[before:]
    assert [c["engine"] for c in calls] == ["claude", "codex"]
    prompt = calls[-1]["argv"][calls[-1]["argv"].index("--") + 1]
    assert "remember project alpha" in prompt
    assert "continue project alpha" in prompt
    assert "Previous account" in prompt and "session limit" in prompt
    assert "Existing worker was already created" in prompt
    assert "project alpha deploy requires a manual review" in prompt
    assert "work" in prompt
    fb = [e for e in await run_events(client, auth, run["id"]) if e["type"] == "engine.fallback"]
    assert len(fb) == 1 and fb[0]["payload"]["reason"] == "usage_limit"


async def test_real_claude_session_limit_falls_back_to_second_claude(client, auth, make_dir):
    d = make_dir()
    a = (await client.post("/api/accounts", json={"name": "claude-2", "engine": "claude",
                                                    "config_dir": str(d)}, headers=auth)).json()
    accs = (await client.get("/api/accounts", headers=auth)).json()
    ids = [accs[0]["id"], a["id"], accs[1]["id"]]
    assert (await client.post("/api/accounts/reorder", json={"ids": ids}, headers=auth)).status_code == 200
    sess = await _session(client, auth, make_dir, kind="orchestrator")
    await run_to_end(client, auth, sess["id"], "project beta stage 1")
    before = len(agent_calls())
    run = await run_to_end(client, auth, sess["id"], "CLAUDE_SESSION_LIMIT")
    assert run["status"] == "succeeded" and run["profile"] == "claude-2"
    calls = agent_calls()[before:]
    assert [c["engine"] for c in calls] == ["claude", "claude"]
    assert "--resume" in calls[0]["argv"] and "--resume" not in calls[1]["argv"]
    prompt = calls[-1]["argv"][calls[-1]["argv"].index("-p") + 1]
    assert "Previous account" in prompt and "session limit" in prompt
    assert "project beta stage 1" in prompt
    assert calls[-1]["account_dir"] == str(d)


async def test_all_limited_accounts_wait_instead_of_failing_and_are_not_retried(client, auth, make_dir):
    sess = await _session(client, auth, make_dir, kind="orchestrator")
    first = await start_run(client, auth, sess["id"], "CLAUDE_LIMIT CODEX_LIMIT")
    waiting = await wait_event(client, auth, first["id"], "run.waiting")
    assert waiting["payload"]["until"] and {a["name"] for a in waiting["payload"]["accounts"]} == {"claude", "codex"}
    run = (await client.get(f"/api/runs/{first['id']}", headers=auth)).json()
    assert run["status"] == "queued" and run["error"] is None
    before = len(agent_calls())
    # 대기 중인 Run 뒤에 선다. 계정을 다시 두드리지 않고, 같은 대기를 다시 알리지도 않는다.
    second = await start_run(client, auth, sess["id"], "new request")
    await manager.dispatch(UUID(sess["id"]))
    await asyncio.sleep(0.3)
    assert len(agent_calls()) == before
    assert (await client.get(f"/api/runs/{second['id']}", headers=auth)).json()["status"] == "queued"
    waits = [e for e in await run_events(client, auth, first["id"]) if e["type"] == "run.waiting"]
    assert len(waits) == 1


async def test_first_use_of_profile_gets_previous_conversation(client, auth, make_dir):
    sess = await _session(client, auth, make_dir, kind="orchestrator")
    assert (await run_to_end(client, auth, sess["id"], "remember banana"))["profile"] == "claude"
    before = len(agent_calls())
    run = await run_to_end(client, auth, sess["id"], "next CLAUDE_LIMIT")
    assert run["profile"] == "codex"
    codex = next(c for c in agent_calls()[before:] if c["engine"] == "codex")
    prompt = codex["argv"][codex["argv"].index("--") + 1]
    assert "[이전 대화" in prompt and "remember banana" in prompt and "next CLAUDE_LIMIT" in prompt


async def test_explicit_engine_does_not_switch_engines(client, auth, make_dir):
    sess = await _session(client, auth, make_dir, engine="claude")
    run = await start_run(client, auth, sess["id"], "CLAUDE_LIMIT")
    await wait_event(client, auth, run["id"], "run.waiting")
    assert {c["engine"] for c in agent_calls()} == {"claude"}


async def test_all_profiles_limited_mid_run_pauses_with_progress_and_reset_time(client, auth, make_dir):
    sess = await _session(client, auth, make_dir, kind="orchestrator")
    run = await start_run(client, auth, sess["id"], "CLAUDE_LIMIT CODEX_LIMIT")
    waiting = await wait_event(client, auth, run["id"], "run.waiting")
    events = await run_events(client, auth, run["id"])
    types = [e["type"] for e in events]
    assert types.count("account.limited") == 2 and types.count("engine.fallback") == 1
    assert "run.finished" not in types
    limited = [e["payload"] for e in events if e["type"] == "account.limited"]
    assert [x["account"] for x in limited] == ["claude", "codex"]
    assert waiting["payload"]["until"]
    assert (await client.get(f"/api/runs/{run['id']}", headers=auth)).json()["status"] == "queued"
    # 계정 목록에는 초기화까지 남은 시간이 보인다.
    assert all(a["cooldown_seconds"] > 0 for a in (await client.get("/api/accounts", headers=auth)).json())


async def test_non_limit_failure_does_not_fall_back(client, auth, make_dir):
    sess = await _session(client, auth, make_dir, kind="orchestrator")
    run = await run_to_end(client, auth, sess["id"], "FAIL")
    assert run["status"] == "failed" and run["profile"] == "claude"


# ---- 오케스트레이터 Run 설정

async def test_orchestrator_run_gets_mcp_server_and_role_note(client, auth, make_dir):
    sess = await _session(client, auth, make_dir, kind="orchestrator")
    await run_to_end(client, auth, sess["id"], "hi")
    argv = agent_calls()[-1]["argv"]
    cfg = json.loads(argv[argv.index("--mcp-config") + 1])
    srv = cfg["mcpServers"]["ttakkari"]
    assert srv["args"] == ["-m", "ttakkari.orchestrator.mcp_server"] and srv["env"]["TTK_ORCH_TOKEN"]
    assert srv["env"]["TTK_ROLE"] == "orchestrator"
    assert "main orchestrator" in argv[argv.index("--append-system-prompt") + 1]


async def test_chat_session_gets_agent_tools_but_not_worker_tools(client, auth, make_dir):
    sess = await _session(client, auth, make_dir)
    await run_to_end(client, auth, sess["id"], "hi")
    argv = agent_calls()[-1]["argv"]
    srv = json.loads(argv[argv.index("--mcp-config") + 1])["mcpServers"]["ttakkari"]
    assert srv["env"]["TTK_ROLE"] == "agent"
    note = argv[argv.index("--append-system-prompt") + 1]
    assert "recall" in note and "main orchestrator" not in note


async def test_codex_orchestrator_gets_mcp_server_via_config_flags(client, auth, make_dir):
    sess = await _session(client, auth, make_dir, kind="orchestrator")
    await run_to_end(client, auth, sess["id"], "CLAUDE_LIMIT")
    argv = next(c for c in agent_calls() if c["engine"] == "codex")["argv"]
    flags = [argv[i + 1] for i, a in enumerate(argv) if a == "-c"]
    assert any(f.startswith("mcp_servers.ttakkari.command=") for f in flags)
    assert any(f.startswith("mcp_servers.ttakkari.env={TTK_ORCH_TOKEN=") for f in flags)


# ---- 도구 API + 결과 보고

async def test_spawn_worker_runs_and_reports_back_to_orchestrator(client, auth, make_dir):
    orch = await _session(client, auth, make_dir, kind="orchestrator")
    h = _orch(issue_token(orch["id"]))
    r = await client.post("/api/orchestrator/workers", json={"title": "보고서", "prompt": "write report", "engine": "claude"}, headers=h)
    assert r.status_code == 201, r.text
    wid = r.json()["worker_id"]

    worker = (await client.get(f"/api/sessions/{wid}", headers=auth)).json()
    assert worker["kind"] == "worker" and worker["parent_id"] == orch["id"] and worker["engine"] == "claude"
    wrun = (await client.get(f"/api/sessions/{wid}/runs", headers=auth)).json()[0]
    assert wrun["origin"] == "orchestrator"
    assert (await wait_run(client, auth, wrun["id"]))["status"] == "succeeded"

    async def report():
        runs = (await client.get(f"/api/sessions/{orch['id']}/runs", headers=auth)).json()
        return next((x for x in runs if x["origin"] == "report"), None)
    rep = await _wait(report, what="결과 보고 Run")
    assert rep["prompt"].startswith("[작업 보고] 작업 세션 '보고서'") and f"worker_id={wid}" in rep["prompt"]
    assert "결과:\ndone" in rep["prompt"]
    assert (await wait_run(client, auth, rep["id"]))["status"] == "succeeded"

    detail = (await client.get(f"/api/orchestrator/workers/{wid}", headers=h)).json()
    assert detail["status"] == "succeeded" and detail["last_result"] == "done" and detail["artifacts"]
    listed = (await client.get("/api/orchestrator/workers", headers=h)).json()
    assert [w["worker_id"] for w in listed] == [wid]


async def test_reports_queued_behind_busy_orchestrator_are_merged(client, auth, make_dir):
    orch = await _session(client, auth, make_dir, kind="orchestrator")
    h = _orch(issue_token(orch["id"]))
    busy = (await client.post(f"/api/sessions/{orch['id']}/runs", json={"prompt": "SLEEP"}, headers=auth)).json()
    ids = []
    for t in ("a", "b"):
        ids.append((await client.post("/api/orchestrator/workers", json={"title": t, "prompt": "go"}, headers=h)).json()["worker_id"])
    for wid in ids:
        wrun = (await client.get(f"/api/sessions/{wid}/runs", headers=auth)).json()[0]
        await wait_run(client, auth, wrun["id"])
    runs = (await client.get(f"/api/sessions/{orch['id']}/runs", headers=auth)).json()
    reports = [x for x in runs if x["origin"] == "report"]
    assert len(reports) == 1 and reports[0]["status"] == "queued"
    assert "'a'" in reports[0]["prompt"] and "'b'" in reports[0]["prompt"]
    await client.post(f"/api/runs/{busy['id']}/cancel", headers=auth)


async def test_user_message_to_worker_also_reports(client, auth, make_dir):
    orch = await _session(client, auth, make_dir, kind="orchestrator")
    h = _orch(issue_token(orch["id"]))
    wid = (await client.post("/api/orchestrator/workers", json={"title": "w", "prompt": "go"}, headers=h)).json()["worker_id"]
    first = (await client.get(f"/api/sessions/{wid}/runs", headers=auth)).json()[0]
    await wait_run(client, auth, first["id"])
    direct = await run_to_end(client, auth, wid, "user follow-up")
    assert direct["origin"] == "user"

    async def two_reports():
        runs = (await client.get(f"/api/sessions/{orch['id']}/runs", headers=auth)).json()
        reps = [x for x in runs if x["origin"] == "report"]
        return reps if sum(x["prompt"].count("[작업 보고]") for x in reps) >= 2 else None
    await _wait(two_reports, what="보고 2건")


async def test_message_and_stop_worker(client, auth, make_dir):
    orch = await _session(client, auth, make_dir, kind="orchestrator")
    h = _orch(issue_token(orch["id"]))
    wid = (await client.post("/api/orchestrator/workers", json={"title": "w", "prompt": "SLEEP"}, headers=h)).json()["worker_id"]
    r = await client.post(f"/api/orchestrator/workers/{wid}/messages", json={"prompt": "after"}, headers=h)
    assert r.status_code == 202
    runs = (await client.get(f"/api/sessions/{wid}/runs", headers=auth)).json()
    assert [x["status"] for x in runs][-1] == "queued"
    stopped = (await client.post(f"/api/orchestrator/workers/{wid}/stop", headers=h)).json()
    assert stopped["status"] in {"cancelled", "idle"}
    for x in (await client.get(f"/api/sessions/{wid}/runs", headers=auth)).json():
        assert (await wait_run(client, auth, x["id"]))["status"] == "cancelled"


async def test_orchestrator_token_is_scoped(client, auth, make_dir):
    a = await _session(client, auth, make_dir, kind="orchestrator")
    b = await _session(client, auth, make_dir, kind="orchestrator")
    chat = await _session(client, auth, make_dir)
    wid = (await client.post("/api/orchestrator/workers", json={"title": "w", "prompt": "go"},
                             headers=_orch(issue_token(a["id"])))).json()["worker_id"]
    hb = _orch(issue_token(b["id"]))
    assert (await client.get(f"/api/orchestrator/workers/{wid}", headers=hb)).status_code == 404
    assert (await client.post(f"/api/orchestrator/workers/{wid}/stop", headers=hb)).status_code == 404
    assert (await client.get("/api/orchestrator/workers", headers=hb)).json() == []
    # 사용자 토큰으로는 도구 API 를, 오케스트레이터 토큰으로는 사용자 API 를 못 쓴다.
    assert (await client.get("/api/orchestrator/workers", headers=auth)).status_code == 401
    assert (await client.get("/api/sessions", headers=hb)).status_code == 401
    # 일반 대화 세션에 묶인 토큰은 거절한다.
    assert (await client.get("/api/orchestrator/workers", headers=_orch(issue_token(chat["id"])))).status_code == 403


async def test_sidebar_hides_workers_and_parent_filter_lists_them(client, auth, make_dir):
    orch = await _session(client, auth, make_dir, kind="orchestrator")
    wid = (await client.post("/api/orchestrator/workers", json={"title": "w", "prompt": "go"},
                             headers=_orch(issue_token(orch["id"])))).json()["worker_id"]
    side = [s["id"] for s in (await client.get("/api/sessions", headers=auth)).json()]
    assert orch["id"] in side and wid not in side
    kids = (await client.get("/api/sessions", params={"parent_id": orch["id"]}, headers=auth)).json()
    assert [s["id"] for s in kids] == [wid]


# ---- MCP 서버(stdio 프로토콜)

AGENT_TOOLS = {"library_catalog", "recall", "remember", "get_entity", "describe_entity", "link", "forget", "list_artifacts", "share_file",
               "set_artifact_area", "server_resources", "notify_user", "list_sessions", "read_session",
               "list_memo_categories", "list_memo_tags", "list_memos", "search_memos", "get_memo", "create_memo", "update_memo", "delete_memo",
               "list_events", "create_event", "update_event", "delete_event", "list_meetings", "get_meeting", "search_meeting_items"}
WORKER_TOOLS = {"spawn_worker", "message_worker", "list_workers", "get_worker", "stop_worker", "account_usage",
                "my_context", "reset_my_context"}


def _mcp_tools(role: str) -> tuple[set[str], dict]:
    env = {**os.environ, "TTK_ORCH_TOKEN": "x", "TTK_API_URL": "http://127.0.0.1:9", "TTK_ROLE": role}
    msgs = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize",
         "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "0"}}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "recall", "arguments": {"query": "x"}}},
    ]
    p = subprocess.run([sys.executable, "-m", "ttakkari.orchestrator.mcp_server"], env=env, text=True, timeout=60,
                       input="".join(json.dumps(m) + "\n" for m in msgs), capture_output=True, check=False)
    replies = {r["id"]: r for r in (json.loads(x) for x in p.stdout.splitlines() if x.strip()) if "id" in r}
    return {t["name"] for t in replies[2]["result"]["tools"]}, replies


def test_mcp_server_tools_by_role():
    agent, replies = _mcp_tools("agent")
    assert agent == AGENT_TOOLS and 3 in replies
    orch, _ = _mcp_tools("orchestrator")
    assert orch == AGENT_TOOLS | WORKER_TOOLS


@pytest.mark.parametrize("role", ["agent", "orchestrator"])
def test_mcp_queries_are_marked_read_only_without_marking_writes(role):
    _, replies = _mcp_tools(role)
    queries = {
        "library_catalog", "recall", "get_entity", "list_artifacts", "list_sessions", "read_session",
        "server_resources", "list_memo_categories", "list_memo_tags", "list_memos", "search_memos", "get_memo",
        "list_events", "list_meetings", "get_meeting", "search_meeting_items",
        "account_usage", "my_context", "list_workers", "get_worker",
    }
    for tool in replies[2]["result"]["tools"]:
        annotations = tool.get("annotations") or {}
        if tool["name"] in queries:
            assert annotations.get("readOnlyHint") is True, tool["name"]
            assert annotations.get("destructiveHint") is False, tool["name"]
            assert annotations.get("openWorldHint") is False, tool["name"]
        else:
            assert annotations.get("readOnlyHint") is not True, tool["name"]


def test_codex_auto_approves_only_ttakkari_queries(make_dir):
    from ttakkari.agents.base import LaunchSpec
    from ttakkari.agents.codex import CodexAdapter

    root = make_dir()
    spec = LaunchSpec(cwd=root, prompt="read previous conversations", engine_session_id=None,
                      model=None, effort=None, outbox=root, write_roots=[root], system_note="",
                      mcp_servers={"ttakkari": {"command": "python", "args": [], "env": {}},
                                   "other": {"command": "python", "args": [], "env": {}}})
    argv = CodexAdapter().build_command(spec, {})
    flags = [argv[i + 1] for i, arg in enumerate(argv[:-1]) if arg == "-c"]
    approvals = {flag.split("=", 1)[0]: flag.split("=", 1)[1] for flag in flags if ".approval_mode=" in flag}
    assert approvals['mcp_servers.ttakkari.tools.list_sessions.approval_mode'] == '"approve"'
    assert approvals['mcp_servers.ttakkari.tools.read_session.approval_mode'] == '"approve"'
    assert approvals['mcp_servers.ttakkari.tools.recall.approval_mode'] == '"approve"'
    assert not any(key.startswith("mcp_servers.other.") for key in approvals)
    for name in ("remember", "forget", "delete_memo", "notify_user", "spawn_worker", "stop_worker"):
        assert f"mcp_servers.ttakkari.tools.{name}.approval_mode" not in approvals
    assert 'approval_policy="never"' in flags
    assert 'sandbox_mode="workspace-write"' in flags


async def test_worker_list_has_summary_for_tabs(client, auth, make_dir):
    orch = await _session(client, auth, make_dir, kind="orchestrator")
    wid = (await client.post("/api/orchestrator/workers", json={"title": "w", "prompt": "go", "engine": "claude"},
                             headers=_orch(issue_token(orch["id"])))).json()["worker_id"]
    wrun = (await client.get(f"/api/sessions/{wid}/runs", headers=auth)).json()[0]
    await wait_run(client, auth, wrun["id"])
    tab = (await client.get("/api/sessions", params={"parent_id": orch["id"]}, headers=auth)).json()[0]
    assert tab["last_run_status"] == "succeeded" and tab["last_result"] == "done" and tab["last_run_profile"] == "claude"


# ---- 사용량 한도 대기·재개

async def test_waiting_run_resumes_same_run_after_reset_with_progress_handoff(client, auth, make_dir, limit_lock, push_calls, monkeypatch):
    monkeypatch.setattr(profiles, "RESET_BUFFER", 0)
    sess = await _session(client, auth, make_dir, kind="orchestrator")
    run = await start_run(client, auth, sess["id"], "LIMIT_LOCK build the report")
    waiting = await wait_event(client, auth, run["id"], "run.waiting")
    assert "progress marker alpha" in waiting["payload"]["handoff"]
    # 사용자에게 한 번 알린다(재개 시각 포함).
    assert len(push_calls) == 1
    kind, title, body, kw = push_calls[0]
    assert kind.value == "push.run_waiting" and "사용량 한도" in title and "이어서 진행" in body
    assert kw["url"] == f"/chat/{sess['id']}"

    limit_lock.unlink()  # 한도가 풀린다. 초기화 시각(약 3초)이 지나면 같은 Run 이 이어서 돈다.
    done = await _wait_status(client, auth, run["id"], "succeeded")
    assert done["id"] == run["id"] and done["profile"] == "claude" and done["started_at"]
    events = await run_events(client, auth, run["id"])
    types = [e["type"] for e in events]
    assert types.count("run.waiting") == 1 and types.count("run.resumed") == 1
    assert types.index("run.waiting") < types.index("run.resumed") < types.index("run.finished")
    # 시작할 때 한 번만 남기는 체크포인트·시작 시각을 다시 만들지 않는다.
    assert types.count("checkpoint") <= 1
    last = agent_calls()[-1]
    assert last["engine"] == "claude"
    prompt = last["argv"][last["argv"].index("-p") + 1]
    assert "Previous account handoff" in prompt and "progress marker alpha" in prompt
    assert "LIMIT_LOCK build the report" in prompt
    assert len(push_calls) == 1


async def test_cancel_waiting_run_disarms_wake(client, auth, make_dir, limit_lock, push_calls):
    sess = await _session(client, auth, make_dir, kind="orchestrator")
    run = await start_run(client, auth, sess["id"], "LIMIT_LOCK cancel me")
    await wait_event(client, auth, run["id"], "run.waiting")
    assert UUID(run["id"]) in manager._waiting
    r = await client.post(f"/api/runs/{run['id']}/cancel", headers=auth)
    assert r.status_code == 200 and r.json()["status"] == "cancelled"
    assert UUID(run["id"]) not in manager._waiting and UUID(run["id"]) not in manager._wakers


async def test_clearing_account_limits_releases_waiting_run_immediately(client, auth, make_dir, limit_lock, push_calls):
    limit_lock.write_text(str(int(time.time()) + 3600))
    sess = await _session(client, auth, make_dir, kind="orchestrator")
    run = await start_run(client, auth, sess["id"], "LIMIT_LOCK wait an hour")
    await wait_event(client, auth, run["id"], "run.waiting")
    limit_lock.unlink()
    for acc in (await client.get("/api/accounts", headers=auth)).json():
        assert (await client.post(f"/api/accounts/{acc['id']}/cooldown/clear", headers=auth)).status_code == 200
    done = await _wait_status(client, auth, run["id"], "succeeded", timeout=10)
    assert done["profile"] == "claude"


async def test_still_limited_after_release_waits_again_without_new_notification(client, auth, make_dir, limit_lock, push_calls):
    limit_lock.write_text(str(int(time.time()) + 3600))
    sess = await _session(client, auth, make_dir, kind="orchestrator")
    run = await start_run(client, auth, sess["id"], "LIMIT_LOCK still limited")
    await wait_event(client, auth, run["id"], "run.waiting")
    before = len(agent_calls())
    await manager.release_waiting()  # 계정 쿨다운이 그대로라 호출 없이 다시 대기한다
    await asyncio.sleep(0.3)
    assert len(agent_calls()) == before and UUID(run["id"]) in manager._waiting
    assert len([e for e in await run_events(client, auth, run["id"]) if e["type"] == "run.waiting"]) == 1
    assert len(push_calls) == 1


async def test_repeated_limit_waits_stop_at_max(client, auth, make_dir, limit_lock, push_calls, monkeypatch):
    from ttakkari.config import get_settings

    monkeypatch.setattr(profiles, "RESET_BUFFER", 0)
    monkeypatch.setattr(get_settings(), "max_limit_waits", 1)
    limit_lock.write_text(str(int(time.time()) + 2))
    sess = await _session(client, auth, make_dir, kind="orchestrator")
    run = await start_run(client, auth, sess["id"], "LIMIT_LOCK never recovers")
    await wait_event(client, auth, run["id"], "run.waiting")
    limit_lock.write_text(str(int(time.time()) + 2))  # 풀렸다고 믿었지만 다시 한도
    failed = await _wait_status(client, auth, run["id"], "failed", timeout=15)
    assert "기다렸지만" in failed["error"]


async def test_cooldown_survives_restart(client, auth, make_dir):
    acc = (await client.get("/api/accounts", headers=auth)).json()[0]
    aid = UUID(acc["id"])
    profiles.cool_down(aid, 600)
    await profiles.flush_persist()
    async with sessionmaker()() as db:
        row = await db.scalar(select(AppSetting).where(AppSetting.key == profiles.COOLDOWN_KEY))
        assert str(aid) in row.value["until"]
    profiles.reset_cooldowns()  # 프로세스 재시작
    assert profiles.cooldown_remaining(aid) == 0
    async with sessionmaker()() as db:
        assert await profiles.load_cooldowns(db) == 1
    assert 590 < profiles.cooldown_remaining(aid) <= 600
    profiles.clear_cooldown(aid)
    await profiles.flush_persist()
    profiles.reset_cooldowns()
    async with sessionmaker()() as db:
        assert await profiles.load_cooldowns(db) == 0


async def test_structured_rate_limit_event_triggers_fallback_even_without_known_wording(client, auth, make_dir):
    sess = await _session(client, auth, make_dir, kind="orchestrator")
    run = await run_to_end(client, auth, sess["id"], "STRUCT_LIMIT please")
    assert run["status"] == "succeeded" and run["profile"] == "codex"
    claude = (await client.get("/api/accounts", headers=auth)).json()[0]
    assert 3500 < claude["cooldown_seconds"] <= 3640  # 이벤트가 알려 준 resetsAt 까지 쉰다


async def test_orchestrator_uses_allowed_model_when_switching_engine(client, auth, make_dir):
    orch = await _session(client, auth, make_dir, kind="orchestrator")
    await run_to_end(client, auth, orch["id"], "CLAUDE_LIMIT")
    codex = next(c for c in agent_calls() if c["engine"] == "codex")
    assert codex["argv"][codex["argv"].index("-m") + 1] == "gpt-6.1-sol"
    assert 'model_reasoning_effort="high"' in codex["argv"]
    # 일반 대화는 모델을 건드리지 않는다.
    profiles.reset_cooldowns()
    chat = await _session(client, auth, make_dir, engine="auto")
    before = len(agent_calls())
    await run_to_end(client, auth, chat["id"], "CLAUDE_LIMIT")
    codex = next(c for c in agent_calls()[before:] if c["engine"] == "codex")
    assert "-m" not in codex["argv"]


async def test_account_resuming_own_conversation_gets_turns_it_missed(client, auth, make_dir):
    sess = await _session(client, auth, make_dir, kind="orchestrator")
    assert (await run_to_end(client, auth, sess["id"], "alpha one"))["profile"] == "claude"
    claude = (await client.get("/api/accounts", headers=auth)).json()[0]
    profiles.cool_down(UUID(claude["id"]), 600)
    assert (await run_to_end(client, auth, sess["id"], "bravo two"))["profile"] == "codex"
    assert (await client.post(f"/api/accounts/{claude['id']}/cooldown/clear", headers=auth)).status_code == 200
    before = len(agent_calls())
    assert (await run_to_end(client, auth, sess["id"], "charlie three"))["profile"] == "claude"
    call = agent_calls()[before]
    prompt = call["argv"][call["argv"].index("-p") + 1]
    assert "--resume" in call["argv"]
    assert "bravo two" in prompt and "charlie three" in prompt and "alpha one" not in prompt


def test_limit_wording_and_reset_time_variants():
    from datetime import datetime, timedelta

    msgs = [
        "You've hit your weekly limit · resets 3pm (Asia/Seoul)",
        "You've hit your limit · resets Oct 20, 3pm (Asia/Seoul)",
        "Weekly limit reached ∙ resets Mon 3pm (Asia/Seoul)",
        "You're out of extra usage · resets 7:20pm",
    ]
    for m in msgs:
        assert profiles.is_usage_limit(m), m
        left = profiles.reset_seconds_from(m)
        assert left is not None and 0 < left <= 8 * 86400, (m, left)
    at = profiles.reset_epoch_from(f"Claude AI usage limit reached|{int(time.time()) + 7200}")
    assert at is not None and 7190 < at - time.time() <= 7200
    soon = datetime.now().astimezone() + timedelta(days=2)
    text = f"resets {soon:%b} {soon.day}, 3pm (Asia/Seoul)"
    left = profiles.reset_seconds_from(text)
    assert left is not None and left < 4 * 86400
    assert profiles.reset_epoch_from("nothing here") is None
    assert not profiles.is_usage_limit("ordinary failure")
