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


async def test_planner_activates_existing_host_login_idempotently(client, auth, make_dir, monkeypatch):
    import time

    from ttakkari.runs import copilot_usage, usage

    def authenticated(*args, **kwargs):
        copilot_accounts.record_run(None, success=True, is_system=True)
        return {'auth_state': 'authenticated'}

    async def quota(*args):
        return usage.Usage(source='copilot_sdk', updated_at=time.time(), windows=[
            usage.Window('copilot_premium_interactions', 'monthly', 2, time.time()+3600, 98.1)])

    monkeypatch.setattr(copilot_accounts, 'check_auth', authenticated)
    monkeypatch.setattr(copilot_usage, 'fetch_usage', quota)
    orch = await _session(client, auth, make_dir, kind='orchestrator')
    headers = _orch(issue_token(orch['id']))
    first = await client.post('/api/orchestrator/copilot/activate', headers=headers)
    assert first.status_code == 200, first.text
    second = await client.post('/api/orchestrator/copilot/activate', headers=headers)
    assert second.status_code == 200, second.text
    assert first.json()['account']['id'] == second.json()['account']['id']
    assert first.json()['account']['config_dir'] is None
    assert first.json()['account']['execution_enabled'] is True
    rows = (await client.get('/api/accounts', headers=auth)).json()
    assert len([a for a in rows if a['engine'] == 'copilot']) == 1
    state = (await client.get('/api/orchestrator/accounts/usage', headers=headers)).json()
    cp = next(a for a in state['accounts'] if a['engine'] == 'copilot')
    assert cp['available'] is True
    assert cp['model_capacity']['auto']['quota_known'] is True
    assert cp['usage']['model_quotas'][0]['remaining_percent'] == 98.1


async def test_activation_failure_stays_visible_but_cannot_execute(client, auth, make_dir, monkeypatch):
    monkeypatch.setattr(copilot_accounts, 'check_auth', lambda *a, **k: {'auth_state': 'auth_expired'})
    orch = await _session(client, auth, make_dir, kind='orchestrator')
    response = await client.post('/api/orchestrator/copilot/activate', headers=_orch(issue_token(orch['id'])))
    assert response.status_code == 200, response.text
    assert response.json()['account']['engine'] == 'copilot'
    assert response.json()['account']['enabled'] is False
    assert response.json()['activated'] is False


async def test_work_planner_cannot_enroll_personal_host_account(client, auth, make_dir):
    orch = await _session(client, auth, make_dir, kind='orchestrator', pool='work')
    response = await client.post('/api/orchestrator/copilot/activate', headers=_orch(issue_token(orch['id'])))
    assert response.status_code == 403, response.text


async def test_emergency_planner_uses_copilot_after_native_limits(client, auth, make_dir, monkeypatch):
    from importlib import import_module

    from ttakkari.runs import profiles

    cp = (await client.post('/api/accounts', headers=auth,
                           json={'name': 'copilot emergency', 'engine': 'copilot'})).json()
    copilot_accounts.record_run(None, success=True, is_system=True)
    module = import_module('ttakkari.runs.manager')
    monkeypatch.setitem(module.ADAPTERS, 'copilot', type(module.ADAPTERS['claude'])())
    rows = (await client.get('/api/accounts', headers=auth)).json()
    for row in rows:
        if row['engine'] in {'claude', 'codex'}:
            from uuid import UUID
            profiles.cool_down(UUID(row['id']), 600)
    orch = await _session(client, auth, make_dir, kind='orchestrator')
    run = await run_to_end(client, auth, orch['id'], 'Copilot emergency fixture')
    assert run['status'] == 'succeeded', run
    assert run['engine'] == 'copilot' and run['profile'] == cp['name']
    assert run['usage']['selected_model'] == 'auto'


async def test_native_planner_keeps_priority_over_copilot_headroom(client, auth, make_dir, monkeypatch):
    from ttakkari.runs import profiles

    await client.post('/api/accounts', headers=auth, json={'name': 'copilot', 'engine': 'copilot'})
    copilot_accounts.record_run(None, success=True, is_system=True)
    monkeypatch.setattr(profiles, '_usage_headroom', lambda p: (98 if p.engine == 'copilot' else 10, 2_000_000_000))
    orch = await _session(client, auth, make_dir, kind='orchestrator')
    run = await run_to_end(client, auth, orch['id'], 'Native planner fixture')
    assert run['status'] == 'succeeded' and run['engine'] in {'claude', 'codex'}, run


async def test_auto_worker_uses_monthly_copilot_capacity_and_requires_review(client, auth, make_dir, monkeypatch):
    import time
    from importlib import import_module
    from uuid import UUID

    from helpers import wait_run

    from ttakkari.runs import usage

    cp = (await client.post('/api/accounts', headers=auth, json={'name': 'copilot auto', 'engine': 'copilot'})).json()
    copilot_accounts.record_run(None, success=True, is_system=True)
    usage.seed(UUID(cp['id']), usage.Usage(source='copilot_sdk', updated_at=time.time(), windows=[
        usage.Window('copilot_premium_interactions', 'monthly', 2, None, 98.1)]), refresh=True)
    module = import_module('ttakkari.runs.manager')
    monkeypatch.setitem(module.ADAPTERS, 'copilot', type(module.ADAPTERS['claude'])())
    orch = await _session(client, auth, make_dir, kind='orchestrator')
    headers = _orch(issue_token(orch['id']))
    spawned = await client.post('/api/orchestrator/workers', headers=headers,
                                json={'title': 'auto fixture', 'prompt': 'fixture', 'task_type': 'general'})
    assert spawned.status_code == 201, spawned.text
    runs = (await client.get(f"/api/sessions/{spawned.json()['worker_id']}/runs", headers=auth)).json()
    run = await wait_run(client, auth, runs[0]['id'])
    assert run['status'] == 'succeeded' and run['engine'] == 'copilot', run
    worker = (await client.get(f"/api/orchestrator/workers/{spawned.json()['worker_id']}", headers=headers)).json()
    assert worker['routing']['selected']['model'] == 'auto'
    assert worker['routing']['selected']['quota_known'] is True
    assert worker['routing']['selected']['remaining_to_limit_percent'] == 98.1
    assert worker['verification_required'] is True


@pytest.mark.parametrize('engine,prefix', [('copilot', 'cp'), ('opencode', 'oc')])
async def test_copilot_harness_shows_real_delivery_mcp_and_guard(client, auth, engine, prefix):
    response = await client.get('/api/harness/layers', headers=auth, params={'session': 'worker', 'engine': engine})
    assert response.status_code == 200, response.text
    ids = {layer['id'] for layer in response.json()['layers']}
    assert {f'{prefix}_delivery', f'{prefix}_mcp', f'{prefix}_sandbox'} <= ids
    assert 'qwen_sandbox' not in ids


def test_orphan_copilot_pid_selection_only_parentless_headless():
    from ttakkari.runs.copilot_usage import _orphan_copilot_pids
    out = "\n".join([
        "101 1 /x/copilot-darwin-arm64/copilot --headless --no-auto-update --stdio",
        "102 555 /x/copilot-darwin-arm64/copilot --headless --stdio",
        "103 1 /x/copilot --stdio",
        "104 1 /usr/bin/python server.py --headless --stdio",
    ])
    assert _orphan_copilot_pids(out) == [101]
