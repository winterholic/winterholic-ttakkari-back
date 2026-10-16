"""오케스트레이터 모델 정책(메인 고정·작업자 기본·승격 사유)과 컨텍스트 사용률·교체(사용자 결정 10-13)."""
from __future__ import annotations

import json
from pathlib import Path

from conftest import agent_calls
from helpers import run_to_end, wait_run
from test_orchestrator import _orch, _session, _wait

from ttakkari.agents.base import ParseState
from ttakkari.agents.claude import ClaudeAdapter
from ttakkari.agents.codex import read_context
from ttakkari.orchestrator.runtime import issue_token
from ttakkari.runs import profiles


def _model(call: dict) -> tuple[str | None, str | None]:
    argv = call["argv"]
    if call["engine"] == "claude":
        get = lambda k: argv[argv.index(k) + 1] if k in argv else None
        return get("--model"), get("--effort")
    model = argv[argv.index("-m") + 1] if "-m" in argv else None
    effort = next((a.split('"')[1] for a in argv if a.startswith("model_reasoning_effort=")), None)
    return model, effort


async def test_orchestrator_always_runs_on_its_allowed_model(client, auth, make_dir):
    # 세션에 다른 모델이 저장돼 있어도 메인은 Opus medium 으로 뜬다.
    orch = await _session(client, auth, make_dir, kind="orchestrator", engine="claude", model="sonnet", effort="low")
    before = len(agent_calls())
    await run_to_end(client, auth, orch["id"], "hi")
    assert _model(agent_calls()[before]) == ("opus", "medium")


async def test_worker_defaults_to_general_task_policy_and_pinned_engine_defaults(client, auth, make_dir):
    orch = await _session(client, auth, make_dir, kind="orchestrator")
    h = _orch(issue_token(orch["id"]))
    auto = (await client.post("/api/orchestrator/workers", json={"title": "a", "prompt": "go"}, headers=h)).json()
    assert (auto["engine"], auto["model"], auto["effort"]) == ("auto", "claude-sonnet-5-5-high", "high")
    claude = (await client.post("/api/orchestrator/workers", json={"title": "c", "prompt": "go", "engine": "claude"},
                                headers=h)).json()
    assert (claude["model"], claude["effort"]) == ("sonnet", "high")
    # Without an Antigravity account, general tasks balance native Sonnet/Luna candidates.
    wrun = (await client.get(f"/api/sessions/{auto['worker_id']}/runs", headers=auth)).json()[0]
    actual = await wait_run(client, auth, wrun["id"])
    assert (actual["engine"], actual["model"]) in {("claude", "sonnet"), ("codex", "gpt-6-luna")}
    # Both fake workers belong to this test. Finish the Claude assertion as well,
    # so fixture teardown need not signal a live process group in sandboxed macOS.
    crun = (await client.get(f"/api/sessions/{claude['worker_id']}/runs", headers=auth)).json()[0]
    assert (await wait_run(client, auth, crun["id"]))["profile"] == "claude"


async def test_peer_model_worker_needs_escalation_reason(client, auth, make_dir):
    orch = await _session(client, auth, make_dir, kind="orchestrator")
    h = _orch(issue_token(orch["id"]))
    r = await client.post("/api/orchestrator/workers", json={"title": "설계", "prompt": "go", "model": "opus"}, headers=h)
    assert r.status_code == 400 and "escalation_reason" in r.text
    r = await client.post("/api/orchestrator/workers", json={"title": "설계", "prompt": "go", "model": "gpt-6.1-sol",
                                                            "escalation_reason": "DB 분할 전략 결정"}, headers=h)
    assert r.status_code == 201 and r.json()["model"] == "gpt-6.1-sol"
    r = await client.post("/api/orchestrator/workers", json={"title": "x", "prompt": "go", "engine": "claude",
                                                            "model": "gpt-6-luna"}, headers=h)
    assert r.status_code == 400


async def test_worker_switching_engine_gets_same_grade_model(client, auth, make_dir):
    orch = await _session(client, auth, make_dir, kind="orchestrator")
    h = _orch(issue_token(orch["id"]))
    w = (await client.post("/api/orchestrator/workers", json={"title": "w", "prompt": "CODEX_LIMIT", "task_type":"verification"}, headers=h)).json()
    wrun = (await client.get(f"/api/sessions/{w['worker_id']}/runs", headers=auth)).json()[0]
    assert (await wait_run(client, auth, wrun["id"]))["profile"] == "claude"
    claude = next(c for c in agent_calls() if c["engine"] == "claude" and "CODEX_LIMIT" in json.dumps(c["argv"]))
    assert _model(claude) == ("sonnet", "high")
    profiles.reset_cooldowns()


async def test_context_usage_is_measured_and_shown_to_orchestrator(client, auth, make_dir):
    orch = await _session(client, auth, make_dir, kind="orchestrator")
    h = _orch(issue_token(orch["id"]))
    first = await run_to_end(client, auth, orch["id"], "CTX55 hello")
    assert first["usage"]["context_percent"] == 55 and first["usage"]["context_window"] == 200000
    ctx = (await client.get("/api/orchestrator/context", headers=h)).json()
    assert ctx["context_percent"] == 55 and "검토" in ctx["context"]
    before = len(agent_calls())
    await run_to_end(client, auth, orch["id"], "next")
    call = agent_calls()[before]
    prompt = call["argv"][call["argv"].index("-p") + 1]
    assert prompt.startswith("[내 대화 컨텍스트 사용률: 55% (110k/200k)")


async def test_worker_report_and_list_show_worker_context(client, auth, make_dir):
    orch = await _session(client, auth, make_dir, kind="orchestrator")
    h = _orch(issue_token(orch["id"]))
    wid = (await client.post("/api/orchestrator/workers", json={"title": "w", "prompt": "CTX72 go", "engine": "claude"},
                             headers=h)).json()["worker_id"]

    async def report():
        runs = (await client.get(f"/api/sessions/{orch['id']}/runs", headers=auth)).json()
        return next((x for x in runs if x["origin"] == "report"), None)
    rep = await _wait(report, what="결과 보고 Run")
    assert "작업 세션 컨텍스트: 72%" in rep["prompt"] and "교체 기준" in rep["prompt"]
    listed = (await client.get("/api/orchestrator/workers", headers=h)).json()
    assert listed[0]["worker_id"] == wid and listed[0]["context"].startswith("72%")


async def test_fresh_context_starts_new_engine_conversation_without_old_turns(client, auth, make_dir):
    orch = await _session(client, auth, make_dir, kind="orchestrator")
    h = _orch(issue_token(orch["id"]))
    wid = (await client.post("/api/orchestrator/workers", json={"title": "w", "prompt": "alpha CTX80", "engine": "claude"},
                             headers=h)).json()["worker_id"]
    wrun = (await client.get(f"/api/sessions/{wid}/runs", headers=auth)).json()[0]
    await wait_run(client, auth, wrun["id"])
    before = len(agent_calls())
    r = await client.post(f"/api/orchestrator/workers/{wid}/messages",
                          json={"prompt": "bravo self-contained", "fresh_context": True}, headers=h)
    assert r.status_code == 202
    nxt = (await client.get(f"/api/sessions/{wid}/runs", headers=auth)).json()[-1]
    await wait_run(client, auth, nxt["id"])
    call = agent_calls()[before]
    prompt = call["argv"][call["argv"].index("-p") + 1]
    assert "--resume" not in call["argv"] and "alpha" not in prompt and "bravo" in prompt
    # 새 대화 뒤에는 새로 잰 값만 보인다.
    assert (await client.get(f"/api/orchestrator/workers/{wid}", headers=h)).json()["context"].startswith("10%")


async def test_reset_my_context_during_turn_applies_after_it(client, auth, make_dir):
    orch = await _session(client, auth, make_dir, kind="orchestrator")
    h = _orch(issue_token(orch["id"]))
    await run_to_end(client, auth, orch["id"], "alpha one")
    busy = (await client.post(f"/api/sessions/{orch['id']}/runs", json={"prompt": "SLEEP"}, headers=auth)).json()

    async def running():
        return (await client.get(f"/api/runs/{busy['id']}", headers=auth)).json()["status"] == "running"
    await _wait(running, what="실행 중")
    assert (await client.post("/api/orchestrator/context/reset", headers=h)).json()["applies"] == "after this turn"
    await client.post(f"/api/runs/{busy['id']}/cancel", headers=auth)
    await wait_run(client, auth, busy["id"])
    before = len(agent_calls())
    await run_to_end(client, auth, orch["id"], "charlie three")
    call = agent_calls()[before]
    prompt = call["argv"][call["argv"].index("-p") + 1]
    assert "--resume" not in call["argv"] and "alpha one" not in prompt
    assert "컨텍스트 사용률: 알 수 없음" in prompt


def test_claude_context_ignores_subagent_requests():
    a, st = ClaudeAdapter(), ParseState()
    msg = {"model": "claude-opus-5-5", "content": [], "usage": {"input_tokens": 100, "cache_read_input_tokens": 99900}}
    a.parse_line({"type": "assistant", "message": msg}, st, Path("."))
    sub = {"model": "claude-sonnet-5-5", "content": [], "usage": {"input_tokens": 190000}}
    a.parse_line({"type": "assistant", "parent_tool_use_id": "toolu_1", "message": sub}, st, Path("."))
    a.parse_line({"type": "result", "subtype": "success", "result": "ok",
                  "modelUsage": {"claude-opus-5-5": {"contextWindow": 1000000}, "claude-sonnet-5-5": {"contextWindow": 200000}}},
                 st, Path("."))
    assert (st.context_tokens, st.context_window) == (100000, 1000000)


def test_codex_context_from_rollout(tmp_path):
    d = tmp_path / "sessions" / "2026" / "10" / "13"
    d.mkdir(parents=True)
    tc = lambda total: json.dumps({"type": "event_msg", "payload": {"type": "token_count", "info": {
        "last_token_usage": {"total_tokens": total}, "model_context_window": 258400}}})
    (d / "rollout-2026-10-13T01-00-00-thread-abc.jsonl").write_text("\n".join([tc(1000), '{"type":"x"}', tc(129200)]) + "\n")
    assert read_context(tmp_path, "thread-abc") == (129200, 258400)
    assert read_context(tmp_path, "other") is None
