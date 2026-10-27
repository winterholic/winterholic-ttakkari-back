"""Binding failures must be bounded; ordinary MCP errors are not engine failures."""
from pathlib import Path

from ttakkari.agents.base import ParseState
from ttakkari.agents.muse import MuseAdapter


def complete(adapter, state, iid, *, tool='mcp__ttakkari__spawn_worker', args='{}', output=None,
             status='failed', revision=2):
    return adapter.parse_line({'method': 'item/completed', 'params': {'item': {
        'kind': 'toolCall', 'itemId': iid, 'revision': revision, 'tool': tool, 'args': args,
        'status': status, 'visibleOutput': output or (
            'Error executing tool spawn_worker: 2 validation errors for spawn_workerArguments\n'
            'title\n  Field required [type=missing, input_value={}, input_type=dict]\n'
            'prompt\n  Field required [type=missing, input_value={}, input_type=dict]'),
    }}}, state, Path('/tmp'))


def test_empty_mcp_arguments_stop_after_two_distinct_failures():
    adapter, state = MuseAdapter(), ParseState()
    complete(adapter, state, 'one')
    assert not state.is_error
    events = complete(adapter, state, 'two', status='completed')
    assert state.is_error
    assert state.adapter_data['mcp_binding_failure']
    assert events[-1].type == 'error'
    assert events[-1].payload['kind'] == 'mcpBindingFailure'
    assert not state.limit_hit


def test_duplicate_completed_revision_does_not_trigger_recovery():
    adapter, state = MuseAdapter(), ParseState()
    complete(adapter, state, 'one')
    complete(adapter, state, 'one', revision=3)
    assert not state.is_error


def test_successful_mcp_call_resets_failure_streak():
    adapter, state = MuseAdapter(), ParseState()
    complete(adapter, state, 'one')
    complete(adapter, state, 'ok', tool='mcp__ttakkari__recall', args='{"query":"Muse"}',
             output='{"results":[]}', status='completed')
    complete(adapter, state, 'two')
    assert not state.is_error


def test_unknown_mcp_tool_repeats_trigger_recovery():
    adapter, state = MuseAdapter(), ParseState()
    for iid in ['one', 'two']:
        complete(adapter, state, iid, tool='mcp.ttakkari.spawn_worker',
                 output='unknown tool `mcp.ttakkari.spawn_worker`')
    assert state.is_error


def test_unknown_tool_accepts_exact_leaf_or_namespace_only():
    for tool in ['mcp__ttakkari__spawn_worker', 'mcp.ttakkari.spawn_worker']:
        for reported in ['spawn_worker', tool]:
            adapter, state = MuseAdapter(), ParseState()
            for iid in ['one', 'two']:
                complete(adapter, state, iid, tool=tool, output=f'unknown tool `{reported}`')
            assert state.is_error, reported
        for reported in ['spawn_worker_v2', 'other_tool']:
            adapter, state = MuseAdapter(), ParseState()
            for iid in ['one', 'two']:
                complete(adapter, state, iid, tool=tool,
                         output=f'unknown tool `{reported}` while executing {tool}')
            assert not state.is_error, reported


def test_non_mcp_success_does_not_prove_mcp_binding_has_recovered():
    adapter, state = MuseAdapter(), ParseState()
    complete(adapter, state, 'one')
    complete(adapter, state, 'read', tool='read_file', args='{"path":"README.md"}',
             output='File contents', status='completed')
    complete(adapter, state, 'two')
    assert state.is_error


def test_business_errors_and_quoted_documents_never_trigger_recovery():
    for kwargs in [
        {'output': 'HTTP 403: permission denied'},
        {'args': '{"title":"x"}'},
        {'tool': 'read_file'},
        {'output': 'Documentation example: Error executing tool spawn_worker: Field required input_value={}',
         'status': 'completed'},
        {'output': 'unknown tool `mcp__ttakkari__spawn_worker`', 'status': 'completed'},
    ]:
        adapter, state = MuseAdapter(), ParseState()
        complete(adapter, state, 'one', **kwargs)
        complete(adapter, state, 'two', **kwargs)
        assert not state.is_error, kwargs


UNAVAILABLE_OBSERVED = ('tool unavailable: unknown tool `mcp__ttakkari.list_workers`. '
                        'Session tool ids: mcp__ttakkari__list_workers')


def test_tool_unavailable_envelope_triggers_recovery():
    adapter, state = MuseAdapter(), ParseState()
    for iid in ['one', 'two']:
        complete(adapter, state, iid, tool='mcp__ttakkari.list_workers',
                 output=UNAVAILABLE_OBSERVED)
    assert state.is_error


def test_tool_unavailable_accepts_exact_leaf_or_full_only():
    for reported in ['mcp__ttakkari__spawn_worker', 'spawn_worker']:
        adapter, state = MuseAdapter(), ParseState()
        for iid in ['one', 'two']:
            complete(adapter, state, iid,
                     output=f'tool unavailable: unknown tool `{reported}`. Session tool ids: mcp__ttakkari__recall')
        assert state.is_error, reported


def test_tool_unavailable_lookalikes_never_trigger_recovery():
    for kwargs in [
        # A different tool is reported; the called tool only appears in session ids.
        {'output': 'tool unavailable: unknown tool `other_tool`. '
                   'Session tool ids: mcp__ttakkari__spawn_worker'},
        # Similar-prefix leaf is not the exact leaf.
        {'output': 'tool unavailable: unknown tool `spawn_worker_v2`. '
                   'Session tool ids: mcp__ttakkari__spawn_worker'},
        # Separator-normalized ids are not assumed equivalent to the called name.
        {'tool': 'mcp__ttakkari.list_workers',
         'output': 'tool unavailable: unknown tool `mcp__ttakkari__list_workers`.'},
        # Envelope wording is matched exactly, including case.
        {'output': 'Tool Unavailable: Unknown Tool `mcp__ttakkari__spawn_worker`.'},
        # Reported ids are matched exactly, including case.
        {'output': 'tool unavailable: unknown tool `MCP__TTAKKARI__SPAWN_WORKER`.'},
        # Success quoting the envelope is documentation, not a binding failure.
        {'output': 'tool unavailable: unknown tool `mcp__ttakkari__spawn_worker`.',
         'status': 'completed'},
        # Ordinary tool errors stay out of scope.
        {'output': 'HTTP 403: permission denied'},
        # The envelope must open the output.
        {'output': 'note: tool unavailable: unknown tool `mcp__ttakkari__spawn_worker`.'},
        # Near-miss wording is not the observed envelope.
        {'output': 'tool unavailable: unknown tools `mcp__ttakkari__spawn_worker`.'},
        # Non-MCP tools never count, even with the same wording.
        {'tool': 'read_file', 'output': 'tool unavailable: unknown tool `read_file`.'},
        # Another MCP tool is called than the one reported.
        {'tool': 'mcp__ttakkari__recall',
         'output': 'tool unavailable: unknown tool `mcp__ttakkari__spawn_worker`.'},
    ]:
        adapter, state = MuseAdapter(), ParseState()
        complete(adapter, state, 'one', **kwargs)
        complete(adapter, state, 'two', **kwargs)
        assert not state.is_error, kwargs
