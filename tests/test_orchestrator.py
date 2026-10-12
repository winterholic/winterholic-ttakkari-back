"""ADR 0006: 엔진 프로필 폴백, 오케스트레이터 도구 API, 결과 보고 루프, MCP 서버."""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import time

from conftest import agent_calls
from helpers import run_events, run_to_end, wait_run

from ttakkari.orchestrator.runtime import issue_token


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
    run = await run_to_end(client, auth, sess["id"], "CLAUDE_LIMIT")
    assert run["status"] == "failed" and "usage limit" in run["error"]
    assert {c["engine"] for c in agent_calls()} == {"claude"}


async def test_all_profiles_limited_fails_with_last_error(client, auth, make_dir):
    sess = await _session(client, auth, make_dir, kind="orchestrator")
    run = await run_to_end(client, auth, sess["id"], "CLAUDE_LIMIT CODEX_LIMIT")
    assert run["status"] == "failed" and run["profile"] == "codex" and "usage limit" in run["error"]


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
    r = await client.post("/api/orchestrator/workers", json={"title": "보고서", "prompt": "write report"}, headers=h)
    assert r.status_code == 201, r.text
    wid = r.json()["worker_id"]

    worker = (await client.get(f"/api/sessions/{wid}", headers=auth)).json()
    assert worker["kind"] == "worker" and worker["parent_id"] == orch["id"] and worker["engine"] == "auto"
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

AGENT_TOOLS = {"recall", "remember", "get_entity", "describe_entity", "link", "forget", "list_artifacts", "share_file",
               "notify_user", "list_sessions", "read_session",
               "list_memos", "search_memos", "get_memo", "create_memo", "update_memo", "delete_memo",
               "list_events", "create_event", "update_event", "delete_event"}
WORKER_TOOLS = {"spawn_worker", "message_worker", "list_workers", "get_worker", "stop_worker"}


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


async def test_worker_list_has_summary_for_tabs(client, auth, make_dir):
    orch = await _session(client, auth, make_dir, kind="orchestrator")
    wid = (await client.post("/api/orchestrator/workers", json={"title": "w", "prompt": "go"},
                             headers=_orch(issue_token(orch["id"])))).json()["worker_id"]
    wrun = (await client.get(f"/api/sessions/{wid}/runs", headers=auth)).json()[0]
    await wait_run(client, auth, wrun["id"])
    tab = (await client.get("/api/sessions", params={"parent_id": orch["id"]}, headers=auth)).json()[0]
    assert tab["last_run_status"] == "succeeded" and tab["last_result"] == "done" and tab["last_run_profile"] == "claude"
