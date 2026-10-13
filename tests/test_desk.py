"""ADR 0013: 접수(desk) → 기획(orchestrator, parent=desk) → 작업(worker). 역할별 지침, 역할별 실행 자리."""
from __future__ import annotations

import asyncio
import json
import time

import pytest
from conftest import agent_calls
from helpers import run_events, start_run, wait_run

from ttakkari.config import get_settings
from ttakkari.orchestrator.runtime import LEAD_NOTE, ORCHESTRATOR_NOTE, issue_token
from ttakkari.runs import instructions, profiles


async def _desk(client, auth, make_dir) -> dict:
    root = make_dir()
    ws = (await client.post("/api/workspaces", json={"name": root.name, "root_path": str(root)}, headers=auth)).json()
    r = await client.post("/api/sessions", json={"workspace_id": ws["id"], "kind": "desk"}, headers=auth)
    assert r.status_code == 201, r.text
    return r.json()


async def _wait(pred, timeout: float = 20.0, what: str = "조건"):
    deadline = time.monotonic() + timeout
    while True:
        v = await pred()
        if v:
            return v
        assert time.monotonic() < deadline, f"{what} 이(가) {timeout}초 안에 충족되지 않음"
        await asyncio.sleep(0.1)


async def _leads(client, auth, desk_id: str) -> list[dict]:
    return (await client.get("/api/sessions", params={"parent_id": desk_id}, headers=auth)).json()


def _desk_calls() -> list[dict]:
    return [c for c in agent_calls() if "--ephemeral" in c["argv"] or "--system-prompt" in c["argv"]]


async def test_desk_answers_directly_with_luna_without_global_harness(client, auth, make_dir):
    desk = await _desk(client, auth, make_dir)
    assert desk["kind"] == "desk" and desk["engine"] == "auto"
    run = await wait_run(client, auth, (await start_run(client, auth, desk["id"], "안녕 DESK_ANSWER"))["id"])
    assert run["status"] == "succeeded" and run["result_text"] == "desk answer by codex"
    assert run["engine"] == "codex" and run["model"] == "gpt-6-luna"
    route = next(e for e in await run_events(client, auth, run["id"]) if e["type"] == "desk.route")
    assert route["payload"]["action"] == "answer"
    assert await _leads(client, auth, desk["id"]) == []

    argv = _desk_calls()[-1]["argv"]
    # 전역 hook 끄기, 일회성, 읽기 전용, 접수 지침만, 따까리 MCP 없음
    assert argv[argv.index("--disable") + 1] == "hooks" and "--ephemeral" in argv
    assert argv[argv.index("-s") + 1] == "read-only"
    dev = json.loads(next(a for a in argv if a.startswith("developer_instructions=")).split("=", 1)[1])
    assert "접수 담당" in dev and "main orchestrator" not in dev
    assert not any(a.startswith("mcp_servers.ttakkari") for a in argv)
    assert 'model_reasoning_effort="low"' in argv


async def test_desk_delegates_to_new_lead_which_reports_back(client, auth, make_dir):
    desk = await _desk(client, auth, make_dir)
    run = await wait_run(client, auth, (await start_run(client, auth, desk["id"], "다크모드 고쳐줘 DESK_DELEGATE"))["id"])
    assert run["status"] == "succeeded" and run["result_text"].startswith("넘겼어요")
    leads = await _leads(client, auth, desk["id"])
    assert len(leads) == 1 and leads[0]["kind"] == "orchestrator" and leads[0]["title"] == "desk title"
    lead_run = (await client.get(f"/api/sessions/{leads[0]['id']}/runs", headers=auth)).json()[0]
    assert lead_run["origin"] == "desk" and lead_run["prompt"].startswith("[접수에서 넘긴 사용자 요청]\n다크모드 고쳐줘")
    assert (await wait_run(client, auth, lead_run["id"]))["status"] == "succeeded"

    async def report():
        runs = (await client.get(f"/api/sessions/{desk['id']}/runs", headers=auth)).json()
        return next((x for x in runs if x["origin"] == "report"), None)
    rep = await _wait(report, what="기획 보고")
    assert rep["status"] == "succeeded" and rep["result_text"] == "done"
    assert rep["prompt"].startswith("[기획 보고] 기획 세션 'desk title'") and f"lead_id={leads[0]['id']}" in rep["prompt"]

    # 기획 세션은 기획 지침·작업 세션 도구를 받는다. 사이드바에는 안 나온다.
    lead_call = next(c for c in agent_calls() if any("[접수에서 넘긴 사용자 요청]" in a for a in c["argv"]))
    note = lead_call["argv"][lead_call["argv"].index("--append-system-prompt") + 1]
    assert LEAD_NOTE in note and ORCHESTRATOR_NOTE not in note
    assert "mcp" in " ".join(lead_call["argv"]) and lead_call["argv"][lead_call["argv"].index("--model") + 1] == "opus"
    sidebar = (await client.get("/api/sessions", headers=auth)).json()
    assert leads[0]["id"] not in {s["id"] for s in sidebar} and desk["id"] in {s["id"] for s in sidebar}


async def test_desk_continues_existing_lead_from_board(client, auth, make_dir):
    desk = await _desk(client, auth, make_dir)
    await wait_run(client, auth, (await start_run(client, auth, desk["id"], "첫 일 DESK_DELEGATE"))["id"])
    lead = (await _leads(client, auth, desk["id"]))[0]
    await _wait(lambda: _all_done(client, auth, lead["id"]), what="첫 기획 턴")
    await wait_run(client, auth, (await start_run(client, auth, desk["id"], "그거 이어서 DESK_CONTINUE"))["id"])
    leads = await _leads(client, auth, desk["id"])
    assert [x["id"] for x in leads] == [lead["id"]]
    runs = (await client.get(f"/api/sessions/{lead['id']}/runs", headers=auth)).json()
    assert len(runs) == 2 and "그거 이어서" in runs[1]["prompt"]
    # 접수는 작업판에서 기획 세션 상태를 본다
    prompt = _desk_calls()[-1]["argv"][-1]
    assert f"lead_id={lead['id']}" in prompt and "「desk title」" in prompt


async def _all_done(client, auth, sid: str) -> bool:
    runs = (await client.get(f"/api/sessions/{sid}/runs", headers=auth)).json()
    return bool(runs) and all(r["status"] in {"succeeded", "failed", "cancelled", "interrupted"} for r in runs)


async def test_desk_falls_back_to_haiku_on_limit_and_cools_down_account(client, auth, make_dir):
    desk = await _desk(client, auth, make_dir)
    run = await wait_run(client, auth, (await start_run(client, auth, desk["id"], "야 DESK_CODEX_LIMIT"))["id"])
    assert run["status"] == "succeeded" and run["result_text"] == "desk answer by claude" and run["model"] == "haiku"
    evs = await run_events(client, auth, run["id"])
    assert next(e for e in evs if e["type"] == "desk.retry")["payload"]["reason"] == "usage_limit"
    argv = _desk_calls()[-1]["argv"]
    assert argv[argv.index("--setting-sources") + 1] == "local" and '"disableAllHooks": true' in argv[argv.index("--settings") + 1]
    assert argv[argv.index("--tools") + 1] == "" and "--strict-mcp-config" in argv
    # 한도 걸린 codex 계정은 쉬므로 다음 메시지는 바로 claude 로 간다
    before = len(_desk_calls())
    await wait_run(client, auth, (await start_run(client, auth, desk["id"], "또 DESK_ANSWER"))["id"])
    assert [c["engine"] for c in _desk_calls()[before:]] == ["claude"]
    profiles.reset_cooldowns()


async def test_bad_output_and_timeout_still_delegate(client, auth, make_dir, monkeypatch):
    desk = await _desk(client, auth, make_dir)
    run = await wait_run(client, auth, (await start_run(client, auth, desk["id"], "DESK_GARBAGE 뭐라는지"))["id"])
    # 두 엔진 모두 출력이 틀리면 멈추지 않고 기획으로 넘긴다
    assert run["status"] == "succeeded" and "기획 세션에 넘겼어요" in run["result_text"]
    assert len(await _leads(client, auth, desk["id"])) == 1

    monkeypatch.setattr(get_settings(), "desk_timeout_seconds", 0.5)
    t = time.monotonic()
    run = await wait_run(client, auth, (await start_run(client, auth, desk["id"], "DESK_SLOW 느림"))["id"])
    assert run["status"] == "succeeded" and time.monotonic() - t < 10
    reasons = [e["payload"]["reason"] for e in await run_events(client, auth, run["id"]) if e["type"] == "desk.retry"]
    assert reasons and all(r.startswith("시간 초과") for r in reasons)


async def test_desk_messages_do_not_queue_behind_each_other(client, auth, make_dir, monkeypatch):
    monkeypatch.setattr(get_settings(), "desk_timeout_seconds", 3)
    desk = await _desk(client, auth, make_dir)
    slow = await start_run(client, auth, desk["id"], "DESK_SLOW")
    t = time.monotonic()
    quick = await wait_run(client, auth, (await start_run(client, auth, desk["id"], "DESK_ANSWER"))["id"])
    assert quick["status"] == "succeeded" and time.monotonic() - t < 2.5
    await wait_run(client, auth, slow["id"])


async def test_lead_lane_does_not_block_workers(client, auth, make_dir, monkeypatch):
    from ttakkari.runs import manager as mgr

    monkeypatch.setattr(mgr.manager, "_sems", {"leads": asyncio.Semaphore(1)})
    desk = await _desk(client, auth, make_dir)
    await wait_run(client, auth, (await start_run(client, auth, desk["id"], "SLEEP DESK_DELEGATE"))["id"])
    lead = (await _leads(client, auth, desk["id"]))[0]
    lead_run = (await client.get(f"/api/sessions/{lead['id']}/runs", headers=auth)).json()[0]
    # 기획 자리 하나가 SLEEP 으로 차 있어도 작업 세션은 돈다
    r = await client.post("/api/orchestrator/workers", json={"title": "w", "prompt": "write", "engine": "claude"},
                          headers={"Authorization": f"Bearer {issue_token(lead['id'])}"})
    assert r.status_code == 201, r.text
    wrun = (await client.get(f"/api/sessions/{r.json()['worker_id']}/runs", headers=auth)).json()[0]
    assert (await wait_run(client, auth, wrun["id"]))["status"] == "succeeded"
    await client.post(f"/api/runs/{lead_run['id']}/cancel", headers=auth)
    await wait_run(client, auth, lead_run["id"])


async def test_role_instructions_are_separate(client, auth, make_dir):
    for role in ("common", "lead", "worker"):
        r = await client.put("/api/assistant/instructions", params={"role": role}, json={"text": f"RULE_{role}"}, headers=auth)
        assert r.status_code == 200 and r.json()["role"] == role
    got = (await client.get("/api/assistant/instructions", params={"role": "desk"}, headers=auth)).json()
    assert got["is_default"] and got["text"] == instructions.DESK_DEFAULT

    desk = await _desk(client, auth, make_dir)
    await wait_run(client, auth, (await start_run(client, auth, desk["id"], "DESK_DELEGATE"))["id"])
    lead = (await _leads(client, auth, desk["id"]))[0]
    await _wait(lambda: _all_done(client, auth, lead["id"]), what="기획 턴")
    r = await client.post("/api/orchestrator/workers", json={"title": "w", "prompt": "WORKER_JOB", "engine": "claude"},
                          headers={"Authorization": f"Bearer {issue_token(lead['id'])}"})
    wrun = (await client.get(f"/api/sessions/{r.json()['worker_id']}/runs", headers=auth)).json()[0]
    await wait_run(client, auth, wrun["id"])

    def note(marker: str) -> str:
        call = next(c for c in reversed(agent_calls()) if any(marker in a for a in c["argv"]))
        argv = call["argv"]
        if "--append-system-prompt" in argv:
            return argv[argv.index("--append-system-prompt") + 1]
        return json.loads(next(a for a in argv if a.startswith("developer_instructions=")).split("=", 1)[1])

    lead_note, worker_note, desk_note = note("[접수에서 넘긴"), note("WORKER_JOB"), note("[사용자 메시지]")
    assert "RULE_common" in lead_note and "RULE_lead" in lead_note and "RULE_worker" not in lead_note
    assert "RULE_common" in worker_note and "RULE_worker" in worker_note and "RULE_lead" not in worker_note
    assert not any(f"RULE_{r}" in desk_note for r in ("common", "lead", "worker"))

    r = await client.put("/api/assistant/instructions", params={"role": "desk"}, json={"text": "DESK_RULE"}, headers=auth)
    assert not r.json()["is_default"]
    reset = (await client.delete("/api/assistant/instructions", params={"role": "desk"}, headers=auth)).json()
    assert reset["is_default"] and reset["text"] == instructions.DESK_DEFAULT
    assert (await client.get("/api/assistant/instructions", params={"role": "nope"}, headers=auth)).status_code == 422


@pytest.fixture(autouse=True)
def _clean_harness():
    yield
    for role in instructions.FILES:
        instructions.reset(role)


# ---- 접수 상주 풀(desk_pool)

def _warm(engine: str):
    from ttakkari.runs import desk_pool

    return next((w for w in desk_pool._warm.values() if w.key[0] == engine), None)


async def _warm_ready(engine: str, other_than: int | None = None):
    async def ready():
        w = _warm(engine)
        return w if w and w.alive() and w.proc.pid != other_than else None
    return await _wait(ready, what=f"{engine} 상주 프로세스")


def _pool_turns(mode: str) -> list[dict]:
    return [c for c in agent_calls() if c.get("mode") == mode]


def _cold_calls() -> list[dict]:
    # 상주 claude 는 띄울 때도 argv 가 기록된다(--input-format 이 있으면 띄운 것).
    return [c for c in _desk_calls() if "mode" not in c and "--input-format" not in c["argv"]]


async def _route(client, auth, run_id: str) -> dict:
    return next(e for e in await run_events(client, auth, run_id) if e["type"] == "desk.route")["payload"]


def _gone(pid: int) -> bool:
    import os

    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    return False


async def test_warm_pool_off_runs_cli_each_time(client, auth, make_dir):
    assert get_settings().desk_warm_pool is False
    desk = await _desk(client, auth, make_dir)
    for _ in range(2):
        run = await wait_run(client, auth, (await start_run(client, auth, desk["id"], "DESK_ANSWER"))["id"])
        assert run["result_text"] == "desk answer by codex" and (await _route(client, auth, run["id"]))["warm"] is False
    assert len(_cold_calls()) == 2 and not _pool_turns("app-server-spawn") and _warm("codex") is None


async def test_warm_pool_answers_from_prespawned_codex_and_replaces_it(client, auth, make_dir, monkeypatch):
    monkeypatch.setattr(get_settings(), "desk_warm_pool", True)
    desk = await _desk(client, auth, make_dir)
    # 첫 메시지는 풀이 비어 있어 1회 실행으로 받고, 그동안 다음 메시지용 프로세스를 띄운다.
    first = await wait_run(client, auth, (await start_run(client, auth, desk["id"], "DESK_ANSWER"))["id"])
    assert first["result_text"] == "desk answer by codex" and (await _route(client, auth, first["id"]))["warm"] is False
    assert len(_cold_calls()) == 1
    w = await _warm_ready("codex")
    old_pid = w.proc.pid

    run = await wait_run(client, auth, (await start_run(client, auth, desk["id"], "다시 DESK_ANSWER"))["id"])
    assert run["status"] == "succeeded" and run["result_text"] == "desk answer by codex" and run["model"] == "gpt-6-luna"
    assert (await _route(client, auth, run["id"]))["warm"] is True
    assert len(_cold_calls()) == 1
    # 접수 플래그를 그대로 유지한다: hook 끄기는 띄울 때, 일회성·읽기 전용·지침·모델·effort 는 스레드에.
    spawn = _pool_turns("app-server-spawn")[0]["argv"]
    assert spawn[:3] == ["app-server", "--disable", "hooks"]
    turn = _pool_turns("app-server-turn")[-1]
    th = turn["thread"]
    assert th["ephemeral"] is True and th["sandbox"] == "read-only" and th["approvalPolicy"] == "never"
    assert th["model"] == "gpt-6-luna" and th["config"] == {"model_reasoning_effort": "low"}
    assert "접수 담당" in th["developerInstructions"] and "다시 DESK_ANSWER" in turn["prompt"]
    # 한 번 쓴 프로세스는 버리고(대화가 쌓이지 않게) 새로 띄운다.
    await _wait(lambda: asyncio.sleep(0, _gone(old_pid)), what="쓴 프로세스 종료")
    assert (await _warm_ready("codex", other_than=old_pid)).proc.pid != old_pid


async def test_warm_pool_falls_back_to_cold_when_process_dies(client, auth, make_dir, monkeypatch):
    import os
    import signal

    monkeypatch.setattr(get_settings(), "desk_warm_pool", True)
    desk = await _desk(client, auth, make_dir)
    await wait_run(client, auth, (await start_run(client, auth, desk["id"], "DESK_ANSWER"))["id"])
    w = await _warm_ready("codex")
    # 띄워 둔 프로세스가 밖에서 죽었다
    os.killpg(w.proc.pid, signal.SIGKILL)
    await _wait(lambda: asyncio.sleep(0, not w.alive()), what="죽음 감지")
    run = await wait_run(client, auth, (await start_run(client, auth, desk["id"], "DESK_ANSWER"))["id"])
    assert run["result_text"] == "desk answer by codex" and (await _route(client, auth, run["id"]))["warm"] is False
    assert len(_cold_calls()) == 2

    # 주고받는 도중에 죽어도 cold 로 다시 받는다
    await _warm_ready("codex", other_than=w.proc.pid)
    run = await wait_run(client, auth, (await start_run(client, auth, desk["id"], "DESK_DIE 하고"))["id"])
    assert run["status"] == "succeeded" and run["result_text"] == "desk answer by codex"
    assert (await _route(client, auth, run["id"]))["warm"] is False
    assert len(_cold_calls()) == 3 and "DESK_DIE" in _pool_turns("app-server-turn")[-1]["prompt"]


async def test_warm_pool_limit_timeout_and_claude_stream(client, auth, make_dir, monkeypatch):
    monkeypatch.setattr(get_settings(), "desk_warm_pool", True)
    desk = await _desk(client, auth, make_dir)
    await wait_run(client, auth, (await start_run(client, auth, desk["id"], "DESK_ANSWER"))["id"])
    await _warm_ready("codex")
    # 상주 codex 가 한도를 알리면 cold 와 똑같이 계정을 쉬게 하고 claude 로 넘어간다(다시 부르지 않는다).
    run = await wait_run(client, auth, (await start_run(client, auth, desk["id"], "DESK_CODEX_LIMIT"))["id"])
    assert run["result_text"] == "desk answer by claude"
    retry = next(e for e in await run_events(client, auth, run["id"]) if e["type"] == "desk.retry")["payload"]
    assert retry["reason"] == "usage_limit" and retry["warm"] is True
    assert len(_cold_calls()) == 2  # 첫 codex + 첫 claude

    w = await _warm_ready("claude")
    run = await wait_run(client, auth, (await start_run(client, auth, desk["id"], "또 DESK_ANSWER"))["id"])
    assert run["result_text"] == "desk answer by claude" and (await _route(client, auth, run["id"]))["warm"] is True
    argv = _pool_turns("stream-turn")[-1]["argv"]
    assert argv[argv.index("--input-format") + 1] == "stream-json" and argv[argv.index("--model") + 1] == "haiku"
    assert argv[argv.index("--setting-sources") + 1] == "local" and argv[argv.index("--tools") + 1] == ""
    assert "--strict-mcp-config" in argv and "--no-session-persistence" in argv and "접수 담당" in argv[argv.index("--system-prompt") + 1]
    assert len(_cold_calls()) == 2
    profiles.reset_cooldowns()

    # 상주 프로세스에서 시간 초과면 다음 계정으로 넘어가고 멈추지 않는다
    await _warm_ready("codex", other_than=None)
    await _warm_ready("claude", other_than=w.proc.pid)
    monkeypatch.setattr(get_settings(), "desk_timeout_seconds", 0.5)
    t = time.monotonic()
    run = await wait_run(client, auth, (await start_run(client, auth, desk["id"], "DESK_SLOW"))["id"])
    assert run["status"] == "succeeded" and time.monotonic() - t < 10
    retries = [e["payload"] for e in await run_events(client, auth, run["id"]) if e["type"] == "desk.retry"]
    assert retries and retries[0]["warm"] is True and retries[0]["reason"].startswith("시간 초과")


async def test_warm_pool_respawns_on_config_change_and_age(client, auth, make_dir, monkeypatch):
    monkeypatch.setattr(get_settings(), "desk_warm_pool", True)
    desk = await _desk(client, auth, make_dir)
    await wait_run(client, auth, (await start_run(client, auth, desk["id"], "DESK_ANSWER"))["id"])
    w = await _warm_ready("codex")
    # 모델 설정이 바뀌면 옛 프로세스를 쓰지 않는다
    models = get_settings().desk_models | {"codex": {"model": "gpt-6-luna", "effort": "medium"}}
    monkeypatch.setattr(get_settings(), "desk_models", models)
    run = await wait_run(client, auth, (await start_run(client, auth, desk["id"], "DESK_ANSWER"))["id"])
    assert (await _route(client, auth, run["id"]))["warm"] is False and len(_cold_calls()) == 2
    w2 = await _warm_ready("codex", other_than=w.proc.pid)
    run = await wait_run(client, auth, (await start_run(client, auth, desk["id"], "DESK_ANSWER"))["id"])
    assert (await _route(client, auth, run["id"]))["warm"] is True
    assert _pool_turns("app-server-turn")[-1]["thread"]["config"] == {"model_reasoning_effort": "medium"}

    # 오래된 프로세스는 요청이 없어도 새로 띄운다
    w3 = await _warm_ready("codex", other_than=w2.proc.pid)
    monkeypatch.setattr(get_settings(), "desk_warm_max_age_seconds", 0.5)
    await wait_run(client, auth, (await start_run(client, auth, desk["id"], "DESK_ANSWER"))["id"])  # 짧은 수명으로 다시 띄운다
    w4 = await _warm_ready("codex", other_than=w3.proc.pid)
    await _warm_ready("codex", other_than=w4.proc.pid)
    await _wait(lambda: asyncio.sleep(0, _gone(w4.proc.pid)), what="오래된 프로세스 종료")
