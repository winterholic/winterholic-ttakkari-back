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
    assert quota_row['windows'] == []
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
    assert row['windows'] == [] and row['updated_at'] is None
    assert row['error']


def test_muse_execution_is_enabled_by_default_without_enabling_auto_routing():
    from ttakkari.config import Settings
    from ttakkari.runs import worker_routing

    assert Settings(_env_file=None).muse_enabled is True
    assert Settings(_env_file=None).muse_auto_routing is False
    assert worker_routing.MUSE_AUTO_ROUTING_ACCEPTED is False


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
