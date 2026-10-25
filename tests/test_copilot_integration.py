"""Account/API/fallback integration with isolated PostgreSQL and no live provider calls."""
from unittest.mock import AsyncMock

import pytest
from helpers import run_events, run_to_end
from test_orchestrator import _orch, _session

from ttakkari import copilot_accounts
from ttakkari.agents.base import ParseState
from ttakkari.orchestrator.runtime import issue_token
from ttakkari.runs.manager import manager

pytestmark = [pytest.mark.security]


async def test_create_system_and_isolated_copilot_accounts(client, auth):
    first = await client.post('/api/accounts', headers=auth, json={'name': 'cp-system', 'engine': 'copilot'})
    assert first.status_code == 201, first.text
    assert first.json()['config_dir'] is None
    assert first.json()['auth_state'] != 'authenticated'
    second = await client.post('/api/accounts', headers=auth, json={'name': 'cp-isolated', 'engine': 'copilot'})
    assert second.status_code == 201, second.text
    assert second.json()['config_dir'] is not None
    assert second.json()['isolation_mode'] == 'managed_copilot_home'


@pytest.mark.parametrize('engine,model', [('copilot', 'gpt-5.6-luna'), ('opencode', 'github-copilot/gpt-5.6-sol')])
async def test_explicit_worker_api_preserves_engine_model_without_running_provider(client, auth, make_dir, monkeypatch, engine, model):
    monkeypatch.setattr(manager, 'dispatch', AsyncMock())
    orch = await _session(client, auth, make_dir, kind='orchestrator')
    result = await client.post('/api/orchestrator/workers', headers=_orch(issue_token(orch['id'])),
        json={'title': 'fixture', 'prompt': 'hello', 'engine': engine, 'model': model, 'strict_engine': True})
    assert result.status_code == 201, result.text
    worker = (await client.get('/api/sessions/' + result.json()['worker_id'], headers=auth)).json()
    assert (worker['engine'], worker['model']) == (engine, model)


async def test_system_auth_failure_falls_back_and_persists_reauthentication(client, auth, make_dir, monkeypatch):
    first = (await client.post('/api/accounts', headers=auth, json={'name': 'cp-system', 'engine': 'copilot'})).json()
    second = (await client.post('/api/accounts', headers=auth, json={'name': 'cp-isolated', 'engine': 'copilot'})).json()
    copilot_accounts.record_run(None, success=True, is_system=True)
    copilot_accounts.record_run(second['config_dir'], success=True)
    attempts = []

    async def attempt(self, db, run, adapter, spec, env, root, handle, profile_id):
        attempts.append(str(profile_id))
        if str(profile_id) == first['id']:
            return ParseState(is_error=True, error='401 Unauthorized'), 1, False, False
        return ParseState(result_text='done'), 0, False, False

    # Patch the defining class: restoring an inherited method on the singleton
    # leaves a bound instance attribute that hides later class-level patches.
    monkeypatch.setattr(type(manager), '_attempt', attempt)
    session = await _session(client, auth, make_dir, engine='copilot', model='gpt-5.6-luna', kind='chat', strict_engine=True)
    run = await run_to_end(client, auth, session['id'], 'hello')
    assert run['status'] == 'succeeded', run
    assert attempts == [first['id'], second['id']]
    state = (await client.get('/api/accounts', headers=auth)).json()
    system = next(a for a in state if a['id'] == first['id'])
    assert system['auth_state'] == 'auth_expired'
    assert not system['execution_enabled']
    fallback = next(e for e in await run_events(client, auth, run['id']) if e['type'] == 'engine.fallback')
    assert fallback['payload']['reason'] == 'auth_failed'
