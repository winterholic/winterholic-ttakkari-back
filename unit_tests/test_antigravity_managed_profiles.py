"""Credential-free contracts for managed homes and native Keychain isolation."""
from pathlib import Path
from uuid import uuid4

import pytest

from ttakkari import antigravity_accounts as accounts

pytestmark = [pytest.mark.security]



class Vault:
    def __init__(self):
        self.secrets = {}
        self.calls = []

    def create(self, path, password):
        assert len(password) >= 32
        path.touch(mode=0o600)
        self.calls.append(('create', path))

    def store(self, account_id, password):
        assert account_id not in self.secrets
        self.secrets[account_id] = password

    def load(self, account_id):
        return self.secrets[account_id]

    def unlock(self, path, password):
        assert password in self.secrets.values()
        self.calls.append(('unlock', path))


def test_new_account_has_private_state_without_sharing_host_files(tmp_path):
    aid = uuid4()
    p = accounts.allocate(tmp_path, aid)
    assert p.home == tmp_path / str(aid) / 'antigravity-home'
    assert p.home.stat().st_mode & 0o777 == 0o700
    assert p.manifest.stat().st_mode & 0o777 == 0o600
    assert not (p.home / '.gemini').is_symlink()
    assert not (p.home / '.ssh').exists()
    assert accounts.status(p.home)['auth_state'] == 'setup_required'


def test_allocating_twice_never_overwrites_existing_state(tmp_path):
    p = accounts.allocate(tmp_path, uuid4())
    canary = p.home / 'keep'
    canary.write_text('existing conversation')
    again = accounts.allocate(tmp_path, p.account_id)
    assert again.home == p.home
    assert canary.read_text() == 'existing conversation'


def test_unprepared_profile_never_falls_back_to_default_auth(tmp_path):
    p = accounts.allocate(tmp_path, uuid4())
    with pytest.raises(accounts.ProfileError, match='setup_required'):
        accounts.execution_env(p.home, {'HOME': '/host', 'SSH_CONNECTION': 'inherited'})


def test_symlinked_home_is_rejected_before_credential_access(tmp_path):
    host = tmp_path / 'host'
    host.mkdir()
    (tmp_path / str(uuid4())).symlink_to(host)
    for p in tmp_path.iterdir():
        if p.is_symlink():
            with pytest.raises(accounts.ProfileError, match='unsafe_path'):
                accounts.allocate(tmp_path, __import__('uuid').UUID(p.name))


def test_setup_never_uses_empty_password_or_changes_host_keychain(tmp_path, monkeypatch):
    p = accounts.allocate(tmp_path, uuid4())
    vault = Vault()
    calls = []
    def security(home, *args):
        calls.append((home, args))
        if '-s' not in args:
            return str(p.keychain if home == p.home else Path('/host/login.keychain-db'))
        return ''
    monkeypatch.setattr(accounts, '_security', security)
    accounts.prepare(p.home, vault=vault)
    assert accounts.status(p.home)['auth_state'] == 'auth_required'
    assert len(vault.secrets[p.account_id]) >= 32
    assert all(home == p.home for home, args in calls if '-s' in args)
    assert not list(p.home.rglob('*.password'))


def test_verified_profile_scrubs_inherited_credentials_and_ssh_mode(tmp_path, monkeypatch):
    p = accounts.allocate(tmp_path, uuid4())
    vault = Vault()
    monkeypatch.setattr(accounts, '_security', lambda home, *args: str(p.keychain))
    accounts.prepare(p.home, vault=vault)
    accounts.record_probe(p.home, success=True, cli_version='1.4.0')
    env = accounts.execution_env(p.home, {'HOME':'/host','SSH_CONNECTION':'fake',
        'SSH_CLIENT':'fake','GEMINI_API_KEY':'test-secret','AGY_ADC_AUTH':'true',
        'GOOGLE_APPLICATION_CREDENTIALS':'/host/adc.json','PATH':'/bin'}, vault=vault)
    assert env['HOME'] == str(p.home)
    assert env['PATH'] == '/bin'
    assert all(key not in env for key in ['SSH_CONNECTION','SSH_CLIENT','GEMINI_API_KEY',
                                         'AGY_ADC_AUTH','GOOGLE_APPLICATION_CREDENTIALS'])


def test_keychain_namespace_mismatch_is_a_hard_failure(tmp_path, monkeypatch):
    p = accounts.allocate(tmp_path, uuid4())
    vault = Vault()
    monkeypatch.setattr(accounts, '_security', lambda home, *args: str(p.keychain))
    accounts.prepare(p.home, vault=vault)
    accounts.record_probe(p.home, success=True, cli_version='1.4.0')
    monkeypatch.setattr(accounts, '_security', lambda home, *args: '/host/login.keychain-db')
    with pytest.raises(accounts.ProfileError, match='keychain_mismatch'):
        accounts.execution_env(p.home, {}, vault=vault)


def test_auth_failure_removes_readiness_and_keeps_other_account_ready(tmp_path, monkeypatch):
    p = accounts.allocate(tmp_path, uuid4())
    q = accounts.allocate(tmp_path, uuid4())
    vault = Vault()
    monkeypatch.setattr(accounts, '_security', lambda home, *args: str(accounts.load(home).keychain))
    for profile in [p, q]:
        accounts.prepare(profile.home, vault=vault)
        accounts.record_probe(profile.home, success=True, cli_version='1.4.0')
    accounts.record_probe(p.home, success=False, error_code='auth_expired')
    assert accounts.status(p.home)['auth_state'] == 'auth_expired'
    assert accounts.status(q.home)['auth_state'] == 'ready'
    assert accounts.status(p.home)['last_success_at'] is not None


def test_arbitrary_error_text_cannot_be_written_as_account_state(tmp_path):
    p = accounts.allocate(tmp_path, uuid4())
    with pytest.raises(ValueError):
        accounts.record_probe(p.home, success=False, error_code='token=secret')
    assert 'token=secret' not in p.manifest.read_text()


def test_profile_env_uses_managed_home_and_rejects_other_account_id(tmp_path, monkeypatch):
    from ttakkari.runs.profiles import Profile
    p = accounts.allocate(tmp_path, uuid4())
    monkeypatch.setattr(accounts, 'execution_env', lambda home, env: {**env, 'HOME': str(home)})
    assert Profile(p.account_id, 'managed', 'antigravity', p.home).env()['HOME'] == str(p.home)
    with pytest.raises(ValueError, match='account_mismatch'):
        Profile(uuid4(), 'wrong', 'antigravity', p.home).env()


def test_managed_readiness_is_required_before_routing(tmp_path):
    from ttakkari.runs.profiles import Profile, execution_ready
    p = accounts.allocate(tmp_path, uuid4())
    profile = Profile(p.account_id, 'managed', 'antigravity', p.home)
    assert not execution_ready(profile)
    accounts.record_probe(p.home, success=True, cli_version='1.4.0')
    assert execution_ready(profile)


@pytest.mark.asyncio
async def test_usage_uses_same_managed_home_and_does_not_reuse_default_cache(tmp_path, monkeypatch):
    from ttakkari.runs import usage
    p = accounts.allocate(tmp_path, uuid4())
    accounts.record_probe(p.home, success=True, cli_version='1.4.0')
    usage._cache[p.account_id] = (usage.time.time(), usage.Usage(source='host-canary'))
    calls = []
    monkeypatch.setattr(accounts, 'execution_env', lambda home, env: {**env, 'HOME': str(home)})
    async def fetch(config_dir=None):
        calls.append(config_dir)
        return usage.Usage(source='antigravity_cli', updated_at=usage.time.time())
    monkeypatch.setattr(usage, '_fetch_antigravity', fetch)
    try:
        result = await usage.usage_for(p.account_id, 'antigravity', str(p.home))
        assert calls == [str(p.home)]
        assert result.source == 'antigravity_cli'
    finally:
        usage.forget(p.account_id)


def test_native_create_keeps_observed_child_error(monkeypatch, tmp_path):
    from types import SimpleNamespace

    from ttakkari.security.antigravity_keychain import KeychainError, NativeKeychain
    monkeypatch.setattr(accounts.subprocess, 'run', lambda *a, **k: SimpleNamespace(
        returncode=1, stdout=b'{"code":"native_keychain_error","os_status":-25293}', stderr=b''))
    with pytest.raises(KeychainError) as exc:
        NativeKeychain().create(tmp_path / 'test.keychain-db', b'x' * 48)
    assert exc.value.status == -25293


def test_login_command_never_runs_default_agy_for_managed_account(tmp_path):
    from ttakkari.api.accounts import login_command
    p = accounts.allocate(tmp_path, uuid4())
    command = login_command('antigravity', str(p.home))
    assert 'ttakkari.antigravity_accounts' in command
    assert 'login' in command and str(p.home) in command


def test_probe_directory_is_private_and_not_project_cwd(tmp_path):
    p = accounts.allocate(tmp_path, uuid4())
    cwd = accounts.probe_directory(p.home)
    assert cwd.parent == p.home
    assert cwd.stat().st_mode & 0o777 == 0o700


def test_usage_probe_does_not_claim_identity_without_user_confirmation(tmp_path, monkeypatch):
    p = accounts.allocate(tmp_path, uuid4())
    monkeypatch.setattr(accounts, 'execution_env', lambda home, env, **kw: {'HOME': str(home)})
    monkeypatch.setattr(accounts, '_probe_cli', lambda *a: ('1.4.0', {'status': 'SUCCESS', 'command': {'data': {'groups': []}}}))
    accounts.check(p.home)
    assert accounts.status(p.home)['auth_state'] == 'isolation_unverified'
    assert 'principal' not in accounts.status(p.home) or accounts.status(p.home)['principal'] is None


def test_confirm_requires_successful_usage_and_explicit_identity(tmp_path, monkeypatch):
    p = accounts.allocate(tmp_path, uuid4())
    monkeypatch.setattr(accounts, 'execution_env', lambda home, env, **kw: {'HOME': str(home)})
    monkeypatch.setattr(accounts, '_probe_cli', lambda *a: ('1.4.0', {'status': 'SUCCESS'}))
    accounts.check(p.home, principal='second@example.test', confirm_isolated_login=True)
    state = accounts.status(p.home)
    assert state['auth_state'] == 'ready'
    assert state['principal'] == 'second@example.test'
    assert state['identity_source'] == 'user_confirmed_cli_login'


def test_probe_network_failure_keeps_auth_state_distinct(tmp_path, monkeypatch):
    p = accounts.allocate(tmp_path, uuid4())
    accounts.record_probe(p.home, success=True, cli_version='1.4.0')
    monkeypatch.setattr(accounts, 'execution_env', lambda home, env, **kw: {})
    def fail(*a):
        raise accounts.ProfileError('network_error')
    monkeypatch.setattr(accounts, '_probe_cli', fail)
    with pytest.raises(accounts.ProfileError, match='network_error'):
        accounts.check(p.home)
    assert accounts.status(p.home)['auth_state'] == 'ready'
    assert accounts.status(p.home)['last_error'] == 'network_error'


def test_new_managed_settings_disable_paid_credits_without_host_config(tmp_path, monkeypatch):
    p = accounts.allocate(tmp_path, uuid4())
    vault = Vault()
    monkeypatch.setattr(accounts, '_security', lambda home, *args: str(p.keychain))
    accounts.prepare(p.home, vault=vault)
    import json
    settings = p.home / '.gemini/antigravity-cli/settings.json'
    config = p.home / '.gemini/config/config.json'
    assert json.loads(settings.read_text())['useG1Credits'] is False
    assert json.loads(config.read_text())['userSettings']['useAiCredits'] is False
    assert settings.stat().st_mode & 0o777 == 0o600


def test_runtime_auth_failure_revokes_only_selected_account(tmp_path):
    p = accounts.allocate(tmp_path, uuid4())
    q = accounts.allocate(tmp_path, uuid4())
    for item in (p, q):
        accounts.record_probe(item.home, success=True)
    accounts.record_run(p.home, success=False, error='invalid credentials: session expired')
    assert accounts.status(p.home)['auth_state'] == 'auth_expired'
    assert accounts.status(q.home)['auth_state'] == 'ready'
    accounts.record_run(q.home, success=False, error='network connection timeout')
    assert accounts.status(q.home)['auth_state'] == 'ready'


def test_ambiguous_authentication_or_timeout_does_not_expire_account(tmp_path):
    p = accounts.allocate(tmp_path, uuid4())
    accounts.record_probe(p.home, success=True)
    accounts.record_run(p.home, success=False, error='authentication failed or timed out')
    assert accounts.status(p.home)['auth_state'] == 'ready'


def test_usage_probe_ambiguous_authentication_or_timeout_is_not_auth_failure(tmp_path, monkeypatch):
    import json
    from types import SimpleNamespace

    p = accounts.allocate(tmp_path, uuid4())
    accounts.record_probe(p.home, success=True)
    monkeypatch.setattr(accounts, 'execution_env', lambda home, env, **kw: {'HOME': str(home)})
    outputs = [
        SimpleNamespace(returncode=0, stdout='agy 1.4.0', stderr=''),
        SimpleNamespace(returncode=1, stdout=json.dumps({
            'status': 'ERROR', 'response': 'authentication failed or timed out'
        }), stderr=''),
    ]
    monkeypatch.setattr(accounts.subprocess, 'run', lambda *a, **kw: outputs.pop(0))
    with pytest.raises(accounts.ProfileError, match='network_error'):
        accounts.check(p.home)
    assert accounts.status(p.home)['auth_state'] == 'ready'
    assert accounts.status(p.home)['last_error'] == 'network_error'


def test_usage_probe_timeout_only_is_transient_and_preserves_readiness(tmp_path, monkeypatch):
    import json
    from types import SimpleNamespace

    p = accounts.allocate(tmp_path, uuid4())
    accounts.record_probe(p.home, success=True)
    monkeypatch.setattr(accounts, 'execution_env', lambda home, env, **kw: {'HOME': str(home)})
    outputs = [
        SimpleNamespace(returncode=0, stdout='agy 1.4.0', stderr=''),
        SimpleNamespace(returncode=1, stdout=json.dumps({
            'status': 'ERROR', 'response': 'request timed out'
        }), stderr=''),
    ]
    monkeypatch.setattr(accounts.subprocess, 'run', lambda *a, **kw: outputs.pop(0))
    with pytest.raises(accounts.ProfileError, match='network_error'):
        accounts.check(p.home)
    assert accounts.status(p.home)['auth_state'] == 'ready'
    assert accounts.status(p.home)['last_error'] == 'network_error'


def test_usage_probe_explicit_auth_failure_fails_closed(tmp_path, monkeypatch):
    import json
    from types import SimpleNamespace

    p = accounts.allocate(tmp_path, uuid4())
    accounts.record_probe(p.home, success=True)
    monkeypatch.setattr(accounts, 'execution_env', lambda home, env, **kw: {'HOME': str(home)})
    outputs = [
        SimpleNamespace(returncode=0, stdout='agy 1.4.0', stderr=''),
        SimpleNamespace(returncode=1, stdout=json.dumps({
            'status': 'ERROR', 'response': 'authentication failed'
        }), stderr=''),
    ]
    monkeypatch.setattr(accounts.subprocess, 'run', lambda *a, **kw: outputs.pop(0))
    with pytest.raises(accounts.ProfileError, match='auth_required'):
        accounts.check(p.home)
    assert accounts.status(p.home)['auth_state'] == 'auth_required'


def test_reprovision_can_recover_after_keychain_creation_failure(tmp_path, monkeypatch):
    from ttakkari.security.antigravity_keychain import KeychainError
    p = accounts.allocate(tmp_path, uuid4())
    vault = Vault()
    create = vault.create
    vault.create = lambda path, password: (_ for _ in ()).throw(KeychainError(-4))
    monkeypatch.setattr(accounts, '_security', lambda home, *args: str(p.keychain))
    with pytest.raises(accounts.ProfileError):
        accounts.prepare(p.home, vault=vault)
    vault.create = create
    accounts.prepare(p.home, vault=vault)
    assert accounts.status(p.home)['auth_state'] == 'auth_required'


def test_vault_failure_does_not_leave_an_unrecoverable_keychain(tmp_path, monkeypatch):
    from ttakkari.security.antigravity_keychain import KeychainError
    p = accounts.allocate(tmp_path, uuid4())
    vault = Vault()
    vault.store = lambda *a: (_ for _ in ()).throw(KeychainError(-25308))
    monkeypatch.setattr(accounts, '_security', lambda home, *args: str(p.keychain))
    with pytest.raises(accounts.ProfileError):
        accounts.prepare(p.home, vault=vault)
    assert not p.keychain.exists()


def test_execution_env_reasserts_paid_credits_off(tmp_path, monkeypatch):
    import json
    p = accounts.allocate(tmp_path, uuid4())
    accounts.record_probe(p.home, success=True)
    monkeypatch.setattr(accounts, '_security', lambda home, *args: str(p.keychain))
    p.keychain.parent.parent.mkdir(mode=0o700)
    p.keychain.parent.mkdir(mode=0o700)
    p.keychain.touch(mode=0o600)
    vault = Vault()
    vault.secrets[p.account_id] = b'x' * 48
    settings = p.home / '.gemini/antigravity-cli/settings.json'
    (p.home / '.gemini').mkdir(mode=0o700)
    settings.parent.mkdir(mode=0o700)
    settings.write_text('{"useG1Credits": true, "colorScheme": "dark"}')
    env = accounts.execution_env(p.home, {'GEMINI_API_KEY': 'x', 'PATH': '/bin'}, vault=vault)
    assert env['HOME'] == str(p.home) and 'GEMINI_API_KEY' not in env
    data = json.loads(settings.read_text())
    assert data['useG1Credits'] is False and data['colorScheme'] == 'dark'


def test_prepare_creates_private_preferences_so_default_keychain_persists(tmp_path, monkeypatch):
    p = accounts.allocate(tmp_path, uuid4())
    monkeypatch.setattr(accounts, '_security', lambda home, *args: str(p.keychain))
    accounts.prepare(p.home, vault=Vault())
    prefs = p.home / 'Library' / 'Preferences'
    assert prefs.is_dir() and prefs.stat().st_mode & 0o777 == 0o700
    assert p.keychain.name != 'login.keychain-db'
