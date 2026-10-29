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
    # 0f6f0a77: 소유자 세션 조회는 회사 세션에도 열려 있다(세션 인증은 유지).
    assert denied.status_code == 200


async def test_read_session_echoes_args_provenance_and_keeps_legacy_rows_null(client, setup_sessions):  # noqa: F811
    personal, _work = setup_sessions
    async with sessionmaker()() as db:
        run = Run(session_id=personal.id, workspace_id=personal.workspace_id,
                  engine='muse', status='failed', prompt='diagnose')
        db.add(run)
        await db.flush()
        events.append(db, run, 'tool.call', {'call_id': 'new', 'name': 'mcp__ttakkari__spawn_worker',
                                           'input': {},
                                           'input_provenance': {'args_present': False, 'args_type': 'NoneType',
                                                                'args_parsed': False}})
        events.append(db, run, 'tool.result', {'call_id': 'new', 'is_error': True, 'output': 'boom'})
        events.append(db, run, 'tool.call', {'call_id': 'old', 'name': 'mcp__ttakkari__spawn_worker',
                                           'input': {}})
        events.append(db, run, 'tool.result', {'call_id': 'old', 'is_error': True, 'output': 'boom'})
        await db.commit()
    headers = {'Authorization': f'Bearer {issue_token(personal.id)}'}
    response = await client.get(f'/api/agent/sessions/{personal.id}', headers=headers)
    assert response.status_code == 200
    errors = response.json()['turns'][0]['mcp_tool_errors']
    assert [row['call_id'] for row in errors] == ['new', 'old']
    assert errors[0]['input_provenance'] == {'args_present': False, 'args_type': 'NoneType',
                                             'args_parsed': False}
    assert errors[1]['input_provenance'] is None
