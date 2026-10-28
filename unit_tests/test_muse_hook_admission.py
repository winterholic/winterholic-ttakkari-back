"""Muse guard admits read-only discovery but stays fail-closed otherwise."""
import pytest

from ttakkari.security.muse_hook import decision


def policy(role='agent'):
    return {'write_roots': ['/tmp/ttk-allowed'], 'protected': ['/tmp/ttk-home'],
            'private_root': '/tmp/ttk-private', 'cwd': '/tmp/ttk-allowed',
            'mcp_servers': ['ttakkari'], 'role': role}


def test_tool_search_admitted_for_discovery():
    data = {'tool_name': 'tool_search', 'tool_input': {'query': 'spawn', 'limit': 10}}
    assert decision(data, policy('agent')) is None
    assert decision(data, policy('orchestrator')) is None


def test_read_skill_admitted_for_loading():
    data = {'tool_name': 'read_skill', 'tool_input': {'name': 'working-discipline'}}
    assert decision(data, policy('agent')) is None
    assert decision(data, policy('orchestrator')) is None


@pytest.mark.parametrize('tool', ['subagent_spawn', 'monitor', 'frobnicate'])
def test_unlisted_tools_stay_denied(tool):
    reason = decision({'tool_name': tool, 'tool_input': {}}, policy('orchestrator'))
    assert reason == f'Muse tool is not yet admitted: {tool}'


def test_worker_tools_need_known_role():
    spawn = {'tool_name': 'mcp__ttakkari__spawn_worker', 'tool_input': {'title': 't'}}
    assert decision(spawn, policy('orchestrator')) is None
    assert decision(spawn, policy('agent')) is None
    assert decision(spawn, policy('')) is not None
    recall = {'tool_name': 'mcp__ttakkari__recall', 'tool_input': {'query': 'q'}}
    assert decision(recall, policy('agent')) is None


def test_malformed_payload_blocked():
    assert decision({'tool_name': 'tool_search'}, policy()) == 'Malformed Muse tool payload'
    assert decision({'tool_name': 123, 'tool_input': {}}, policy()) == 'Malformed Muse tool payload'
