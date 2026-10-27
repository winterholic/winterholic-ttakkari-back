"""Account quota comes from Copilot's read-only RPC, never inference or Codex."""
import time
from uuid import uuid4

import pytest

from ttakkari.runs import usage


def snapshot(remaining=72.5, **extra):
    return {"entitlementRequests": 300, "usedRequests": 82, "remainingPercentage": remaining,
            "isUnlimitedEntitlement": False, "resetDate": "2030-11-01T00:00:00Z", **extra}


def test_copilot_quota_uses_provider_percent_and_monthly_reset():
    from ttakkari.runs.copilot_usage import parse_quota
    u = parse_quota({"quotaSnapshots": {"premium_interactions": snapshot()}}, now=1793000000)
    assert u.error is None
    assert u.source == "copilot_sdk"
    assert u.updated_at == 1793000000
    assert len(u.windows) == 1
    w = u.windows[0]
    assert w.key == "copilot_premium_interactions"
    assert w.percent == 28 and w.remaining_percent == 72.5
    assert w.resets_at == 1919721600


def test_live_quota_with_runtime_fetch_timestamp_is_not_hidden_as_expired():
    # Observed on CLI 1.0.103 + SDK 1.0.26: resetDate was a few seconds
    # before the new process returned its fresh account quota (#2619).
    from ttakkari.runs import profiles
    from ttakkari.runs.copilot_usage import parse_quota
    aid = uuid4()
    result = parse_quota({'quotaSnapshots': {'premium_interactions': snapshot(
        98.1, resetDate='2026-10-27T22:16:18Z')}}, now=1793139382.10624)
    assert result.windows[0].resets_at is None  # no invented next reset
    assert result.windows[0].remaining_percent == 98.1
    usage.seed(aid, result, refresh=True)
    p = profiles.Profile(aid, 'copilot', 'copilot', model='auto')
    assert profiles._usage_headroom(p, now=1793139383)[0] == 98.1


def test_exhausted_live_snapshot_without_future_reset_keeps_short_cooldown():
    from ttakkari.runs import profiles
    from ttakkari.runs.copilot_usage import parse_quota
    aid = uuid4()
    now = time.time()
    result = parse_quota({'quotaSnapshots': {'premium_interactions': snapshot(
        0, resetDate='2026-01-01T00:00:00Z')}}, now=now)
    usage.seed(aid, result, refresh=True)
    p = profiles.Profile(aid, 'copilot', 'copilot', model='auto')
    assert profiles.usage_limit_until(p, now=now) == now+90


@pytest.mark.parametrize("body", [{}, {"quotaSnapshots": {}}, {"quotaSnapshots": {
    "premium_interactions": {"usedRequests": 0, "entitlementRequests": 300}}}])
def test_absent_percentage_never_becomes_zero_usage(body):
    from ttakkari.runs.copilot_usage import parse_quota
    u = parse_quota(body)
    assert not u.windows
    assert u.error and "미제공" in u.error


def test_zero_remaining_is_a_real_measurement():
    from ttakkari.runs.copilot_usage import parse_quota
    u = parse_quota({"quotaSnapshots": {"premium_interactions": snapshot(0)}})
    assert u.windows[0].percent == 100
    assert u.windows[0].remaining_percent == 0


def test_unlimited_entitlement_is_not_a_zero_percent_window():
    from ttakkari.runs.copilot_usage import parse_quota
    u = parse_quota({"quotaSnapshots": {"chat": snapshot(100, entitlementRequests=-1,
                                                            isUnlimitedEntitlement=True)}})
    assert not u.windows
    assert u.error is None
    assert u.quota_unlimited
    assert u.updated_at is not None


@pytest.mark.parametrize("remaining", [True, -1, 101, float("nan"), float("inf"), "50"])
def test_invalid_percentages_are_not_published(remaining):
    from ttakkari.runs.copilot_usage import parse_quota
    u = parse_quota({"quotaSnapshots": {"premium_interactions": snapshot(remaining)}})
    assert not u.windows
    assert u.error


async def test_registered_engine_calls_copilot_quota_reader_not_codex(monkeypatch):
    from ttakkari.runs import copilot_usage
    calls = []

    async def fetch(account_id, engine, config_dir):
        calls.append((account_id, engine, config_dir))
        return usage.Usage(source="copilot_sdk", updated_at=time.time(), windows=[
            usage.Window("copilot_premium_interactions", "Copilot 월간", 25, 1919721600, 75)])

    monkeypatch.setattr(copilot_usage, "fetch_usage", fetch)
    for engine in ("opencode", "copilot"):
        aid = uuid4()
        u = await usage._fetch_live(aid, engine, "/managed-account")
        assert u.source == "copilot_sdk" and u.windows[0].remaining_percent == 75
        assert calls[-1] == (aid, engine, "/managed-account")


async def test_copilot_observation_reaches_persistence_sink(monkeypatch):
    from ttakkari.runs import copilot_usage
    seen = []
    sample = usage.Usage(source="copilot_sdk", updated_at=time.time(), windows=[
        usage.Window("copilot_premium_interactions", "Copilot 월간", 25, 1919721600, 75)])

    async def fetch(*_args):
        return sample

    monkeypatch.setattr(copilot_usage, "fetch_usage", fetch)
    monkeypatch.setattr(usage, "_sink", lambda aid, u: seen.append((aid, u)))
    aid = uuid4()
    await usage._fetch_live(aid, "opencode", "/managed-account")
    assert seen == [(aid, sample)]


def test_unlimited_is_successful_non_numeric_entitlement():
    from ttakkari.runs.copilot_usage import parse_quota
    u = parse_quota({'quotaSnapshots': {'chat': snapshot(isUnlimitedEntitlement=True)}})
    assert u.error is None
    assert u.quota_unlimited == {'copilot_chat': 'Copilot 채팅'}
    assert not u.windows


async def test_quota_rpc_uses_selected_opencode_token_only(tmp_path, monkeypatch):
    import json

    from ttakkari import opencode_accounts
    from ttakkari.runs import copilot_usage
    aid = uuid4()
    profile = opencode_accounts.allocate(tmp_path, aid)
    profile.auth_json_path.parent.mkdir(mode=0o700, exist_ok=True)
    profile.auth_json_path.write_text(json.dumps({'github-copilot': {'type': 'oauth', 'refresh': 'synthetic-only-token'}}))
    profile.auth_json_path.chmod(0o600)
    seen = {}

    async def rpc(*, env, token, use_logged_in_user):
        seen.update(env=env, token=token, logged=use_logged_in_user)
        assert env['HOME'] != str(tmp_path)
        assert env['COPILOT_HOME'].startswith(env['HOME'])
        assert env['GH_CONFIG_DIR'].startswith(env['HOME'])
        return {'quotaSnapshots': {'premium_interactions': snapshot()}}

    monkeypatch.setattr(copilot_usage, '_quota_rpc', rpc)
    monkeypatch.setenv('GH_TOKEN', 'ambient-should-not-be-used')
    monkeypatch.setenv('COPILOT_GITHUB_TOKEN', 'ambient-should-not-be-used')
    u = await copilot_usage.fetch_usage(aid, 'opencode', str(profile.home))
    assert not u.error
    assert seen['token'] == 'synthetic-only-token' and seen['logged'] is False
    assert 'GH_TOKEN' not in seen['env'] and 'COPILOT_GITHUB_TOKEN' not in seen['env']
    assert 'synthetic-only-token' not in repr(u)


async def test_missing_or_wrong_account_never_queries_default_quota(tmp_path, monkeypatch):
    from ttakkari import opencode_accounts
    from ttakkari.runs import copilot_usage
    profile = opencode_accounts.allocate(tmp_path, uuid4())

    async def forbidden(**kwargs):
        pytest.fail('must not borrow host account')

    monkeypatch.setattr(copilot_usage, '_quota_rpc', forbidden)
    for aid, home in ((uuid4(), str(profile.home)), (profile.account_id, str(profile.home)), (uuid4(), None)):
        u = await copilot_usage.fetch_usage(aid, 'opencode', home)
        assert u.error and not u.windows


async def test_rpc_failure_is_sanitized_and_cached(monkeypatch):
    from ttakkari.runs import copilot_usage
    calls = []

    async def failure(**kwargs):
        calls.append(1)
        raise RuntimeError('token synthetic-secret provider-body')

    monkeypatch.setattr(copilot_usage, '_quota_rpc', failure)
    aid = uuid4()
    u = await usage.usage_for(aid, 'copilot', None)
    again = await usage.usage_for(aid, 'copilot', None)
    assert 'synthetic-secret' not in repr(u)
    assert u.error and u.updated_at is None and not u.windows
    assert again == u and len(calls) == 1


async def test_sdk_timeout_always_force_stops_child(monkeypatch):
    import asyncio
    from types import SimpleNamespace

    from ttakkari.runs import copilot_usage
    stopped = []

    class Client:
        def __init__(self, **kwargs):
            self.rpc = SimpleNamespace(account=SimpleNamespace(get_quota=self.get_quota))
        async def start(self):
            await asyncio.sleep(20)
        async def get_quota(self, request):
            pytest.fail('start timed out')
        async def force_stop(self):
            stopped.append(True)

    monkeypatch.setattr(copilot_usage, '_new_client', lambda **kwargs: Client(**kwargs))
    monkeypatch.setattr(copilot_usage, 'RPC_TIMEOUT', 0.01)
    with pytest.raises(TimeoutError):
        await copilot_usage._quota_rpc(env={}, token=None, use_logged_in_user=True)
    assert stopped == [True]


async def test_refresh_failure_preserves_observation_and_marks_stale(monkeypatch):
    from ttakkari.runs import account_status, copilot_usage
    aid = uuid4()
    old = usage.Usage(source='copilot_sdk', updated_at=time.time(), windows=[
        usage.Window('copilot_premium_interactions', 'Copilot 월간', 100, 1919721600, 0)])
    results = iter((old, usage.Usage(source='copilot_sdk', error='조회 실패')))

    async def fetch(*args):
        return next(results)

    monkeypatch.setattr(copilot_usage, 'fetch_usage', fetch)
    await usage.usage_for(aid, 'copilot', None)
    u = await usage.usage_for(aid, 'copilot', None, refresh=True)
    assert u.windows == old.windows and u.updated_at == old.updated_at
    assert u.error
    view = account_status._usage(aid, time.time(), engine='copilot')
    assert view['stale'] is True
    assert view['model_quotas'][0]['state'] == 'stale'


def test_numeric_and_unlimited_quotas_reach_api_and_mcp(monkeypatch):
    from ttakkari.api import accounts
    from ttakkari.models import EngineAccount
    from ttakkari.runs import account_status, profiles
    aid = uuid4()
    sample = usage.Usage(source='copilot_sdk', updated_at=time.time(), windows=[
        usage.Window('copilot_premium_interactions', 'Copilot 월간', 100, 1919721600, 0)],
        quota_unlimited={'copilot_chat': 'Copilot 채팅'})
    monkeypatch.setattr(profiles, 'identity', lambda *_: ('unknown', 'Copilot'))
    usage._cache[aid] = (time.time(), sample)
    a = EngineAccount(id=aid, name='OC', engine='opencode', config_dir=None, enabled=True, priority=0,
                      max_used_percent=100, pool='personal', work_enabled=False)
    out = accounts._usage_out(0, a, {}, sample)
    assert out.remaining_to_limit_percent == 0
    assert out.quota_unlimited == sample.quota_unlimited
    assert out.windows[0].remaining_percent == 0 and out.windows[0].state == 'current'
    mcp = account_status._usage(aid, time.time(), engine='opencode')
    assert mcp['quota_unlimited'] == sample.quota_unlimited
    assert mcp['model_quotas'][0]['remaining_percent'] == 0


def test_quotas_roundtrip_storage_without_numeric_unlimited():
    from ttakkari.runs import usage_store
    sample = usage.Usage(source='copilot_sdk', windows=[
        usage.Window('copilot_premium_interactions', 'Copilot 月', 35, 1919721600, 65)],
        quota_unlimited={'copilot_chat': 'Copilot 채팅'})
    payload = usage_store._window_payload(sample)
    assert payload[-1] == {'key': 'copilot_chat', 'label': 'Copilot 채팅', 'unlimited': True}
    assert 'percent' not in payload[-1]
    windows, unlimited = usage_store._decode_windows(payload)
    assert windows == sample.windows and unlimited == sample.quota_unlimited


async def test_cache_does_not_cross_account_root_or_engine(monkeypatch):
    from ttakkari.runs import copilot_usage
    aid = uuid4()
    calls = []

    async def fetch(account_id, engine, root):
        calls.append((engine, root))
        return usage.Usage(source='copilot_sdk', updated_at=time.time(), windows=[
            usage.Window('copilot_premium_interactions', 'Copilot 월간', len(calls), 1919721600, 99)])

    monkeypatch.setattr(copilot_usage, 'fetch_usage', fetch)
    await usage.usage_for(aid, 'opencode', '/account-one')
    await usage.usage_for(aid, 'opencode', '/account-two')
    await usage.usage_for(aid, 'copilot', '/account-two')
    assert calls == [('opencode', '/account-one'), ('opencode', '/account-two'), ('copilot', '/account-two')]


def test_copilot_source_is_classified_in_account_quota_inventory():
    from scripts.platform_v2_inventory import classify_source_file
    assert classify_source_file('src/ttakkari/runs/copilot_usage.py') == 'core.accounts_quota'


async def test_success_rpc_never_creates_a_model_session(monkeypatch):
    from types import SimpleNamespace

    from ttakkari.runs import copilot_usage
    pytest.importorskip('copilot')
    calls = []

    class Client:
        def __init__(self):
            self.rpc = SimpleNamespace(account=SimpleNamespace(get_quota=self.get_quota))
        async def start(self):
            calls.append('start')
        async def get_quota(self, request):
            calls.append(request.to_dict())
            return SimpleNamespace(to_dict=lambda: {'quotaSnapshots': {}})
        async def force_stop(self):
            calls.append('stop')
        async def create_session(self, *args, **kwargs):
            pytest.fail('quota read must never create a session')

    monkeypatch.setattr(copilot_usage, '_new_client', lambda **kwargs: Client())
    assert await copilot_usage._quota_rpc(env={}, token='test-token', use_logged_in_user=False) == {'quotaSnapshots': {}}
    assert calls == ['start', {'gitHubToken': 'test-token'}, 'stop']


async def test_cancelled_quota_rpc_cleans_up_and_propagates(monkeypatch):
    import asyncio

    from ttakkari.runs import copilot_usage
    started, stopped = asyncio.Event(), asyncio.Event()

    class Client:
        async def start(self):
            started.set()
            await asyncio.Event().wait()
        async def force_stop(self):
            stopped.set()

    monkeypatch.setattr(copilot_usage, '_new_client', lambda **kwargs: Client())
    t = asyncio.create_task(copilot_usage._quota_rpc(env={}, token=None, use_logged_in_user=False))
    await started.wait()
    t.cancel()
    with pytest.raises(asyncio.CancelledError):
        await t
    assert stopped.is_set()
