"""ADR 0006: 놀고 있는 계정 먼저 고르기, 계정별 상태(account_usage), 보관함·메모·서버 도구용 에이전트 API."""
from __future__ import annotations

import time
import uuid

from conftest import agent_calls
from helpers import run_events, run_to_end, start_run, wait_run
from test_orchestrator import _orch, _session, _wait

from ttakkari.orchestrator.runtime import issue_token
from ttakkari.runs import profiles
from ttakkari.runs import usage as usage_mod
from ttakkari.runs.profiles import Profile


def _p(name: str, engine: str = "claude") -> Profile:
    return Profile(id=uuid.uuid4(), name=name, engine=engine)


async def _three_accounts(client, auth, make_dir) -> dict[str, dict]:
    """우선순위 claude(기본 계정) > claude-2 > codex."""
    d = make_dir()
    b = (await client.post("/api/accounts", json={"name": "claude-2", "engine": "claude", "config_dir": str(d)}, headers=auth)).json()
    accs = {a["name"]: a for a in (await client.get("/api/accounts", headers=auth)).json()}
    ids = [accs["claude"]["id"], b["id"], accs["codex"]["id"]]
    assert (await client.post("/api/accounts/reorder", json={"ids": ids}, headers=auth)).status_code == 200
    return {"claude": accs["claude"], "claude-2": b, "codex": accs["codex"]}


async def _wait_calls(n: int) -> list[dict]:
    async def enough():
        return agent_calls() if len(agent_calls()) >= n else None

    return await _wait(enough, 10, f"엔진 호출 {n}개")


# ---- choose: 순서 규칙

def test_choose_prefers_idle_account_then_priority():
    a, b, c = _p("a"), _p("b"), _p("c")
    r1, r2, r3 = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    try:
        assert profiles.choose([a, b, c], r1) is a  # 모두 놀면 우선순위
        assert profiles.choose([a, b, c], r2) is b  # a 는 r1 이 쓰는 중
        assert profiles.choose([a, b, c], r3) is c
        assert profiles.running_counts() == {a.id: 1, b.id: 1, c.id: 1}
        # 모두 바쁘면 다시 우선순위
        assert profiles.choose([a, b, c], uuid.uuid4()) is a
        profiles.release(r2)
        assert profiles.choose([a, b, c], uuid.uuid4()) is b
    finally:
        for rid in list(profiles._in_use):
            profiles.release(rid)


def test_choose_ignores_own_claim_and_can_skip_idle_rule():
    a, b = _p("a"), _p("b")
    rid = uuid.uuid4()
    try:
        assert profiles.choose([a, b], rid) is a
        assert profiles.choose([b, a], rid) is b  # 자기 자신이 쓰던 계정은 바쁜 것으로 세지 않는다
        other = uuid.uuid4()
        assert profiles.choose([a, b], other, idle_first=False) is a  # 오케스트레이터 시작은 우선순위
    finally:
        for r in list(profiles._in_use):
            profiles.release(r)


def test_choose_model_engine_comes_before_idleness():
    claude, codex = _p("c1", "claude"), _p("x1", "codex")
    rid = uuid.uuid4()
    try:
        assert profiles.choose([claude, codex], uuid.uuid4()) is claude
        # claude 가 바빠도 claude 모델을 정했으면 claude 계정 먼저(모델 이름은 그 엔진 계정에만 넘어간다)
        assert profiles.choose([claude, codex], rid, prefer_engine="claude") is claude
        assert profiles.choose([claude, codex], uuid.uuid4(), prefer_engine="codex") is codex
    finally:
        for r in list(profiles._in_use):
            profiles.release(r)


def test_limited_until_reads_cooldown_and_exhausted_usage_without_network():
    aid = uuid.uuid4()
    assert profiles.limited_until(aid) is None
    profiles.cool_down(aid, 600)
    assert profiles.limited_until(aid) > time.time()
    profiles.clear_cooldown(aid)
    bid = uuid.uuid4()
    usage_mod.remember_stream(bid, [usage_mod.Window("five_hour", "5시간", 100, int(time.time()) + 900)])
    assert profiles.limited_until(bid) > time.time()


def test_usage_cap_blocks_at_or_above_threshold_and_expired_window_is_ignored(monkeypatch):
    aid = uuid.uuid4()
    profile = Profile(id=aid, name="limited", engine="codex", max_used_percent=95)
    now = int(time.time())
    usage_mod.remember_stream(aid, [usage_mod.Window("five_hour", "5시간", 95, now + 3600),
                                   usage_mod.Window("seven_day", "주간", 94, now + 86400)])
    assert profiles.usage_limit_until(profile) > time.time()
    usage_mod.remember_stream(aid, [usage_mod.Window("five_hour", "5시간", 94, now + 3600),
                                   usage_mod.Window("seven_day", "주간", 95, now + 86400)])
    assert profiles.usage_limit_until(profile) > time.time()
    usage_mod.remember_stream(aid, [usage_mod.Window("five_hour", "5시간", 100, now - 1),
                                   usage_mod.Window("seven_day", "주간", 10, now + 86400)])
    assert profiles.usage_limit_until(profile) is None


def test_auto_choose_balances_fresh_usage_but_stale_values_keep_priority():
    a, b = _p("a", "codex"), _p("b", "claude")
    a = Profile(a.id, a.name, a.engine, max_used_percent=95)
    now = int(time.time())
    usage_mod.remember_stream(a.id, [usage_mod.Window("five_hour", "5시간", 80, now + 3600),
                                    usage_mod.Window("seven_day", "주간", 50, now + 86400)])
    usage_mod.remember_stream(b.id, [usage_mod.Window("five_hour", "5시간", 30, now + 7200),
                                    usage_mod.Window("seven_day", "주간", 45, now + 86400)])
    rid = uuid.uuid4()
    try:
        assert profiles.choose([a, b], rid, balance_usage=True) is b
        usage_mod._stream[b.id].updated_at = time.time() - 3600
        # 하나라도 stale/null 이면 추측하지 않고 기존 순서를 따른다.
        assert profiles.choose([a, b], uuid.uuid4(), balance_usage=True) is a
    finally:
        for run_id in list(profiles._in_use):
            profiles.release(run_id)


# ---- 시작할 때: 작업 세션은 놀고 있는 계정으로

async def test_workers_spread_over_idle_accounts(client, auth, make_dir):
    accs = await _three_accounts(client, auth, make_dir)
    s1 = await _session(client, auth, make_dir, engine="auto")
    s2 = await _session(client, auth, make_dir, engine="auto")
    s3 = await _session(client, auth, make_dir, engine="auto")
    await start_run(client, auth, s1["id"], "first SLEEP")
    await _wait_calls(1)
    await start_run(client, auth, s2["id"], "second SLEEP")
    await _wait_calls(2)
    await start_run(client, auth, s3["id"], "third SLEEP")
    calls = await _wait_calls(3)
    # 기본 계정(claude) → claude-2 → codex. 이미 다른 Run 이 쓰는 계정은 건너뛴다.
    assert [c["engine"] for c in calls] == ["claude", "claude", "codex"]
    assert calls[0]["account_dir"] is None and calls[1]["account_dir"] == accs["claude-2"]["config_dir"]


async def test_auto_worker_skips_account_at_its_configured_usage_cap(client, auth, make_dir):
    accs = await _three_accounts(client, auth, make_dir)
    await client.patch(f"/api/accounts/{accs['claude']['id']}", json={"max_used_percent": 95}, headers=auth)
    claude_id = uuid.UUID(accs["claude"]["id"])
    now = int(time.time())
    usage_mod.remember_stream(claude_id, [usage_mod.Window("five_hour", "5시간", 30, now + 3600),
                                         usage_mod.Window("seven_day", "주간", 95, now + 86400)])
    worker = await _session(client, auth, make_dir, engine="auto")
    run = await run_to_end(client, auth, worker["id"], "small task")
    assert run["status"] == "succeeded" and run["profile"] == "claude-2"


async def test_account_cap_update_changes_usage_and_auto_assignment(client, auth, make_dir):
    accs = await _three_accounts(client, auth, make_dir)
    orch = await _session(client, auth, make_dir, kind="orchestrator")
    token = _orch(issue_token(orch["id"]))
    claude_id = uuid.UUID(accs["claude"]["id"])
    now = int(time.time())
    usage_mod.remember_stream(claude_id, [usage_mod.Window("five_hour", "5시간", 95, now + 3600),
                                         usage_mod.Window("seven_day", "주간", 20, now + 86400)])

    before = (await client.get("/api/orchestrator/accounts/usage", headers=token)).json()
    claude = next(a for a in before["accounts"] if a["name"] == "claude")
    assert claude["max_used_percent"] == 100 and claude["available"] is True

    changed = await client.patch(f"/api/accounts/{accs['claude']['id']}", json={"max_used_percent": 95}, headers=auth)
    assert changed.status_code == 200 and changed.json()["max_used_percent"] == 95
    after = (await client.get("/api/orchestrator/accounts/usage", headers=token)).json()
    claude = next(a for a in after["accounts"] if a["name"] == "claude")
    assert claude["max_used_percent"] == 95 and claude["remaining_to_limit_percent"] == 0
    assert claude["available"] is False and claude["limit"]["waiting"] is True

    worker = await _session(client, auth, make_dir, engine="auto")
    run = await run_to_end(client, auth, worker["id"], "small task")
    assert run["status"] == "succeeded" and run["profile"] == "claude-2"


async def test_orchestrator_keeps_priority_at_start_even_if_account_is_busy(client, auth, make_dir):
    accs = await _three_accounts(client, auth, make_dir)
    s1 = await _session(client, auth, make_dir, engine="auto")
    await start_run(client, auth, s1["id"], "hold SLEEP")
    await _wait_calls(1)
    orch = await _session(client, auth, make_dir, kind="orchestrator")
    await start_run(client, auth, orch["id"], "orch SLEEP")
    calls = await _wait_calls(2)
    # 작업 세션이 쓰는 claude 가 바빠도 메인은 우선순위대로 시작한다(엔진 대화 이어쓰기).
    assert calls[1]["engine"] == "claude" and calls[1]["account_dir"] is None
    assert accs["claude-2"]["config_dir"] != calls[1]["account_dir"]


async def test_finished_run_frees_its_account(client, auth, make_dir):
    await _three_accounts(client, auth, make_dir)
    s1 = await _session(client, auth, make_dir, engine="auto")
    r1 = await run_to_end(client, auth, s1["id"], "quick")
    assert r1["profile"] == "claude"
    s2 = await _session(client, auth, make_dir, engine="auto")
    assert (await run_to_end(client, auth, s2["id"], "quick"))["profile"] == "claude"
    assert profiles.running_counts() == {}


async def test_explicit_engine_session_only_uses_that_engine_even_if_busy(client, auth, make_dir):
    await _three_accounts(client, auth, make_dir)
    s1 = await _session(client, auth, make_dir, engine="codex")
    await start_run(client, auth, s1["id"], "hold SLEEP")
    await _wait_calls(1)
    s2 = await _session(client, auth, make_dir, engine="codex")
    run = await run_to_end(client, auth, s2["id"], "quick")
    assert run["engine"] == "codex" and run["profile"] == "codex"


# ---- 한도 전환: 놀고 있는 계정 먼저

async def test_fallback_prefers_idle_account_over_priority(client, auth, make_dir):
    await _three_accounts(client, auth, make_dir)
    # claude-2 를 다른 Run 이 쓰는 중으로 만든다(claude 가 먼저 바쁘도록 한 Run 을 더 둔다).
    hold1 = await _session(client, auth, make_dir, engine="auto")
    hold2 = await _session(client, auth, make_dir, engine="auto")
    await start_run(client, auth, hold1["id"], "hold SLEEP")
    await _wait_calls(1)
    await start_run(client, auth, hold2["id"], "hold SLEEP")
    await _wait_calls(2)  # claude(기본), claude-2 가 바쁘다
    orch = await _session(client, auth, make_dir, kind="orchestrator")
    run = await start_run(client, auth, orch["id"], "CLAUDE_SESSION_LIMIT")
    done = await wait_run(client, auth, run["id"])
    assert done["status"] == "succeeded" and done["profile"] == "codex", done
    fb = [e for e in await run_events(client, auth, run["id"]) if e["type"] == "engine.fallback"]
    # 우선순위로는 claude-2 가 다음이지만 다른 Run 이 쓰는 중이라 놀고 있는 codex 로 넘어간다.
    assert [(e["payload"]["from"], e["payload"]["to"]) for e in fb] == [("claude", "codex")]


async def test_fallback_keeps_priority_when_next_accounts_are_equally_idle(client, auth, make_dir):
    await _three_accounts(client, auth, make_dir)
    orch = await _session(client, auth, make_dir, kind="orchestrator")
    run = await run_to_end(client, auth, orch["id"], "CLAUDE_SESSION_LIMIT")
    assert run["status"] == "succeeded" and run["profile"] == "claude-2"


# ---- account_usage

async def test_account_usage_reports_state_without_secrets(client, auth, make_dir):
    accs = await _three_accounts(client, auth, make_dir)
    orch = await _session(client, auth, make_dir, kind="orchestrator")
    h = _orch(issue_token(orch["id"]))
    codex_id = uuid.UUID(accs["codex"]["id"])
    claude_id = uuid.UUID(accs["claude"]["id"])
    now = int(time.time())
    usage_mod.remember_stream(claude_id, [usage_mod.Window("five_hour", "5시간", 42, now + 3600),
                                          usage_mod.Window("seven_day", "주간", 10, now + 86400),
                                          usage_mod.Window("seven_day_opus", "주간 Opus", 70, now + 86400)])
    profiles.cool_down(codex_id, 1200)

    r = await client.get("/api/orchestrator/accounts/usage", headers=h)
    assert r.status_code == 200, r.text
    body = r.json()
    by = {a["name"]: a for a in body["accounts"]}
    assert list(by) == ["claude", "claude-2", "codex"] and [a["priority"] for a in body["accounts"]] == [1, 2, 3]
    c = by["claude"]
    assert c["engine"] == "claude" and c["scope"] == "personal" and c["enabled"] and c["available"]
    assert c["models"] == ["opus", "sonnet", "haiku"] and c["orchestrator_model"] == {"model": "opus", "effort": "medium"}
    assert c["usage"]["five_hour"]["used_percent"] == 42 and c["usage"]["five_hour"]["remaining_percent"] == 58
    assert c["usage"]["weekly"]["remaining_percent"] == 90 and c["usage"]["five_hour"]["resets_at"].endswith("+09:00")
    assert c["usage"]["model_weekly"]["opus"]["used_percent"] == 70
    assert c["max_used_percent"] == 100 and c["remaining_to_limit_percent"] == 58
    assert c["usage"]["source"] == "stream" and c["usage"]["stale"] is False and c["usage"]["measured_at"]
    assert c["limit"] == {"waiting": False, "until": None, "reason": None} and c["running_runs"] == 0
    # 한 번도 못 본 계정은 값이 없다고 알린다.
    assert by["claude-2"]["usage"] is None
    x = by["codex"]
    assert x["available"] is False and x["limit"]["waiting"] and x["limit"]["reason"] == "cooldown" and x["limit"]["until"]
    assert x["orchestrator_model"] == {"model": "gpt-6.1-sol", "effort": "high"} and x["models"] == ["gpt-6-luna", "gpt-6.1-sol"]
    # 계정 폴더·로그인 정보는 나오지 않는다.
    raw = r.text
    assert accs["claude-2"]["config_dir"] not in raw and "config_dir" not in raw and "token" not in raw.replace("tokens", "")


async def test_account_usage_marks_stale_and_passed_windows_and_running_count(client, auth, make_dir):
    accs = await _three_accounts(client, auth, make_dir)
    orch = await _session(client, auth, make_dir, kind="orchestrator")
    h = _orch(issue_token(orch["id"]))
    claude_id = uuid.UUID(accs["claude"]["id"])
    worker = await _session(client, auth, make_dir, engine="auto")
    await start_run(client, auth, worker["id"], "busy SLEEP")
    await _wait_calls(1)

    async def streamed():
        return claude_id in usage_mod._stream  # 실행 중 엔진이 낸 사용량 이벤트가 바로 반영된다

    await _wait(streamed, 10, "실행 중 사용량 이벤트")
    usage_mod.remember_stream(claude_id, [usage_mod.Window("five_hour", "5시간", 80, int(time.time()) - 60)])
    usage_mod._stream[claude_id].updated_at = time.time() - 3600
    body = (await client.get("/api/orchestrator/accounts/usage", headers=h)).json()
    c = next(a for a in body["accounts"] if a["name"] == "claude")
    assert c["running_runs"] == 1 and c["usage"]["stale"] is True and c["usage"]["age_seconds"] >= 3600
    assert c["usage"]["five_hour"]["reset_since_measured"] is True and c["usage"]["five_hour"]["remaining_percent"] is None
    assert c["usage"]["weekly"] is None
    assert next(a for a in body["accounts"] if a["name"] == "claude-2")["running_runs"] == 0


async def test_account_usage_work_scope_and_token_rules(client, auth, make_dir):
    accs = await _three_accounts(client, auth, make_dir)
    r = await client.patch(f"/api/accounts/{accs['claude-2']['id']}", json={"pool": "work"}, headers=auth)
    assert r.status_code == 200
    personal = await _session(client, auth, make_dir, kind="orchestrator")
    work = await _session(client, auth, make_dir, kind="orchestrator", pool="work")
    ph = (await client.get("/api/orchestrator/accounts/usage", headers=_orch(issue_token(personal["id"])))).json()
    assert {a["name"]: a["scope"] for a in ph["accounts"]} == {"claude": "personal", "claude-2": "work", "codex": "personal"}
    wh = (await client.get("/api/orchestrator/accounts/usage", headers=_orch(issue_token(work["id"])))).json()
    assert [a["name"] for a in wh["accounts"]] == ["claude-2"]
    # 일반 세션 토큰·사용자 토큰·무토큰은 거절
    chat = await _session(client, auth, make_dir)
    assert (await client.get("/api/orchestrator/accounts/usage", headers=_orch(issue_token(chat["id"])))).status_code == 403
    assert (await client.get("/api/orchestrator/accounts/usage", headers=auth)).status_code == 401
    assert (await client.get("/api/orchestrator/accounts/usage")).status_code == 401


# ---- 에이전트 API: 서버 리소스·메모 태그·보관함 분류

async def test_agent_server_resources_matches_user_view(client, auth, make_dir):
    sess = await _session(client, auth, make_dir)
    r = await client.get("/api/agent/system/resources", headers=_orch(issue_token(sess["id"])))
    assert r.status_code == 200 and r.json()["cpu"]["cores_logical"] >= 1
    assert (await client.get("/api/agent/system/resources")).status_code == 401
    for row in r.json()["processes"]["top_cpu"]:
        assert "cmdline" not in row


async def test_agent_memo_tags_by_category(client, auth, make_dir):
    sess = await _session(client, auth, make_dir)
    h = _orch(issue_token(sess["id"]))
    cat = (await client.post("/api/memo-categories", json={"name": "업무"}, headers=auth)).json()
    await client.post("/api/memos", json={"body": "a", "tags": ["x", "y"], "category_id": cat["id"]}, headers=auth)
    await client.post("/api/memos", json={"body": "b", "tags": ["x"]}, headers=auth)
    allt = (await client.get("/api/agent/memos/tags", headers=h)).json()["tags"]
    assert allt == [{"name": "x", "count": 2}, {"name": "y", "count": 1}]
    named = (await client.get("/api/agent/memos/tags", params={"category": "업무"}, headers=h)).json()["tags"]
    assert named == [{"name": "x", "count": 1}, {"name": "y", "count": 1}]
    none = (await client.get("/api/agent/memos/tags", params={"category": "none"}, headers=h)).json()["tags"]
    assert none == [{"name": "x", "count": 1}]
    assert (await client.get("/api/agent/memos/tags", params={"category": "없는이름"}, headers=h)).status_code in (400, 422)


async def test_agent_artifact_area_filter_and_move(client, auth, make_dir):
    sess = await _session(client, auth, make_dir)
    ws = sess["workspace_id"]
    await run_to_end(client, auth, sess["id"], "write report")
    up = await client.post("/api/uploads", files={"file": ("up.md", b"# up", "text/markdown")},
                           data={"workspace_id": ws}, headers=auth)
    assert up.status_code == 201
    h = _orch(issue_token(sess["id"]))
    everything = (await client.get("/api/agent/artifacts", headers=h)).json()["artifacts"]
    assert {a["filename"]: a["area"] for a in everything} == {"report.md": "files", "up.md": "files"}
    # 채팅 세션의 outbox 결과물은 사용자에게 건넨 것이라 files. 에이전트 파일은 수정된 워크스페이스 파일 등이다.
    assert {a["filename"] for a in (await client.get("/api/agent/artifacts", params={"area": "agent"}, headers=h)).json()["artifacts"]} == set()
    report = next(a for a in everything if a["filename"] == "report.md")
    moved = await client.patch(f"/api/agent/artifacts/{report['id']}", json={"area": "agent"}, headers=h)
    assert moved.status_code == 200 and moved.json()["area"] == "agent"
    assert [a["filename"] for a in (await client.get("/api/agent/artifacts", params={"area": "agent"}, headers=h)).json()["artifacts"]] == ["report.md"]
    assert (await client.get(f"/api/artifacts/{report['id']}", headers=auth)).json()["area"] == "agent"
    back = await client.patch(f"/api/agent/artifacts/{report['id']}", json={"area": "files"}, headers=h)
    assert back.json()["area"] == "files"
    # 사용자가 올린 파일은 에이전트가 옮기지 못한다. 잘못된 값·없는 파일도 거절.
    mine = next(a for a in everything if a["filename"] == "up.md")
    assert (await client.patch(f"/api/agent/artifacts/{mine['id']}", json={"area": "agent"}, headers=h)).status_code == 403
    assert (await client.get(f"/api/artifacts/{mine['id']}", headers=auth)).json()["area"] == "files"
    assert (await client.patch(f"/api/agent/artifacts/{report['id']}", json={"area": "trash"}, headers=h)).status_code == 422
    assert (await client.patch(f"/api/agent/artifacts/{uuid.uuid4()}", json={"area": "agent"}, headers=h)).status_code == 404
    assert (await client.patch(f"/api/agent/artifacts/{report['id']}", json={"area": "agent"})).status_code == 401
