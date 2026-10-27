"""Exercise the real stdout pump and provider handoff using a scripted Muse process."""
import json
import sys
import uuid
from importlib import import_module

from helpers import run_events, run_to_end, start_run, wait_event
from test_orchestrator import _session
from test_orchestrator_muse import _seed_muse

from ttakkari.agents.muse import MuseAdapter
from ttakkari.runs import profiles


class BrokenMcpMuse(MuseAdapter):
    def build_command(self, spec, extra_env):
        frames = [{'method': 'item/completed', 'params': {'item': {
            'kind': 'toolCall', 'itemId': f'bad-{i}', 'revision': 1,
            'tool': 'mcp__ttakkari__spawn_worker', 'args': '{}', 'status': 'failed',
            'visibleOutput': 'Error executing tool spawn_worker: Field required [input_value={}]',
        }}} for i in range(2)]
        frames.append({'method': 'item/completed', 'params': {'item': {
            'kind': 'agentMessage', 'itemId': 'false-success', 'revision': 1,
            'text': 'Must not accept success after broken MCP',
        }}})
        frames.append({'method': 'turn/completed', 'params': {'terminal': 'completed'}})
        return [sys.executable, '-c',
                'import json; frames=json.loads(' + repr(json.dumps(frames)) + '); '
                '[print(json.dumps(f),flush=True) for f in frames]']


async def test_binding_failure_hands_off_to_native_with_tool_history(client, auth, make_dir, monkeypatch):
    await _seed_muse(client, auth)
    module = import_module('ttakkari.runs.manager')
    monkeypatch.setitem(module.ADAPTERS, 'muse', BrokenMcpMuse())
    original_choose = profiles.choose_async

    async def start_on_muse(candidates, run_id, **kwargs):
        muse = [p for p in candidates if p.engine == 'muse']
        return await original_choose(muse or candidates, run_id, **kwargs)

    monkeypatch.setattr(profiles, 'choose_async', start_on_muse)
    launched = []
    original_attempt = module.RunManager._attempt

    async def attempt(self, db, run, adapter, spec, *args):
        launched.append((adapter.engine, spec.prompt))
        return await original_attempt(self, db, run, adapter, spec, *args)

    monkeypatch.setattr(module.RunManager, '_attempt', attempt)
    orch = await _session(client, auth, make_dir, kind='orchestrator')
    run = await run_to_end(client, auth, orch['id'], 'Continue the authorized work')
    assert run['status'] == 'succeeded', run
    assert run['engine'] in {'claude', 'codex'}, run
    assert len(launched) == 2
    assert launched[0][0] == 'muse'
    assert 'spawn_worker' in launched[1][1]
    assert 'input_value={}' in launched[1][1]
    events = await run_events(client, auth, run['id'])
    fallback = [e['payload'] for e in events if e['type'] == 'engine.fallback']
    assert fallback[-1]['reason'] == 'mcp_binding_failure'
    assert not any(e['type'] == 'account.limited' for e in events)
    assert not any(e['type'] == 'message' and 'Must not accept' in e['payload'].get('text', '')
                   for e in events)


async def test_strict_muse_failure_does_not_switch_engine(client, auth, make_dir, monkeypatch):
    await _seed_muse(client, auth)
    module = import_module('ttakkari.runs.manager')
    monkeypatch.setitem(module.ADAPTERS, 'muse', BrokenMcpMuse())
    orch = await _session(client, auth, make_dir, engine='muse', strict_engine=True)
    run = await run_to_end(client, auth, orch['id'], 'Strict engine diagnostic')
    assert run['status'] == 'failed', run
    assert run['engine'] == 'muse'
    assert 'MCP binding failed' in run['error']
    events = await run_events(client, auth, run['id'])
    assert not any(e['type'] in {'engine.fallback', 'account.limited'} for e in events)


async def test_native_quota_wait_keeps_handoff_and_excludes_broken_muse(client, auth, make_dir, monkeypatch):
    await _seed_muse(client, auth)
    module = import_module('ttakkari.runs.manager')
    monkeypatch.setitem(module.ADAPTERS, 'muse', BrokenMcpMuse())
    for row in (await client.get('/api/accounts', headers=auth)).json():
        if row['engine'] in {'claude', 'codex'}:
            profiles.cool_down(uuid.UUID(row['id']), 600)
    orch = await _session(client, auth, make_dir, kind='orchestrator')
    created = await start_run(client, auth, orch['id'], 'Continue after native reset')
    await wait_event(client, auth, created['id'], 'run.waiting')
    run = (await client.get(f"/api/runs/{created['id']}", headers=auth)).json()
    assert run['status'] == 'queued' and run['resume_after'], run
    from ttakkari.db import sessionmaker
    from ttakkari.models import ChatSession, Run
    async with sessionmaker()() as db:
        stored = await db.get(Run, uuid.UUID(run['id']))
        assert 'spawn_worker' in stored.resume_handoff
        assert stored.usage['mcp_binding_failed_engines'] == ['muse']
        sess = await db.get(ChatSession, uuid.UUID(orch['id']))
        ready, _ = await module._available_profiles(db, sess, stored, False)
        assert not any(p.engine == 'muse' for p in ready)
    await client.post(f"/api/runs/{created['id']}/cancel", headers=auth)
