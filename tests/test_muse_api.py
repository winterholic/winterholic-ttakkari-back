"""Disposable DB API tests. No Muse credential, model or operating runtime is used."""
from helpers import run_to_end


async def test_muse_account_list_checks_login_without_usage_windows(client, auth, monkeypatch):
    from ttakkari.runs import usage

    async def authenticated(_config_dir):
        return usage.muse_usage_from_probe({'account': {'state': 'accountLogin'}, 'usage': {}})

    monkeypatch.setattr(usage, '_fetch_muse', authenticated)
    created = await client.post('/api/accounts', headers=auth,
        json={'name': 'Logged in Muse', 'engine': 'muse'})
    account_id = created.json()['id']
    rows = (await client.get('/api/accounts', headers=auth)).json()
    row = next(row for row in rows if row['id'] == account_id)
    assert row['login'] == 'ok'
    assert row['login_command'] is None
    # No quota snapshot is needed to establish authentication.
    quota = (await client.get('/api/accounts/usage', headers=auth)).json()
    quota_row = next(row for row in quota if row['id'] == account_id)
    assert quota_row['login'] == 'ok'
    assert {w['state'] for w in quota_row['windows']} == {'unavailable'}
    assert quota_row['updated_at'] is None


async def test_muse_account_list_preserves_logged_out_state(client, auth, monkeypatch):
    from ttakkari.runs import usage

    async def logged_out(_config_dir):
        return usage.muse_usage_from_probe({'account': {'state': 'loggedOut'}, 'usage': {}})

    monkeypatch.setattr(usage, '_fetch_muse', logged_out)
    created = await client.post('/api/accounts', headers=auth,
        json={'name': 'Logged out Muse', 'engine': 'muse'})
    rows = (await client.get('/api/accounts', headers=auth)).json()
    row = next(row for row in rows if row['id'] == created.json()['id'])
    assert row['login'] == 'missing'
    assert row['login_command']


async def test_muse_profile_change_discards_previous_login(client, auth, monkeypatch, make_dir):
    from ttakkari.runs import usage

    async def lookup(config_dir):
        return usage.muse_usage_from_probe({
            'account': {'state': 'loggedOut' if config_dir else 'accountLogin'}, 'usage': {}})

    monkeypatch.setattr(usage, '_fetch_muse', lookup)
    created = await client.post('/api/accounts', headers=auth,
        json={'name': 'Changed Muse', 'engine': 'muse'})
    account_id = created.json()['id']
    await client.get('/api/accounts', headers=auth)
    changed = await client.patch(f'/api/accounts/{account_id}', headers=auth,
        json={'config_dir': str(make_dir())})
    assert changed.json()['login'] != 'ok'
    rows = (await client.get('/api/accounts', headers=auth)).json()
    row = next(row for row in rows if row['id'] == account_id)
    assert row['login'] == 'missing'
    assert row['login_command']


async def test_muse_account_usage_reports_execution_disabled_when_explicitly_disabled(client, auth, make_dir, monkeypatch):
    from ttakkari.config import get_settings

    monkeypatch.setattr(get_settings(), "muse_enabled", False)
    profile = make_dir()
    created = await client.post('/api/accounts', headers=auth,
        json={'name': 'Muse fixture', 'engine': 'muse', 'config_dir': str(profile)})
    assert created.status_code == 201
    assert 'XDG_CONFIG_HOME=' in created.json()['login_command']
    rows = (await client.get('/api/accounts/usage', headers=auth)).json()
    row = next(row for row in rows if row['engine'] == 'muse')
    assert row['execution_enabled'] is False
    assert {w['state'] for w in row['windows']} == {'unavailable'} and row['updated_at'] is None
    assert row['error']


def test_verified_muse_execution_and_auto_routing_are_enabled_by_default():
    from ttakkari.config import Settings
    from ttakkari.runs import worker_routing

    assert Settings(_env_file=None).muse_enabled is True
    assert Settings(_env_file=None).muse_auto_routing is True
    assert worker_routing.MUSE_AUTO_ROUTING_ACCEPTED is True


def test_muse_auto_routing_environment_opt_out(monkeypatch):
    from ttakkari.config import Settings

    monkeypatch.setenv("TTAKKARI_MUSE_AUTO_ROUTING", "false")
    settings = Settings(_env_file=None)
    assert settings.muse_enabled is True
    assert settings.muse_auto_routing is False


async def test_muse_account_usage_reports_execution_enabled_by_default(client, auth):
    created = await client.post('/api/accounts', headers=auth, json={'name': 'Muse fixture', 'engine': 'muse'})
    rows = (await client.get('/api/accounts/usage', headers=auth)).json()
    row = next(row for row in rows if row['id'] == created.json()['id'])
    assert row['execution_enabled'] is True


async def test_non_coding_model_is_rejected_before_session_creation(client, auth, new_session):
    ws, _sess = await new_session()
    response = await client.post('/api/sessions', headers=auth,
        json={'workspace_id': ws['id'], 'engine': 'muse', 'model': 'sam-3.1'})
    assert response.status_code == 422


async def test_explicitly_disabled_muse_run_fails_without_mock_success(client, auth, new_session, monkeypatch):
    from ttakkari.config import get_settings

    monkeypatch.setattr(get_settings(), "muse_enabled", False)
    await client.post('/api/accounts', headers=auth, json={'name': 'Muse fixture', 'engine': 'muse'})
    _ws, sess = await new_session(engine='muse')
    done = await run_to_end(client, auth, sess['id'], 'Do not execute a model.')
    assert done['status'] == 'failed'
    assert 'disabled' in done['error']
    assert done['result_text'] is None


async def test_muse_observation_survives_empty_read_and_database_reload(client, auth, monkeypatch):
    import time
    from uuid import UUID

    from ttakkari.db import sessionmaker
    from ttakkari.runs import usage, usage_store

    async def empty(_config_dir):
        return usage.Usage(login='ok', error='새 관측값 없음')

    monkeypatch.setattr(usage, '_fetch_muse', empty)
    created = await client.post('/api/accounts', headers=auth, json={'name': 'Quota Muse', 'engine': 'muse'})
    aid = UUID(created.json()['id'])
    observed_at = time.time() - 600
    data = usage.Usage(windows=[usage.Window('five_hour', '5시간', 35, int(time.time()+3600))],
                       source='muse_cli', updated_at=observed_at, plan='Power Usage', login='ok')
    usage_store.install()
    usage.remember_observation(aid, data.as_dict())
    await usage_store.flush()
    usage.clear_cache()
    async with sessionmaker()() as db:
        await usage_store.load_latest(db)
    rows = (await client.get('/api/accounts/usage?refresh=true', headers=auth)).json()
    row = next(row for row in rows if row['id'] == str(aid))
    assert row['windows'][0]['percent'] == 35
    assert abs(row['updated_at'] - observed_at) < .001
    assert row['source'] == 'muse_cli' and row['plan'] == 'Power Usage'
    assert row['error'] == '새 관측값 없음'


async def _cached_row(client, auth, name, observed):
    """Seed the account's cache with `observed` (a Usage) and read it back through the API."""
    from uuid import UUID

    from ttakkari.runs import usage

    created = await client.post('/api/accounts', headers=auth, json={'name': name, 'engine': 'muse'})
    aid = UUID(created.json()['id'])
    if observed is not None:
        usage.seed(aid, observed, refresh=True)
    rows = (await client.get('/api/accounts/usage/cached', headers=auth)).json()
    return next(row for row in rows if row['id'] == str(aid))


def _states(row):
    return {w['key']: w['state'] for w in row['windows']}


async def test_muse_window_states_for_current_failed_reset_and_partial_observations(client, auth):
    import time

    from ttakkari.runs import usage

    now = time.time()

    def obs(five=(10, 3600), week=(20, 86400), age=30, error=None):
        windows = [usage.Window(k, usage.label_of(k), p, int(now + r))
                   for k, (p, r) in (('five_hour', five), ('seven_day', week)) if p is not None]
        return usage.Usage(windows=windows, source='muse_cli', updated_at=now - age, login='ok', error=error)

    current = await _cached_row(client, auth, 'M current', obs())
    assert _states(current) == {'five_hour': 'current', 'seven_day': 'current'}
    assert current['stale'] is False and current['remaining_to_limit_percent'] == 80

    failed = await _cached_row(client, auth, 'M failed refresh', obs(error='Muse /usage unavailable'))
    assert _states(failed) == {'five_hour': 'stale', 'seven_day': 'stale'}
    assert failed['stale'] is True and failed['remaining_to_limit_percent'] is None

    reset = await _cached_row(client, auth, 'M idle after reset', obs(five=(100, -3600), week=(33, 86400), age=36000,
                                                                    error='Muse /usage unavailable'))
    assert _states(reset) == {'five_hour': 'unavailable', 'seven_day': 'stale'}
    five = next(w for w in reset['windows'] if w['key'] == 'five_hour')
    assert five['percent'] == 0 and five['resets_at'] is not None, 'a placeholder, not a measurement of 0%'
    assert reset['remaining_to_limit_percent'] is None
    assert '5시간 창이 초기화된 뒤' in reset['error']

    partial = await _cached_row(client, auth, 'M partial', obs(week=(None, 0)))
    assert _states(partial) == {'five_hour': 'stale', 'seven_day': 'unavailable'}
    assert partial['remaining_to_limit_percent'] is None

    never = await _cached_row(client, auth, 'M never observed', None)
    assert _states(never) == {'five_hour': 'unavailable', 'seven_day': 'unavailable'}
    assert never['updated_at'] is None and never['remaining_to_limit_percent'] is None


async def test_muse_both_windows_reset_without_new_observation_are_both_unavailable(client, auth):
    import time

    from ttakkari.runs import usage

    now = time.time()
    both = usage.Usage(windows=[usage.Window('five_hour', '5시간', 100, int(now - 60)),
                                usage.Window('seven_day', '주간', 100, int(now - 30))],
                       source='muse_cli', updated_at=now - 90000, login='ok', error='Muse /usage unavailable')
    row = await _cached_row(client, auth, 'M all reset', both)
    assert _states(row) == {'five_hour': 'unavailable', 'seven_day': 'unavailable'}
    assert row['remaining_to_limit_percent'] is None and row['stale'] is True


async def test_account_usage_tool_reports_exploration_eligibility_for_idle_muse(client, auth, make_dir):
    import time
    from uuid import UUID

    from test_orchestrator import _orch, _session

    from ttakkari.orchestrator.runtime import issue_token
    from ttakkari.runs import usage

    created = await client.post('/api/accounts', headers=auth, json={'name': 'Idle Muse', 'engine': 'muse'})
    aid = UUID(created.json()['id'])
    now = time.time()
    usage.seed(aid, usage.Usage(windows=[usage.Window('five_hour', '5h', 100, int(now - 60)),
                                         usage.Window('seven_day', '7d', 33, int(now + 86400))],
                                source='muse_cli', updated_at=now - 36000, login='ok', error='Muse /usage unavailable'),
               refresh=True)
    orch = await _session(client, auth, make_dir, kind='orchestrator')
    body = (await client.get('/api/orchestrator/accounts/usage', headers=_orch(issue_token(orch['id'])))).json()
    row = next(a for a in body['accounts'] if a['name'] == 'Idle Muse')
    capacity = row['model_capacity']['muse-spark-1.3']
    assert capacity['quota_known'] is False and capacity['exploration_eligible'] is True
    assert row['remaining_to_limit_percent'] is None and row['usage']['stale'] is True
    assert 'exploration_eligible' in body['note']
