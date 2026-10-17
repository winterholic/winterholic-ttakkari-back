"""Managed Antigravity homes: encrypted native credentials, no session sharing.

Manifest contains operational metadata only; OAuth and refresh remain in agy's
own native keychain. Invalid paths and keychain namespaces always fail closed.
"""
from __future__ import annotations

import argparse
import fcntl
import json
import os
import secrets
import shlex
import stat
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

from ttakkari.security.antigravity_keychain import KeychainError, NativeKeychain

ERROR_CODES = {'setup_required','auth_required','auth_expired','keychain_locked','keychain_mismatch',
               'keychain_unavailable','unsafe_path','invalid_profile','cli_unavailable','cli_error',
               'network_error','isolation_unverified','usage_limited'}
SCRUB_ENV = {'SSH_CONNECTION','SSH_CLIENT','SSH_TTY','GEMINI_API_KEY','GOOGLE_API_KEY',
             'GOOGLE_APPLICATION_CREDENTIALS','AGY_ADC_AUTH','AGY_API_KEY',
             'XDG_CONFIG_HOME','XDG_DATA_HOME','XDG_CACHE_HOME','USERPROFILE', 'GEMINI_CLI_HOME'}
MANIFEST = 'ttakkari-antigravity-profile.json'


class ProfileError(ValueError):
    pass


@dataclass(frozen=True)
class ManagedProfile:
    home: Path
    account_id: UUID

    @property
    def manifest(self):
        return self.home / MANIFEST

    @property
    def keychain(self):
        # NOTE: a file literally named like macOS's per-user keychain rejects the password it was created with
        # (observed -25293 on SecKeychainUnlock), so the managed keychain uses its own name.
        return self.home / 'Library' / 'Keychains' / 'ttakkari-agy.keychain-db'


def _safe(path: Path):
    if not path.is_absolute() or any(p.is_symlink() for p in [path, *path.parents]):
        raise ProfileError('unsafe_path')
    if path.exists():
        st = path.stat()
        if st.st_uid != os.getuid() or stat.S_IMODE(st.st_mode) & 0o077:
            raise ProfileError('unsafe_path')


def _write(profile: ManagedProfile, data: dict):
    _safe(profile.home)
    if profile.manifest.is_symlink():
        raise ProfileError('unsafe_path')
    tmp = profile.home / f'.profile-{secrets.token_hex(8)}.tmp'
    fd = os.open(tmp, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        with os.fdopen(fd, 'w') as f:
            json.dump(data, f)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, profile.manifest)
    finally:
        tmp.unlink(missing_ok=True)


def allocate(base: Path, account_id: UUID) -> ManagedProfile:
    home = base / str(account_id) / 'antigravity-home'
    # The shared accounts folder also holds other engines' homes, so it only has to be ours and not writable by others.
    base = base.absolute()
    if any(p.is_symlink() for p in [base, *base.parents]):
        raise ProfileError('unsafe_path')
    base.mkdir(mode=0o700, parents=True, exist_ok=True)
    if base.stat().st_uid != os.getuid() or stat.S_IMODE(base.stat().st_mode) & 0o022:
        raise ProfileError('unsafe_path')
    for directory in [home.parent, home]:
        _safe(directory)
        directory.mkdir(mode=0o700, exist_ok=True)
    profile = ManagedProfile(home, account_id)
    if profile.manifest.exists():
        return load(home)
    _write(profile, {'version':1,'account_id':str(account_id),'auth_state':'setup_required',
                     'last_error':None,'last_success_at':None,'cli_version':None,'checked_at':None})
    return profile


def load(home: Path | str) -> ManagedProfile:
    home = Path(home).absolute()
    _safe(home)
    manifest = home / MANIFEST
    _safe(manifest)
    try:
        data = json.loads(manifest.read_text())
        aid = UUID(data['account_id'])
        if data.get('version') != 1 or home.name != 'antigravity-home' or home.parent.name != str(aid):
            raise ValueError
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise ProfileError('invalid_profile') from exc
    for directory in [home / '.gemini', home / 'Library', home / 'Library/Keychains', home / 'Library/Preferences']:
        if directory.exists() or directory.is_symlink():
            _safe(directory)
    return ManagedProfile(home, aid)


def status(home: Path | str) -> dict:
    p = load(home)
    data = json.loads(p.manifest.read_text())
    keys = {'auth_state','last_error','last_success_at','cli_version','checked_at',
            'principal','identity_source','identity_confirmed_at','usage_checked_at'}
    return {k:v for k,v in data.items() if k in keys}


def is_ready(home: Path | str) -> bool:
    try:
        return status(home)['auth_state'] == 'ready'
    except (OSError, ValueError):
        return False


def _security(home: Path | None, *args: str) -> str:
    env = os.environ.copy()
    if home is not None:
        env['HOME'] = str(home)
    for key in SCRUB_ENV:
        env.pop(key, None)
    r = subprocess.run(['/usr/bin/security', *args], env=env, stdin=subprocess.DEVNULL,
                       capture_output=True, text=True, timeout=10, check=False)
    if r.returncode:
        raise ProfileError('keychain_unavailable')
    return r.stdout.strip()


def _check_namespace(p: ManagedProfile):
    default = shlex.split(_security(p.home, 'default-keychain', '-d', 'user'))
    search = shlex.split(_security(p.home, 'list-keychains', '-d', 'user'))
    expected = str(p.keychain)
    if default != [expected] or search != [expected]:
        raise ProfileError('keychain_mismatch')


def _write_json_private(path: Path, data: dict):
    if path.is_symlink():
        raise ProfileError('unsafe_path')
    # mkdir(parents=True) ignores mode for intermediate directories, so create each level privately.
    missing = [d for d in (path.parent.parent, path.parent) if not d.exists()]
    for directory in missing:
        directory.mkdir(mode=0o700)
    _safe(path.parent)
    tmp = path.parent / f'.{path.name}-{secrets.token_hex(8)}.tmp'
    fd = os.open(tmp, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        with os.fdopen(fd, 'w') as f:
            json.dump(data, f)
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def _merge_json(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text())
    except ValueError as exc:
        raise ProfileError('invalid_profile') from exc
    if not isinstance(data, dict):
        raise ProfileError('invalid_profile')
    return data


def _write_safe_settings(p: ManagedProfile):
    """Paid AI credits stay off in every managed home, independent of any host configuration."""
    settings = p.home / '.gemini' / 'antigravity-cli' / 'settings.json'
    config = p.home / '.gemini' / 'config' / 'config.json'
    data = _merge_json(settings)
    data['useG1Credits'] = False
    _write_json_private(settings, data)
    data = _merge_json(config)
    user = data.get('userSettings')
    data['userSettings'] = {**(user if isinstance(user, dict) else {}), 'useAiCredits': False}
    _write_json_private(config, data)


def prepare(home: Path | str, *, vault=None):
    p = load(home)
    vault = vault or NativeKeychain()
    # Serialize provisioning only; executions never mutate a search/default list.
    lock_path = p.home.parent.parent / '.keychain-provision.lock'
    if lock_path.is_symlink():
        raise ProfileError('unsafe_path')
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR | getattr(os,'O_NOFOLLOW',0), 0o600)
    with os.fdopen(fd, 'w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        current = status(p.home)
        try:
            # Preferences must exist: without it `security default-keychain -s` reports success but persists nothing.
            for folder in [p.keychain.parent.parent, p.keychain.parent, p.keychain.parent.parent / 'Preferences']:
                _safe(folder)
                folder.mkdir(mode=0o700, exist_ok=True)
            if not p.keychain.exists():
                password = secrets.token_bytes(48)
                vault.create(p.keychain, password)
                try:
                    vault.store(p.account_id, password)
                except BaseException:
                    # Without the unlock secret this keychain can never be reopened; allow a clean retry.
                    p.keychain.unlink(missing_ok=True)
                    raise
            else:
                _safe(p.keychain)
                vault.unlock(p.keychain, vault.load(p.account_id))
            _security(p.home, 'default-keychain', '-d','user','-s',str(p.keychain))
            _security(p.home, 'list-keychains', '-d','user','-s',str(p.keychain))
            _check_namespace(p)
            _write_safe_settings(p)
        except ProfileError as exc:
            record_probe(p.home, success=False, error_code=str(exc))
            raise
        except (KeychainError, OSError, TimeoutError, subprocess.SubprocessError) as exc:
            record_probe(p.home, success=False, error_code='keychain_unavailable')
            raise ProfileError('keychain_unavailable') from exc
        if current['auth_state'] != 'ready':
            record_probe(p.home, success=False, error_code='auth_required')


def execution_env(home: Path | str, base_env: dict[str,str], *, vault=None, allow_login=False) -> dict[str,str]:
    p = load(home)
    current = status(p.home)['auth_state']
    if current != 'ready' and not (allow_login and current in {'auth_required','auth_expired','isolation_unverified'}):
        raise ProfileError(current if current in ERROR_CODES else 'auth_required')
    _check_namespace(p)
    _safe(p.keychain)
    try:
        vault = vault or NativeKeychain()
        vault.unlock(p.keychain, vault.load(p.account_id))
    except (KeychainError, OSError) as exc:
        record_probe(p.home, success=False, error_code='keychain_locked')
        raise ProfileError('keychain_locked') from exc
    _write_safe_settings(p)
    env = {k:v for k,v in base_env.items() if k not in SCRUB_ENV}
    env['HOME'] = str(p.home)
    env['AGY_CLI_DISABLE_AUTO_UPDATE'] = 'true'
    return env


def probe_directory(home: Path | str) -> Path:
    p = load(home)
    directory = p.home / 'probe-workspace'
    _safe(directory)
    directory.mkdir(mode=0o700, exist_ok=True)
    return directory


def _probe_cli(home: Path | str, env: dict) -> tuple[str, dict]:
    from ttakkari.config import get_settings
    binary = get_settings().antigravity_bin
    try:
        version = subprocess.run([binary, '--version'], env=env, cwd=probe_directory(home),
                                 capture_output=True, timeout=15, text=True, stdin=subprocess.DEVNULL, check=False)
        result = subprocess.run([binary, '--print', '/usage', '--output-format', 'json', '--log-file', os.devnull],
                                env=env, cwd=probe_directory(home), capture_output=True, timeout=45,
                                text=True, stdin=subprocess.DEVNULL, check=False)
    except FileNotFoundError as exc:
        raise ProfileError('cli_unavailable') from exc
    except subprocess.TimeoutExpired as exc:
        raise ProfileError('network_error') from exc
    try:
        body = json.loads(result.stdout)
    except ValueError as exc:
        raise ProfileError('cli_error') from exc
    if not isinstance(body, dict):
        raise ProfileError('cli_error')
    if result.returncode or body.get('status') != 'SUCCESS':
        # Never persist or echo the CLI response: OAuth codes may appear there.
        message = str(body.get('response', '')).lower()
        # The CLI uses this combined message for either auth trouble or a
        # transport timeout; it cannot safely revoke account readiness.
        if 'authentication failed' in message and ('timed out' in message or 'timeout' in message):
            raise ProfileError('network_error')
        if any(t in message for t in ('not logged in','login required','not authenticated','authentication failed','sign in')):
            raise ProfileError('auth_required')
        if 'timed out' in message or 'timeout' in message:
            # Fail this usage check, but keep a previously ready account eligible for a later attempt.
            raise ProfileError('network_error')
        raise ProfileError('cli_error')
    if version.returncode:
        raise ProfileError('cli_error')
    return version.stdout.strip()[:64], body


def check(home: Path | str, *, principal: str | None = None, confirm_isolated_login: bool = False) -> dict:
    p = load(home)
    if confirm_isolated_login and (not principal or len(principal) > 254 or any(ord(c) < 32 for c in principal)):
        raise ProfileError('invalid_profile')
    try:
        env = execution_env(p.home, os.environ.copy(), allow_login=True)
        version, _body = _probe_cli(p.home, env)
    except ProfileError as exc:
        code = str(exc)
        if code not in ERROR_CODES:
            code = 'cli_error'
        if code in {'auth_required','auth_expired','keychain_locked','keychain_mismatch','unsafe_path'}:
            record_probe(p.home, success=False, error_code=code)
        else:
            data = json.loads(p.manifest.read_text())
            data.update(last_error=code, checked_at=time.time())
            _write(p, data)
        raise
    data = json.loads(p.manifest.read_text())
    data.update(cli_version=version, usage_checked_at=time.time(), checked_at=time.time(), last_error=None)
    if confirm_isolated_login:
        data.update(principal=principal, identity_source='user_confirmed_cli_login',
                    identity_confirmed_at=time.time(), auth_state='ready')
    elif not data.get('identity_confirmed_at'):
        data['auth_state'] = 'isolation_unverified'
    _write(p, data)
    return status(p.home)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description='Managed Antigravity profile; no Google credential import/export')
    parser.add_argument('command', choices=['setup','login','check','status','disable'])
    parser.add_argument('home', type=Path)
    parser.add_argument('--principal')
    parser.add_argument('--confirm-isolated-login', action='store_true')
    args = parser.parse_args(argv)
    try:
        p = load(args.home)
        if args.command == 'setup':
            prepare(p.home)
        elif args.command == 'login':
            from ttakkari.config import get_settings
            # Reauthentication immediately revokes any previous identity approval.
            prepare(p.home)
            data = json.loads(p.manifest.read_text())
            for key in ('principal','identity_source','identity_confirmed_at'):
                data.pop(key, None)
            data.update(auth_state='auth_required')
            _write(p, data)
            env = execution_env(p.home, os.environ.copy(), allow_login=True)
            # Interactive only when explicitly invoked by the human from a terminal.
            return subprocess.call([get_settings().antigravity_bin, '--log-file', os.devnull],
                                   cwd=probe_directory(p.home), env=env)
        elif args.command == 'check':
            check(p.home, principal=args.principal, confirm_isolated_login=args.confirm_isolated_login)
        elif args.command == 'disable':
            record_probe(p.home, success=False, error_code='auth_required')
        print(json.dumps(status(p.home), ensure_ascii=False))
        return 0
    except (ProfileError, KeychainError, OSError, subprocess.SubprocessError) as exc:
        code = str(exc) if isinstance(exc, ProfileError) else 'keychain_unavailable' if isinstance(exc, KeychainError) else 'cli_error'
        print(json.dumps({'error': code}), file=sys.stderr)
        return 1


_AUTH_FAILURE = ('authentication failed','not logged in','login required','not authenticated',
                 'unauthenticated','invalid credentials','token expired','sign in','401')


def record_run(home: Path | str, *, success: bool, error: str | None = None):
    """Runtime outcome of one account. Only authentication failures change auth_state; the text is never stored."""
    p = load(home)
    if success:
        data = json.loads(p.manifest.read_text())
        data.update(last_error=None, last_success_at=time.time())
        _write(p, data)
        return
    text = (error or '').lower()
    # `agy` reports an ambiguous auth-or-timeout message for both classes.
    # The current Run still fails; preserve readiness so later Runs may retry it.
    if 'authentication failed' in text and ('timed out' in text or 'timeout' in text):
        return
    if any(t in text for t in _AUTH_FAILURE):
        record_probe(p.home, success=False, error_code='auth_expired')


def record_probe(home: Path | str, *, success: bool, cli_version: str | None = None, error_code: str | None = None):
    if error_code is not None and error_code not in ERROR_CODES:
        raise ValueError('invalid error code')
    p = load(home)
    data = json.loads(p.manifest.read_text())
    data.update(auth_state='ready' if success else (error_code or 'cli_error'),
                last_error=None if success else (error_code or 'cli_error'), checked_at=time.time())
    if cli_version is not None:
        data['cli_version'] = cli_version[:64]
    if success:
        data['last_success_at'] = time.time()
    _write(p,data)


if __name__ == '__main__':
    sys.exit(main())
