"""Per-Run guidance uses configured paths without reading protected files."""
from pathlib import Path
from types import SimpleNamespace

import pytest

from ttakkari.runs import instructions
from ttakkari.security import guard


def test_qwen_short_contract_stops_repeated_denied_access_without_requiring_mcp():
    from ttakkari.agents.qwen_local import QwenLocalAdapter

    note = QwenLocalAdapter.build_short_system_note(outbox='/run/outbox', write_roots=['/workspace'], kind='worker')
    assert '접근 거부' in note and '재시도' in note
    assert 'MCP 없이' in note and 'ops_deploy_log' not in note


def test_access_note_uses_current_run_and_custom_paths_without_file_reads(monkeypatch):
    settings = SimpleNamespace(home=Path('/custom/assistant'), uploads_root=Path('/custom/uploads'),
                               inbox_root=Path('/custom/inbox'))
    monkeypatch.setattr(instructions, 'get_settings', lambda: settings)
    def no_read(*args, **kwargs):
        raise AssertionError('guidance must not read protected contents')
    monkeypatch.setattr(Path, 'read_text', no_read)
    first = instructions.access_note(Path('/custom/assistant/data/outbox/run-a'))
    second = instructions.access_note(Path('/custom/assistant/data/outbox/run-b'))
    assert '/custom/assistant/data/outbox/run-a' in first
    assert '/custom/assistant/data/outbox/run-a' not in second
    assert '/custom/assistant/data/outbox/run-b' in second
    assert '/custom/uploads' in second and '/custom/inbox' in second
    assert '~/.ttakkari' not in second  # no assumed home when configured elsewhere
    assert 'ops_deploy_log' in second and 'read_session' in second


@pytest.mark.parametrize('channel', ['path', 'shell', 'mcp'])
def test_protected_denial_explains_alternative_and_still_allows_own_outbox(tmp_path, channel):
    home = tmp_path / '.ttakkari'
    own = home / 'data/outbox/current'
    config = guard.GuardConfig(write_roots=[own], protected=[home])
    foreign = home / 'data/outbox/other/result.md'
    def check(path):
        if channel == 'path':
            return guard.check_path_tool('Read', {'file_path': str(path)}, tmp_path, config)
        if channel == 'shell':
            return guard.check_bash(f'cat {path}', tmp_path, config)
        return guard.check_mcp_paths({'input': {'path': str(path)}}, tmp_path, config)
    denied = check(foreign)
    assert 'ttakkari 보호 영역 접근 차단' in denied
    assert 'ops_deploy_log' in denied and '재시도' in denied
    assert check(own / 'result.md') is None
