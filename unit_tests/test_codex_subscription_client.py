"""Subscription-only usage RPC: no inference, secret output, or project/session mutation."""
import importlib
import json
import sys

import pytest


def client_module():
    name = "ttakkari.runs.codex_subscription"
    assert importlib.util.find_spec(name) is not None, "subscription client is not implemented"
    return importlib.import_module(name)


def test_rpc_selects_codex_bucket_and_classifies_by_duration():
    c = client_module()
    body = {"ordinaryUsageAllowed": True, "rateLimitResetCredits": {"availableCount": 2},
            "rateLimits": {"secondary": {"usedPercent": 99, "windowDurationMins": 10080}},
            "rateLimitsByLimitId": {"codex": {"planType": "plus", "primary": {
                "usedPercent": 0.2, "windowDurationMins": 10080, "resetsAt": 2000000000},
                "secondary": {"usedPercent": 11, "windowDurationMins": 300, "resetsAt": 1900000000}}}}
    u = c.parse_limits(body, now=123)
    assert u.plan == "plus" and u.updated_at == 123 and u.source == "api"
    assert u.ordinary_usage_allowed is True and u.reset_credits.available == 2
    assert [(w.key, w.percent, w.resets_at) for w in u.windows] == [
        ("five_hour", 11, 1900000000), ("seven_day", 1, 2000000000)]


@pytest.mark.parametrize("window", [
    {"usedPercent": 0, "resetsAt": 123},
    {"usedPercent": True, "windowDurationMins": 10080},
    {"usedPercent": -1, "windowDurationMins": 10080},
    {"usedPercent": float("nan"), "windowDurationMins": 10080},
    {"usedPercent": 0, "windowDurationMins": 60},
])
def test_unknown_or_malformed_windows_never_become_weekly_zero(window):
    c = client_module()
    assert not c.parse_limits({"rateLimits": {"primary": window}}).windows


@pytest.mark.parametrize("body", [
    {"rateLimits": "invalid"},
    {"rateLimits": {"primary": {"usedPercent": 0, "windowDurationMins": []}}},
    {"rateLimits": {"limitId": "another_bucket", "primary": {"usedPercent": 0, "windowDurationMins": 10080}}},
    {"rateLimits": {}, "rateLimitResetCredits": "invalid"},
])
def test_invalid_bucket_shapes_and_other_metered_buckets_are_not_activation_evidence(body):
    assert not client_module().parse_limits(body).windows


def test_exec_has_isolation_and_subscription_guards():
    c = client_module()
    cmd = c.activation_command("codex", "gpt-6-luna")
    for flag in ("--ephemeral", "--ignore-user-config", "--ignore-rules", "--skip-git-repo-check", "--json"):
        assert flag in cmd
    assert 'forced_login_method="chatgpt"' in cmd and 'model_provider="openai"' in cmd
    assert cmd[cmd.index("-s") + 1] == "read-only"
    assert "resume" not in cmd and cmd[-1] == "Reply only OK. Do not use tools."
    env = c.child_env("/selected-account", {"PATH": "/bin", "CODEX_HOME": "/wrong",
                       "OPENAI_API_KEY": "paid", "CODEX_API_KEY": "paid", "OPENAI_BASE_URL": "proxy",
                       "TTAKKARI_JWT_SECRET": "secret", "CODEX_THREAD_ID": "existing"})
    assert env == {"PATH": "/bin", "CODEX_HOME": "/selected-account"}


async def test_read_only_protocol_does_not_start_thread_or_turn(tmp_path):
    c = client_module()
    trace = tmp_path / "trace.jsonl"
    fake = tmp_path / "fake.py"
    fake.write_text('''import sys, json
trace = open(sys.argv[1], 'a')
for line in sys.stdin:
    msg = json.loads(line)
    trace.write(json.dumps(msg) + '\\n'); trace.flush()
    method = msg['method']
    if 'id' not in msg: continue
    if method == 'initialize': result = {}
    elif method == 'account/read': result = {'account': {'type': 'chatgpt', 'planType': 'plus'}}
    elif method == 'account/rateLimits/read': result = {'rateLimits': {'secondary': {
        'usedPercent': 12, 'windowDurationMins': 10080, 'resetsAt': 2000000000}}}
    else: raise AssertionError(method)
    print(json.dumps({'id': msg['id'], 'result': result}), flush=True)
''')
    async with c.RpcClient([sys.executable, str(fake), str(trace)], env={}, cwd=tmp_path) as rpc:
        account = await rpc.call("account/read", {"refreshToken": True})
        limits = await rpc.call("account/rateLimits/read")
        assert account["account"]["type"] == "chatgpt"
        assert c.parse_limits(limits).windows[0].percent == 12
        with pytest.raises(ValueError, match="read-only"):
            await rpc.call("turn/start", {})
    assert [json.loads(x)["method"] for x in trace.read_text().splitlines()] == [
        "initialize", "initialized", "account/read", "account/rateLimits/read"]


def test_rpc_errors_have_distinct_safe_codes():
    c = client_module()
    for msg, expected in [("401 unauthorized token=secret", "auth_error"),
                          ("429 usage limit secret", "rate_limit"),
                          ("connection reset secret", "transport_error")]:
        error = c.RpcError.from_response({"code": -32000, "message": msg})
        assert error.kind == expected and "secret" not in str(error)


async def test_usage_fetch_uses_account_read_and_never_a_model(monkeypatch):
    c = client_module()
    assert hasattr(c, "read_usage"), "official usage read is not implemented"
    seen = []

    class FakeRpc:
        def __init__(self, command, **kwargs):
            assert command[1:4] == ["app-server", "--listen", "stdio://"]
        async def __aenter__(self):
            return self
        async def __aexit__(self, *exc):
            pass
        async def call(self, method, params=None):
            seen.append((method, params))
            return {"account": {"type": "chatgpt", "planType": "plus"}} if method == "account/read" else {
                "rateLimits": {"secondary": {"usedPercent": 0, "windowDurationMins": 10080, "resetsAt": 123}}}

    monkeypatch.setattr(c, "RpcClient", FakeRpc)
    u = await c.read_usage("/selected", binary="fake")
    assert u.login == "ok" and u.plan == "plus" and u.windows[0].resets_at == 123
    assert seen == [("account/read", {"refreshToken": False}),
                    ("account/rateLimits/read", {"excludeResetCreditDetails": True})]


async def test_legacy_login_refresh_no_longer_launches_exec(monkeypatch):
    c = client_module()
    from ttakkari.runs import usage
    called = []

    async def read(*args, **kwargs):
        called.append(args)
        return usage.Usage(login="ok")

    async def forbidden(*args, **kwargs):
        pytest.fail("usage login refresh must never start inference")

    monkeypatch.setattr(c, "read_usage", read, raising=False)
    monkeypatch.setattr(usage.asyncio, "create_subprocess_exec", forbidden)
    usage._codex_refresh_at.clear()
    assert await usage._refresh_codex_login("/selected") is None
    assert called == [("/selected",)]


async def test_startup_failure_preserves_safe_cause_and_exit_code(tmp_path):
    c = client_module()
    fake = tmp_path / "failure.py"
    fake.write_text("import sys; print('Operation not permitted token=secret', file=sys.stderr); sys.exit(161)")
    with pytest.raises(c.RpcError, match="sandbox_denied.*161") as exc:
        async with c.RpcClient([sys.executable, str(fake)], env={}, cwd=tmp_path):
            pass
    assert "secret" not in str(exc.value)


async def test_activation_requires_subscription_and_never_falls_back_to_api(monkeypatch):
    c = client_module()
    assert hasattr(c, "activate_subscription"), "subscription activation is not implemented"

    async def no_subscription(*args, **kwargs):
        from ttakkari.runs.usage import Usage
        return Usage(error="API key login", login="missing")

    async def forbidden(*args, **kwargs):
        pytest.fail("API key account must not dispatch")

    monkeypatch.setattr(c, "read_usage", no_subscription)
    monkeypatch.setattr(c.asyncio, "create_subprocess_exec", forbidden)
    assert (await c.activate_subscription("/selected"))["status"] == "auth_error"


async def test_activation_is_ephemeral_in_temp_directory_and_saves_only_token_counts(monkeypatch, tmp_path):
    c = client_module()
    assert hasattr(c, "activate_subscription"), "subscription activation is not implemented"
    from ttakkari.runs.usage import Usage, Window
    seen = []

    async def subscribed(*args, **kwargs):
        return Usage(login="ok", plan="plus", windows=[Window("seven_day", "주간", 0, 123)], ordinary_usage_allowed=True)

    class Process:
        returncode = 0
        async def communicate(self):
            return b'{"type":"turn.completed","usage":{"input_tokens":8,"output_tokens":1},"secret":"hidden"}\n', b""

    async def start(*args, **kwargs):
        seen.append((args, kwargs))
        assert kwargs["cwd"].is_dir() and str(kwargs["cwd"]).find("ttk-codex-activate-") >= 0
        assert "--ephemeral" in args and "--ignore-user-config" in args
        return Process()

    monkeypatch.setattr(c, "read_usage", subscribed)
    monkeypatch.setattr(c.asyncio, "create_subprocess_exec", start)
    result = await c.activate_subscription("/selected")
    assert result == {"status": "succeeded", "tokens": {"input_tokens": 8, "output_tokens": 1}}
    assert "hidden" not in str(result) and len(seen) == 1
    assert not seen[0][1]["cwd"].exists()


def test_dashboard_reads_reuse_codex_observation_for_twelve_hours_without_reset_polling():
    import time
    from uuid import uuid4

    from ttakkari.runs import usage
    aid, now = uuid4(), time.time()
    u = usage.Usage(windows=[usage.Window("seven_day", "주간", 100, int(now - 10))],
                    source="api", updated_at=now - 3600)
    usage._cache[aid] = (now - 3600, u)
    try:
        assert usage._ready_value(aid, "codex", None, False) is u
        assert usage._ready_value(aid, "codex", None, True) is None
    finally:
        usage.forget(aid)


def test_live_observation_failure_keeps_prior_windows_and_exposes_error():
    from uuid import uuid4

    from ttakkari.runs import usage
    assert hasattr(usage, "publish_live_observation"), "live subscription observations are not published"
    aid = uuid4()
    old = usage.Usage(windows=[usage.Window("seven_day", "주간", 20, 1000)], source="api", updated_at=500,
                      ordinary_usage_allowed=True)
    usage.publish_live_observation(aid, old)
    usage.publish_live_observation(aid, usage.Usage(error="auth_error", login="missing"))
    result = usage.cached(aid)
    assert result.windows == old.windows and result.updated_at == 500
    assert result.error == "auth_error" and result.login == "missing" and result.ordinary_usage_allowed is None
    usage.forget(aid)
