"""Bounded, scoped evidence for diagnosing MCP dispatch failures without reading private logs."""
from test_agent_scope_auth import setup_sessions  # noqa: F401 -- shared fixture

from ttakkari.db import sessionmaker
from ttakkari.models import Run
from ttakkari.orchestrator.runtime import issue_token
from ttakkari.runs import events


async def test_read_session_returns_mcp_failure_identity_and_argument_keys_only(client, setup_sessions):  # noqa: F811
    personal, work = setup_sessions
    async with sessionmaker()() as db:
        run = Run(session_id=personal.id, workspace_id=personal.workspace_id,
                  engine='muse', status='succeeded', prompt='diagnose')
        db.add(run)
        await db.flush()
        for i in range(5):
            events.append(db, run, 'tool.call', {'call_id': f'mcp-{i}', 'name': 'mcp__ttakkari__spawn_worker',
                                               'input': {} if i == 4 else {'prompt': 'PRIVATE_ARG_VALUE'}})
            events.append(db, run, 'tool.result', {'call_id': f'mcp-{i}', 'is_error': i != 4,
                'output': 'Error executing tool spawn_worker: Field required input_value={} api_key=PRIVATE_SECRET'})
        events.append(db, run, 'tool.call', {'call_id': 'shell', 'name': 'shell', 'input': {'cmd': 'PRIVATE_SHELL'}})
        events.append(db, run, 'tool.result', {'call_id': 'shell', 'is_error': True, 'output': 'PRIVATE_SHELL_ERROR'})
        # Another provider can reuse a local call id after a handoff. Its result
        # must be bound to the nearest preceding call, not an older MCP call.
        events.append(db, run, 'tool.call', {'call_id': 'mcp-4', 'name': 'shell', 'input': {}})
        events.append(db, run, 'tool.result', {'call_id': 'mcp-4', 'is_error': True, 'output': 'PRIVATE_SHELL_REUSED_ID'})
        events.append(db, run, 'tool.call', {'call_id': 'ok', 'name': 'mcp__ttakkari__recall', 'input': {'query': 'x'}})
        events.append(db, run, 'tool.result', {'call_id': 'ok', 'is_error': False, 'output': '[]'})
        # Successful documentation responses can quote error messages repeatedly.
        # They must not occupy the bounded window and hide real older failures.
        for i in range(5):
            events.append(db, run, 'tool.call', {'call_id': f'doc-{i}', 'name': 'mcp__ttakkari__recall',
                                               'input': {'query': 'errors'}})
            events.append(db, run, 'tool.result', {'call_id': f'doc-{i}', 'is_error': False,
                'output': 'Documentation: unknown tool spawn_worker; Field required input_value={}'})
        await db.commit()
    headers = {'Authorization': f'Bearer {issue_token(personal.id)}'}
    response = await client.get(f'/api/agent/sessions/{personal.id}', headers=headers)
    assert response.status_code == 200
    errors = response.json()['turns'][0]['mcp_tool_errors']
    assert len(errors) == 3
    assert [row['call_id'] for row in errors] == ['mcp-2', 'mcp-3', 'mcp-4']
    assert errors[-1]['input_keys'] == []
    assert errors[-1]['input_empty'] is True
    assert errors[-1]['recorded_is_error'] is False  # embedded validation failure was misreported as success
    assert errors[-1]['call_seq'] < errors[-1]['result_seq']
    assert 'PRIVATE_ARG_VALUE' not in response.text
    assert 'PRIVATE_SECRET' not in response.text
    assert 'PRIVATE_SHELL' not in response.text
    denied = await client.get(f'/api/agent/sessions/{personal.id}',
                             headers={'Authorization': f'Bearer {issue_token(work.id)}'})
    assert denied.status_code == 403
