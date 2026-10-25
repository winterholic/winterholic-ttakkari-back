"""Exercise launch/account boundaries missed by the first integration tests."""
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import uuid4

import pytest

from ttakkari import copilot_accounts as accounts
from ttakkari.agents.base import LaunchSpec
from ttakkari.agents.copilot import CopilotAdapter
from ttakkari.api.accounts import AccountIn
from ttakkari.runs.profiles import is_usage_limit
from ttakkari.security import seatbelt

pytestmark = [pytest.mark.security]


@pytest.fixture
def system_home(tmp_path, monkeypatch):
    home = tmp_path / 'user'
    home.mkdir()
    monkeypatch.setattr(Path, 'home', lambda: home)
    monkeypatch.setattr(accounts, 'get_settings', lambda: SimpleNamespace(home=home / 'ttakkari', copilot_bin='copilot'))
    return home


def test_copilot_registration_schema_accepts_engine():
    assert AccountIn(name='Copilot', engine='copilot').engine == 'copilot'


def test_system_config_file_does_not_prove_authentication(system_home):
    config = system_home / '.copilot' / 'config.json'
    config.parent.mkdir()
    config.write_text('{"theme":"dark"}')
    assert accounts.status(None, is_system=True)['auth_state'] != 'authenticated'


def test_system_auth_failure_survives_status_refresh(system_home):
    accounts.record_run(None, success=False, error='401 Unauthorized', is_system=True)
    state = accounts.status(None, is_system=True)
    assert state['auth_state'] == 'auth_expired'
    assert not accounts.is_ready(None, is_system=True)
    accounts.record_run(None, success=True, is_system=True)
    assert accounts.is_ready(None, is_system=True)


def test_config_listing_success_does_not_prove_auth(tmp_path, monkeypatch):
    profile = accounts.allocate(tmp_path, uuid4())
    (profile.home / 'config.json').write_text('{"theme":"dark"}')
    run = Mock(return_value=SimpleNamespace(returncode=0, stdout='theme=dark', stderr=''))
    monkeypatch.setattr(accounts.subprocess, 'run', run)
    assert accounts.check_auth(profile.home, cli_bin='copilot')['auth_state'] != 'authenticated'


@pytest.mark.parametrize('engine', ['opencode', 'copilot'])
def test_sandbox_mandatory_even_with_empty_opt_in(engine, tmp_path):
    cmd = seatbelt.maybe_wrap(engine, ['/bin/true'], enabled_engines=[], home=tmp_path,
                             ttakkari_home=tmp_path / 'ttk', outbox=tmp_path / 'out',
                             write_roots=[tmp_path / 'workspace'], env={}, workspace=tmp_path / 'workspace')
    assert cmd[0] == seatbelt.SANDBOX_EXEC
    profile = cmd[2]
    assert '(deny file-write*)' in profile
    assert f'(subpath "{tmp_path / "workspace"}")' in profile


def test_system_copilot_home_environment_is_not_denied(tmp_path):
    policy = seatbelt.policy_for(home=tmp_path, ttakkari_home=tmp_path / 'ttk',
                                outbox=tmp_path / 'out', write_roots=[], engine='copilot',
                                env={'COPILOT_HOME': str(tmp_path / '.copilot')})
    assert '.copilot' not in policy.secret_dirs


@pytest.mark.parametrize('message', [
    'You have exceeded your premium request allowance',
    'You have exceeded your premium requests allowance for this month',
])
def test_real_premium_allowance_is_a_limit(message):
    assert is_usage_limit(message)


@pytest.mark.parametrize('message', [
    'See the quota documentation for details',
    'Your quota is healthy and requests are available',
])
def test_quota_guidance_is_not_a_limit(message):
    assert not is_usage_limit(message)


def test_copilot_mcp_config_path_uses_cli_file_prefix(tmp_path):
    spec = LaunchSpec(cwd=tmp_path, prompt='hi', engine_session_id=None, model='auto', effort=None,
                      outbox=tmp_path, write_roots=[tmp_path], system_note='', session_id=str(uuid4()),
                      mcp_servers={'test': {'command': '/bin/echo', 'args': ['test']}})
    adapter = CopilotAdapter()
    try:
        cmd = adapter.build_command(spec, {})
        assert cmd[cmd.index('--additional-mcp-config') + 1].startswith('@')
    finally:
        adapter.cleanup(spec)


def test_unverified_legacy_manifest_cannot_enable_execution(tmp_path):
    import json
    profile = accounts.allocate(tmp_path, uuid4())
    data = json.loads(profile.manifest_path.read_text())
    data['auth_state'] = 'authenticated'
    profile.manifest_path.write_text(json.dumps(data))
    assert not accounts.is_ready(profile.home)


def test_environment_cannot_override_selected_copilot_account(tmp_path):
    profile = accounts.allocate(tmp_path, uuid4())
    env = accounts.execution_env(profile.home, {'GH_TOKEN': 'fixture', 'COPILOT_GITHUB_TOKEN': 'fixture',
        'COPILOT_PROVIDER_BASE_URL': 'https://example.invalid', 'COPILOT_ALLOW_ALL': 'true', 'PATH': '/bin'})
    assert not {'GH_TOKEN', 'COPILOT_GITHUB_TOKEN', 'COPILOT_PROVIDER_BASE_URL', 'COPILOT_ALLOW_ALL'} & env.keys()
    assert env['PATH'] == '/bin'


def test_copilot_empty_success_cannot_authenticate():
    from ttakkari.agents.base import ParseState
    state = ParseState()
    CopilotAdapter().finalize_process(state, return_code=0, stderr='')
    assert state.is_error


def test_copilot_partial_stream_without_final_record_fails():
    from ttakkari.agents.base import ParseState
    state = ParseState(result_text='partial')
    CopilotAdapter().finalize_process(state, return_code=0, stderr='')
    assert state.is_error


@pytest.mark.parametrize('model', ['gpt-5.6-luna', 'gpt-5.4', 'claude-sonnet-5.5'])
def test_explicit_copilot_model_is_not_replaced_by_auto(model):
    from ttakkari.runs.worker_routing import primary_target
    assert primary_target('copilot', model) == ('copilot', model)


@pytest.mark.asyncio
@pytest.mark.parametrize('engine', ['copilot', 'opencode'])
async def test_new_engine_usage_never_queries_codex(engine, monkeypatch):
    from unittest.mock import AsyncMock

    from ttakkari.runs import profiles, usage
    codex = AsyncMock(return_value=usage.Usage(windows=[usage.Window('five_hour', '5시간', 73, None)]))
    monkeypatch.setattr(usage, '_codex_usage_or_log', codex)
    monkeypatch.setattr(profiles, 'identity', lambda *args: ('unknown', None))
    result = await usage._fetch_live(uuid4(), engine, None)
    codex.assert_not_awaited()
    assert result.windows == []
    assert result.updated_at is None


@pytest.mark.parametrize('engine,model', [('copilot', 'gpt-5.6-luna'), ('opencode', 'github-copilot/gpt-5.6-sol')])
def test_worker_schema_accepts_explicit_new_engine(engine, model):
    from ttakkari.api.orchestrator import WorkerIn
    body = WorkerIn(title='probe', prompt='hello', engine=engine, model=model)
    assert body.engine == engine


def test_opencode_executable_remains_readable_without_opening_auth_store(tmp_path):
    policy = seatbelt.policy_for(home=tmp_path, ttakkari_home=tmp_path / 'ttk',
                                outbox=tmp_path / 'out', write_roots=[], engine='opencode', env={})
    rendered = seatbelt.render_profile(policy)
    exception = f'(allow file-read-data (literal "{tmp_path / ".opencode/bin/opencode"}"))'
    assert exception in rendered
    assert rendered.index(';; 비밀 경로') < rendered.index(exception)
