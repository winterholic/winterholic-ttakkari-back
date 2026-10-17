"""ArtCraft 관제 API. 실제 설치(~/development/projects/artcraft)는 건드리지 않고, 가짜 bin/artcraft 로 계약을 검증한다."""
import asyncio
import json
import os
import stat
import textwrap

import pytest

from ttakkari import artcraft
from ttakkari.config import get_settings

SECRET = "SECRET-SENTINEL-do-not-leak"

FAKE = textwrap.dedent('''\
    #!/usr/bin/env python3
    import json, os, sys, time
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    beh_path = os.path.join(root, "behavior.json")
    beh = json.load(open(beh_path))
    with open(os.path.join(root, "calls.jsonl"), "a") as f:
        f.write(json.dumps({"argv": sys.argv[1:], "env": sorted(os.environ)}) + "\\n")
    time.sleep(beh.get("sleep", 0))
    reg = json.load(open(os.path.join(root, "registry.json")))["apps"]
    a = sys.argv[1:]
    if a[:1] == ["status"]:
        if beh.get("status_fail"):
            print("boom"); sys.exit(1)
        apps = []
        for n, m in reg.items():
            row = {"app": n, "title": m["title"], "domain": m["domain"], "site_status": m["site_status"],
                   "cli": {"ok": True, "version_line": f"{n}-cli {m['ver']}"},
                   "bundle": {"version": m["ver"], "installed": True},
                   "source": {"commit": "abc1234", "built_version": f"{n}-cli {m['ver']} (dev build)"},
                   "gui": {"pid": 99 if n in beh.get("running", []) else None, "control_port": m["control_port"],
                           "listening": n in beh.get("running", []), "control_auth": m["control_auth"],
                           "running": n in beh.get("running", [])},
                   "healthy": True}
            if "--deep" in a:
                row["mcp"] = {"ok": True, "tool_count": 25 if n == "alpha" else 400, "ms": 3, "error": None}
            if "--remote" in a:
                tag = beh.get("tags", {}).get(n)
                row["latest_release"] = tag
            apps.append(row)
        print(json.dumps({"generated_at": "x", "apps": apps})); sys.exit(0)
    if a[:1] == ["mcp-config"]:
        print(json.dumps({"mcpServers": {f"artcraft-{n}": {"command": os.path.join(root, "bin", "artcraft"),
              "args": ["mcp", n], "env": {}} for n in reg}})); sys.exit(0)
    if a[:2] in (["gui", "start"], ["gui", "stop"]):
        run = beh.setdefault("running", [])
        if a[1] == "start": run.append(a[2])
        else: run[:] = [x for x in run if x != a[2]]
        json.dump(beh, open(beh_path, "w"))
        print(json.dumps({"app": a[2], "listening": not beh.get("gui_dead")}))
        sys.exit(1 if (a[1] == "start" and beh.get("gui_dead")) else 0)
    if a[:1] == ["smoke"]:
        time.sleep(beh.get("smoke_sleep", 0))
        apps = [x for x in a[1:] if not x.startswith("--")] or list(reg)
        out = {"ts": "2099-01-01T00:00:00+0900", "source_build": False, "apps": {
            n: {"tests": [{"test": "t1", "cmd": "c", "ok": True, "rc": 0, "secs": 0.1, "evidence": "ok"},
                          {"test": "t2", "cmd": "c", "ok": n not in beh.get("smoke_fail", []), "rc": 1, "secs": 0.1,
                              "evidence": "token=abcdef failed"}],
                "mcp": {"ok": True, "tools": 7, "error": None}} for n in apps}}
        os.makedirs(os.path.join(root, "smoke", "results"), exist_ok=True)
        json.dump(out, open(os.path.join(root, "smoke", "results", "latest.json"), "w"))
        print("smoke done token=abcdef")
        sys.exit(1 if beh.get("smoke_fail") else 0)
    print("unknown", a); sys.exit(2)
    ''')

BUGS = textwrap.dedent('''\
    # BUGS.md

    | ID | App(s) | Sev | Kind | One line |
    |---|---|---|---|---|
    | BUG-001 | alpha | S1 | pr | alpha scans fonts |

    ---

    ## BUG-001: alpha scans fonts

    - **Repos:** storytold/alpha
    - **Apps:** alpha
    - **Severity:** S1
    - **Kind:** pr
    - **Difficulty:** medium
    - **Upstream:** related: checked 2026-10-18, similar alpha#12
    - **Found:** 2026-10-18
    - **Verified:** yes, release 1.0.0

    ### Summary

    Font scan stalls every open.

    ### Repro

    ```sh
    time alpha-cli info in.png
    ```

    ### Evidence

    ```
    1:36.66 total
    ```

    ### Suggestion

    Scan lazily.

    ## BUG-002: control channel is open

    - **Repos:** storytold/beta, storytold/gamma
    - **Apps:** all
    - **Severity:** S2
    - **Kind:** security
    - **Difficulty:** hard
    - **Upstream:** unchecked
    - **Found:** 2026-10-18
    - **Verified:** yes

    ### Summary

    No auth on the control port.

    ### Repro

    nc to the port.

    ### Evidence

    ok:true

    ## BUG-003: sloppy entry

    - **Repos:** alpha
    - **Apps:** alpha
    - **Severity:** S9
    - **Difficulty:** easy
    - **Upstream:** maybe
    - **Found:** yesterday
    - **Verified:** no

    ### Summary

    Something.

    ### Notes

    unknown subsection.

    ---

    ## Things checked that did NOT fail
    nothing.
    ''')


def _registry():
    def app(title, port, auth, ver, **extra):
        return {"title": title, "domain": f"{title} domain", "site_status": "In development", "control_port": port,
                "control_auth": auth, "ver": ver, "cli_modes": ["info"], "mcp_args": [], "env": {}, **extra}
    return {"apps": {
        "alpha": app("Alpha", 7001, "token-file", "1.0.0", env={"ALPHA_NO_FONTS": "1"}),
        "beta": app("Beta", 7002, "none-documented", "0.8.0"),
        "gamma": app("Gamma", None, "control-file", "0.7.0", control_file=f"run/{SECRET}.control"),
        "delta": app("Delta", 7004, "key-file", "0.7.0", key_file=f"~/{SECRET}/key"),
    }}


@pytest.fixture
def fake(tmp_path, monkeypatch):
    base = tmp_path / "artcraft"
    (base / "bin").mkdir(parents=True)
    script = base / "bin" / "artcraft"
    script.write_text(FAKE)
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    (base / "registry.json").write_text(json.dumps(_registry()))
    (base / "BUGS.md").write_text(BUGS)
    results = base / "smoke" / "results"
    results.mkdir(parents=True)
    original = {"ts": "2026-10-18T01:14:10+0900", "apps": {
        n: {"tests": [{"test": "t", "ok": True, "rc": 0, "evidence": "ok"}], "gui": {"started": True, "inspect": True, "screenshot": True}}
        for n in _registry()["apps"]}}
    (results / "latest.json").write_text(json.dumps(original))
    (base / "behavior.json").write_text("{}")
    s = get_settings()
    monkeypatch.setattr(s, "artcraft_root", base)
    monkeypatch.setattr(s, "artcraft_auto_remote_check", False)
    monkeypatch.setattr(s, "home", tmp_path / "home")
    artcraft.reset_caches()

    class Fake:
        root = base

        def behave(self, **kw):
            path = base / "behavior.json"
            data = json.loads(path.read_text())
            data.update(kw)
            path.write_text(json.dumps(data))

        def calls(self):
            path = base / "calls.jsonl"
            return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

        original_latest = (results / "latest.json").read_bytes()

    yield Fake()
    artcraft.reset_caches()


async def wait_job(client, auth, job_id, timeout=15):
    for _ in range(int(timeout / 0.05)):
        r = await client.get(f"/api/artcraft/jobs/{job_id}", headers=auth)
        assert r.status_code == 200, r.text
        if r.json()["state"] not in ("queued", "running"):
            return r.json()
        await asyncio.sleep(0.05)
    raise AssertionError("job did not finish")


async def test_requires_login(client, fake):
    for method, path in [("get", "/api/artcraft/apps"), ("post", "/api/artcraft/smoke"), ("get", "/api/artcraft/bugs"),
                         ("get", "/api/artcraft/agent-config"), ("post", "/api/artcraft/remote-check"),
                         ("post", "/api/artcraft/apps/alpha/gui"), ("put", "/api/artcraft/bugs/BUG-001")]:
        assert (await getattr(client, method)(path)).status_code == 401, path


async def test_overview_shape_and_no_secret_leak(client, auth, fake):
    r = await client.get("/api/artcraft/apps", headers=auth)
    assert r.status_code == 200, r.text
    body = r.json()
    assert [a["app"] for a in body["apps"]] == ["alpha", "beta", "gamma", "delta"]
    alpha, beta, gamma, delta = body["apps"]
    assert alpha["installed"]["release"] == "1.0.0" and alpha["installed"]["source_build"]["commit"] == "abc1234"
    assert alpha["mcp"]["tool_count"] == 25 and alpha["gui"] == {"running": False, "listening": False, "port": 7001, "transport": "tcp"}
    assert alpha["auth"] == {"mode": "token", "label": "토큰 파일", "protected": True}
    assert beta["auth"]["mode"] == "none" and beta["auth"]["protected"] is False
    assert gamma["gui"]["transport"] == "file" and gamma["gui"]["port"] is None
    assert delta["auth"]["mode"] == "key"
    assert alpha["smoke"]["passed"] == 1 and alpha["smoke"]["source"] == "artcraft"
    assert alpha["known_bugs"] == ["BUG-001", "BUG-002", "BUG-003"] and beta["known_bugs"] == ["BUG-002"]
    assert alpha["latest"]["tag"] is None
    assert "Cache-Control" in r.headers and r.headers["Cache-Control"] == "no-store"
    # registry 의 key_file·control_file 경로는 어떤 응답에도 나오지 않는다
    everything = json.dumps(body) + (await client.get("/api/artcraft/agent-config", headers=auth)).text
    assert SECRET not in everything and "key_file" not in everything and "control_file" not in everything


async def test_status_is_cached_and_single_flight(client, auth, fake):
    await asyncio.gather(*[client.get("/api/artcraft/apps", headers=auth) for _ in range(5)])
    await client.get("/api/artcraft/apps", headers=auth)
    assert len([c for c in fake.calls() if c["argv"][0] == "status"]) == 1
    await client.get("/api/artcraft/apps", params={"refresh": "true"}, headers=auth)
    assert len([c for c in fake.calls() if c["argv"][0] == "status"]) == 2


async def test_commands_are_fixed_and_env_is_scrubbed(client, auth, fake):
    await client.get("/api/artcraft/apps", headers=auth)
    call = fake.calls()[0]
    assert call["argv"] == ["status", "--json", "--deep"]
    assert not [k for k in call["env"] if k.startswith("TTAKKARI_")]
    assert "ALPHA_NO_FONTS" in call["env"]  # registry 의 앱별 보정 변수


async def test_stale_when_status_fails_after_success_and_502_when_cold(client, auth, fake):
    fake.behave(status_fail=True)
    r = await client.get("/api/artcraft/apps", headers=auth)
    assert r.status_code == 502 and r.json()["code"] == "status_failed"
    fake.behave(status_fail=False)
    assert (await client.get("/api/artcraft/apps", headers=auth)).status_code == 200
    fake.behave(status_fail=True)
    stale = (await client.get("/api/artcraft/apps", params={"refresh": "true"}, headers=auth)).json()
    assert stale["stale"] is True and stale["error"] and len(stale["apps"]) == 4


async def test_status_timeout(client, auth, fake, monkeypatch):
    monkeypatch.setattr(artcraft, "STATUS_TIMEOUT", 0.3)
    fake.behave(sleep=2)
    r = await client.get("/api/artcraft/apps", headers=auth)
    assert r.status_code == 502 and "초" in r.json()["message"]


def test_version_compare():
    assert artcraft.update_available("0.9.0", "v0.10.0") is True
    assert artcraft.update_available("0.10.0", "v0.10.0") is False
    assert artcraft.update_available("0.12.0", "v0.9.0") is False
    assert artcraft.update_available(None, "v1.0.0") is None and artcraft.update_available("1.0", "nightly") is None


async def test_remote_check_job_cache_throttle_and_partial_failure(client, auth, fake, monkeypatch):
    fake.behave(tags={"alpha": "v1.1.0", "beta": "v0.8.0"})
    r = await client.post("/api/artcraft/remote-check", headers=auth)
    assert r.status_code == 202
    job = await wait_job(client, auth, r.json()["id"])
    assert job["state"] == "succeeded" and "2개" in job["summary"]
    apps = {a["app"]: a for a in (await client.get("/api/artcraft/apps", params={"refresh": "true"}, headers=auth)).json()["apps"]}
    assert apps["alpha"]["latest"]["tag"] == "v1.1.0" and apps["alpha"]["latest"]["update_available"] is True
    assert apps["beta"]["latest"]["update_available"] is False and apps["gamma"]["latest"]["tag"] is None
    # 연타하면 GitHub 한도를 아끼려고 막는다
    again = await client.post("/api/artcraft/remote-check", headers=auth)
    assert again.status_code == 429 and again.json()["code"] == "rate_limited"
    # 전부 실패하면 이전 캐시를 유지하고 오류만 기록한다
    monkeypatch.setattr(artcraft, "REMOTE_MIN_INTERVAL", 0)
    fake.behave(tags={})
    job = await wait_job(client, auth, (await client.post("/api/artcraft/remote-check", headers=auth)).json()["id"])
    assert job["state"] == "failed"
    body = (await client.get("/api/artcraft/apps", headers=auth)).json()
    assert body["remote"]["error"] and {a["app"]: a for a in body["apps"]}["alpha"]["latest"]["tag"] == "v1.1.0"


async def test_auto_remote_check_when_cache_is_empty(client, auth, fake, monkeypatch):
    monkeypatch.setattr(get_settings(), "artcraft_auto_remote_check", True)
    fake.behave(tags={"alpha": "v1.0.0"})
    body = (await client.get("/api/artcraft/apps", headers=auth)).json()
    assert body["remote"]["checking"] is True
    await asyncio.wait_for(next(iter(artcraft.JOBS.values())).task, 10)
    # 시도 직후에는 다시 시작하지 않는다
    again = (await client.get("/api/artcraft/apps", params={"refresh": "true"}, headers=auth)).json()
    assert again["remote"]["checking"] is False and len(artcraft.JOBS) == 1


async def test_smoke_cli_job_keeps_reference_file_and_redacts_output(client, auth, fake):
    r = await client.post("/api/artcraft/smoke", json={"apps": ["beta", "alpha"], "mode": "cli"}, headers=auth)
    assert r.status_code == 202, r.text
    job = await wait_job(client, auth, r.json()["id"])
    smoke_calls = [c["argv"] for c in fake.calls() if c["argv"][0] == "smoke"]
    assert smoke_calls == [["smoke", "alpha", "beta", "--no-gui"]]  # 레지스트리 순서·GUI 제외
    assert job["state"] == "succeeded" and "4/4" in job["summary"]
    assert "abcdef" not in job["output"] and "[REDACTED]" in job["output"]
    # 13앱 기준 기록(latest.json)은 부분 실행이 덮어쓰지 않는다
    assert (fake.root / "smoke" / "results" / "latest.json").read_bytes() == fake.original_latest
    apps = {a["app"]: a for a in (await client.get("/api/artcraft/apps", headers=auth)).json()["apps"]}
    assert apps["alpha"]["smoke"]["source"] == "ttakkari" and apps["alpha"]["smoke"]["total"] == 2
    assert apps["alpha"]["smoke"]["mcp"] == {"ok": True, "tools": 7}
    assert apps["gamma"]["smoke"]["source"] == "artcraft"  # 안 돌린 앱은 기존 기록 그대로


async def test_smoke_failure_is_reported_and_validation(client, auth, fake):
    fake.behave(smoke_fail=["beta"])
    job = await wait_job(client, auth, (await client.post("/api/artcraft/smoke", json={"apps": ["beta"]}, headers=auth)).json()["id"])
    assert job["state"] == "failed" and "실패" in job["summary"] and "3/4" not in job["summary"]
    shown = {a["app"]: a for a in (await client.get("/api/artcraft/apps", headers=auth)).json()["apps"]}["beta"]["smoke"]
    assert shown["passed"] == 1 and shown["total"] == 2 and shown["failed"][0]["test"] == "t2"
    assert "abcdef" not in shown["failed"][0]["evidence"] and "[REDACTED]" in shown["failed"][0]["evidence"]
    assert (await client.post("/api/artcraft/smoke", json={"apps": ["nope"]}, headers=auth)).status_code == 404
    assert (await client.post("/api/artcraft/smoke", json={"apps": ["alpha; rm -rf /"]}, headers=auth)).status_code == 404
    assert (await client.post("/api/artcraft/smoke", json={"mode": "everything"}, headers=auth)).status_code == 422
    assert (await client.post("/api/artcraft/smoke", json={"apps": ["--source"]}, headers=auth)).status_code == 404
    assert not [c for c in fake.calls() if "--source" in c["argv"] or any(";" in x for x in c["argv"])]


async def test_smoke_full_needs_confirm_and_refuses_running_apps(client, auth, fake):
    r = await client.post("/api/artcraft/smoke", json={"apps": ["alpha"], "mode": "full"}, headers=auth)
    assert r.status_code == 428 and r.json()["code"] == "confirm_required"
    fake.behave(running=["alpha"])
    r = await client.post("/api/artcraft/smoke", json={"apps": ["alpha"], "mode": "full", "confirm": True}, headers=auth)
    assert r.status_code == 409 and r.json()["code"] == "app_running"
    fake.behave(running=[])
    r = await client.post("/api/artcraft/smoke", json={"apps": ["alpha"], "mode": "full", "confirm": True}, headers=auth)
    assert r.status_code == 202
    await wait_job(client, auth, r.json()["id"])
    assert ["smoke", "alpha"] in [c["argv"] for c in fake.calls()]  # --no-gui 없음


async def test_one_smoke_at_a_time_and_gui_blocked_during_full_smoke(client, auth, fake):
    fake.behave(smoke_sleep=0.6)
    first = await client.post("/api/artcraft/smoke", json={"apps": ["beta"]}, headers=auth)
    assert first.status_code == 202
    second = await client.post("/api/artcraft/smoke", json={"apps": ["alpha"]}, headers=auth)
    assert second.status_code == 409 and second.json()["code"] == "busy"
    await wait_job(client, auth, first.json()["id"])


async def test_gui_blocked_while_full_smoke_runs(client, auth, fake):
    fake.behave(smoke_sleep=1.0)
    full = await client.post("/api/artcraft/smoke", json={"apps": ["alpha"], "mode": "full", "confirm": True}, headers=auth)
    assert full.status_code == 202
    blocked = await client.post("/api/artcraft/apps/beta/gui", json={"action": "start"}, headers=auth)
    assert blocked.status_code == 409 and blocked.json()["code"] == "busy"
    await wait_job(client, auth, full.json()["id"])


async def test_smoke_timeout_restores_reference_file(client, auth, fake, monkeypatch):
    monkeypatch.setattr(artcraft, "SMOKE_CLI_TIMEOUT", 0.3)
    fake.behave(smoke_sleep=2)
    job = await wait_job(client, auth, (await client.post("/api/artcraft/smoke", json={"apps": ["beta"]}, headers=auth)).json()["id"])
    assert job["state"] == "timeout" and "중단" in job["summary"]
    assert (fake.root / "smoke" / "results" / "latest.json").read_bytes() == fake.original_latest


async def test_gui_start_stop_with_confirmation(client, auth, fake):
    url = "/api/artcraft/apps/alpha/gui"
    r = await client.post(url, json={"action": "stop", "confirm": True}, headers=auth)
    assert r.status_code == 409 and r.json()["code"] == "not_running"
    r = await client.post(url, json={"action": "start"}, headers=auth)
    assert r.status_code == 202
    job = await wait_job(client, auth, r.json()["id"])
    assert job["state"] == "succeeded" and job["apps"] == ["alpha"]
    apps = {a["app"]: a for a in (await client.get("/api/artcraft/apps", headers=auth)).json()["apps"]}
    assert apps["alpha"]["gui"]["running"] is True  # 작업이 끝나면 캐시를 새로 고친다
    assert (await client.post(url, json={"action": "start"}, headers=auth)).json()["code"] == "already_running"
    no = await client.post(url, json={"action": "stop"}, headers=auth)
    assert no.status_code == 428 and no.json()["code"] == "confirm_required"
    ok = await client.post(url, json={"action": "stop", "confirm": True}, headers=auth)
    assert ok.status_code == 202
    assert (await wait_job(client, auth, ok.json()["id"]))["state"] == "succeeded"
    argvs = [c["argv"] for c in fake.calls()]
    assert ["gui", "start", "alpha"] in argvs and ["gui", "stop", "alpha"] in argvs


async def test_gui_rejects_unknown_app_and_action(client, auth, fake):
    assert (await client.post("/api/artcraft/apps/photocraft/gui", json={"action": "start"}, headers=auth)).status_code == 404
    assert (await client.post("/api/artcraft/apps/alpha/gui", json={"action": "kill"}, headers=auth)).status_code == 422
    assert (await client.post("/api/artcraft/apps/..%2Fetc/gui", json={"action": "start"}, headers=auth)).status_code in (404, 422)
    assert not [c for c in fake.calls() if c["argv"][0] == "gui"]


async def test_gui_start_failure_message(client, auth, fake):
    fake.behave(gui_dead=True)
    job = await wait_job(client, auth, (await client.post("/api/artcraft/apps/beta/gui", json={"action": "start"}, headers=auth)).json()["id"])
    assert job["state"] == "failed" and "제어 포트" in job["summary"]


async def test_stop_refuses_listening_but_unmanaged_app(client, auth, fake):
    status = fake.root / "bin" / "artcraft"
    # 포트는 열려 있지만 pid 기록이 없는 앱(사용자가 직접 연 경우)을 흉내 낸다
    status.write_text(status.read_text().replace('"pid": 99 if n in beh.get("running", []) else None',
                                                '"pid": None').replace('"running": n in beh.get("running", [])', '"running": False'))
    fake.behave(running=["beta"])
    r = await client.post("/api/artcraft/apps/beta/gui", json={"action": "stop", "confirm": True}, headers=auth)
    assert r.status_code == 409 and "관제가 켠 앱이 아니" in r.json()["message"]


async def test_bug_details_are_parsed_from_bugs_md(client, auth, fake):
    body = (await client.get("/api/artcraft/bugs", headers=auth)).json()
    assert [b["id"] for b in body["bugs"]] == ["BUG-001", "BUG-002", "BUG-003"]
    assert body["apps"] == ["alpha", "beta", "delta", "gamma"]  # all 은 registry 전체로 풀린다
    one, two, three = body["bugs"]
    assert one["title"] == "alpha scans fonts" and one["apps"] == ["alpha"] and one["repos"] == ["storytold/alpha"]
    assert (one["severity"], one["kind"], one["difficulty"]) == ("S1", "pr", "medium")
    assert one["upstream"] == {"state": "related", "note": "checked 2026-10-18, similar alpha#12"}
    assert one["found"] == "2026-10-18" and one["verified"].startswith("yes")
    assert "time alpha-cli info in.png" in one["sections"]["repro"] and one["sections"]["evidence"].strip("`\n") == "1:36.66 total"
    assert one["sections"]["suggestion"] == "Scan lazily." and one["problems"] == []
    assert two["kind"] == "security" and two["apps"] == ["alpha", "beta", "delta", "gamma"]
    # 형식이 어긋난 항목도 숨기지 않고 사유를 보여 준다
    assert three["severity"] is None and three["kind"] is None and three["upstream"]["state"] == "unchecked"
    joined = " ".join(three["problems"])
    for needle in ("Kind", "Severity", "Upstream", "Found", "storytold/<이름>", "### Notes", "### Repro", "### Evidence"):
        assert needle in joined, needle
    assert body["problems"] == 1 and body["counts"]["candidate"] == 3


async def test_bug_status_flow_history_and_persistence(client, auth, fake):
    put = lambda body, bug="BUG-001": client.put(f"/api/artcraft/bugs/{bug}", json=body, headers=auth)
    r = await put({"status": "selected", "note": " 먼저 볼 것\x00 "})
    assert r.status_code == 200
    bug = r.json()
    assert bug["status"] == "selected" and bug["note"] == "먼저 볼 것" and bug["updated_at"]
    assert [h["status"] for h in bug["history"]] == ["selected"]
    for status in ("in_progress", "reported"):
        assert (await put({"status": status})).json()["status"] == status
    done = (await put({"status": "reported", "url": "https://github.com/storytold/alpha/issues/99"})).json()
    assert done["url"].endswith("/issues/99") and [h["status"] for h in done["history"]] == ["selected", "in_progress", "reported"]
    artcraft.reset_caches()  # 재시작 후에도 파일에서 읽는다
    again = (await client.get("/api/artcraft/bugs", headers=auth)).json()
    assert again["counts"]["reported"] == 1 and again["counts"]["candidate"] == 2
    assert again["bugs"][0]["status"] == "reported" and again["bugs"][0]["url"].endswith("/issues/99")
    for status in ("resolved", "deferred", "excluded", "candidate"):
        assert (await put({"status": status}, "BUG-002")).json()["status"] == status
    # 해결·제외·보류로 정리한 항목은 앱 카드의 BUG 칩에서 빠진다
    await put({"status": "excluded"}, "BUG-002")
    apps = {a["app"]: a for a in (await client.get("/api/artcraft/apps", headers=auth)).json()["apps"]}
    assert apps["beta"]["known_bugs"] == [] and apps["alpha"]["known_bugs"] == ["BUG-001", "BUG-003"]


async def test_legacy_unreported_state_reads_as_candidate(client, auth, fake):
    artcraft.update_state(lambda s: s.setdefault("bugs", {}).update({"BUG-001": {"status": "unreported", "note": "예전"}}))
    bug = (await client.get("/api/artcraft/bugs", headers=auth)).json()["bugs"][0]
    assert bug["status"] == "candidate" and bug["note"] == "예전"


async def test_handoff_text_per_kind_never_authorizes_posting(client, auth, fake):
    bugs = {b["id"]: b for b in (await client.get("/api/artcraft/bugs", headers=auth)).json()["bugs"]}
    pr, sec = bugs["BUG-001"]["handoff"], bugs["BUG-002"]["handoff"]
    for text, bug_id in ((pr, "BUG-001"), (sec, "BUG-002")):
        assert bug_id in text and "storytold/" in text and "BUGS.md" in text
        assert "내가 초안을 확인하고 올리라고 말한 뒤에만" in text and "아무것도 외부로 보내지 마" in text
    assert "alpha" in pr and "재현 요약: time alpha-cli info in.png" in pr and "PR 설명 초안" in pr
    assert "SECURITY.md" in sec and "공개 이슈" in sec and "PR 설명 초안" not in sec
    assert "similar alpha#12" in pr


async def test_bug_validation(client, auth, fake):
    put = lambda body, bug="BUG-001": client.put(f"/api/artcraft/bugs/{bug}", json=body, headers=auth)
    assert (await put({"status": "resolved"}, "BUG-099")).status_code == 404
    assert (await put({"status": "fixed"})).status_code == 422
    assert (await put({"status": "unreported"})).status_code == 422  # 예전 값은 읽기만 호환한다
    assert (await put({"status": "reported", "note": "x" * 501})).status_code == 422
    for bad in ("http://github.com/a/b", "https://evil.example/a/b", "javascript:alert(1)", "https://github.com/a", "https://github.com/a/b c"):
        assert (await put({"status": "reported", "url": bad})).status_code == 422, bad
    assert (await client.put("/api/artcraft/bugs/..", json={"status": "reported"}, headers=auth)).status_code in (404, 405)


async def test_agent_config_snippets(client, auth, fake):
    await client.get("/api/artcraft/apps", headers=auth)  # 도구 수 힌트
    body = (await client.get("/api/artcraft/agent-config", headers=auth)).json()
    alpha = body["apps"][0]
    assert alpha["server"] == "artcraft-alpha" and alpha["tool_count"] == 25
    cfg = json.loads(alpha["snippets"]["json"])["mcpServers"]["artcraft-alpha"]
    assert cfg["args"] == ["mcp", "alpha"] and cfg["command"].endswith("bin/artcraft")
    assert alpha["snippets"]["claude"].startswith("claude mcp add artcraft-alpha -- ")
    assert "[mcp_servers.artcraft-alpha]" in alpha["snippets"]["codex"] and 'args = ["mcp", "alpha"]' in alpha["snippets"]["codex"]
    assert any("ALPHA_NO_FONTS=1" in n for n in alpha["notes"])
    beta = body["apps"][1]
    assert any("도구가 400개" in n for n in beta["notes"])
    assert set(json.loads(body["all"]["json"])["mcpServers"]) == {"artcraft-alpha", "artcraft-beta", "artcraft-gamma", "artcraft-delta"}


async def test_unavailable_repository_returns_503(client, auth, fake, monkeypatch):
    monkeypatch.setattr(get_settings(), "artcraft_root", fake.root / "missing")
    assert (await client.get("/api/artcraft/apps", headers=auth)).status_code in (502, 503)
    assert (await client.get("/api/artcraft/bugs", headers=auth)).status_code == 503
    assert (await client.get("/api/artcraft/agent-config", headers=auth)).status_code == 503


def test_job_cannot_be_found_after_restart(fake):
    with pytest.raises(artcraft.ArtcraftError) as e:
        artcraft.get_job("nope")
    assert e.value.status == 404


def test_fake_script_is_executable_and_state_file_is_private(fake):
    assert os.access(fake.root / "bin" / "artcraft", os.X_OK)
    artcraft.update_state(lambda s: s.setdefault("x", 1))
    mode = stat.S_IMODE(artcraft._state_path().parent.stat().st_mode)
    assert mode == 0o700
