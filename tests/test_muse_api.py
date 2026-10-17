"""Disposable DB API tests. No Muse credential, model or operating runtime is used."""
from helpers import run_to_end


async def test_muse_account_usage_reports_execution_disabled(client, auth, make_dir):
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


async def test_non_coding_model_is_rejected_before_session_creation(client, auth, new_session):
    ws, _sess = await new_session()
    response = await client.post('/api/sessions', headers=auth,
        json={'workspace_id': ws['id'], 'engine': 'muse', 'model': 'sam-3.1'})
    assert response.status_code == 422


async def test_disabled_muse_run_fails_without_mock_success(client, auth, new_session):
    await client.post('/api/accounts', headers=auth, json={'name': 'Muse fixture', 'engine': 'muse'})
    _ws, sess = await new_session(engine='muse')
    done = await run_to_end(client, auth, sess['id'], 'Do not execute a model.')
    assert done['status'] == 'failed'
    assert 'disabled' in done['error']
    assert done['result_text'] is None
