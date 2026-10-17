"""macOS encrypted profile keychains; only Ttakkari's own unlock secret is read.

No Google OAuth token is accessed here. Keychain creation runs in a separate
process with its own HOME so SecKeychainCreate cannot alter the host search list.
The classic Apple APIs remain the public mechanism for custom file keychains
(SecKeychain.h in the installed macOS SDK); they are deprecated but still shipped.
"""
from __future__ import annotations

import ctypes as C
import json
import os
import subprocess
import sys
from pathlib import Path
from uuid import UUID

SERVICE = b'net.winterholic.ttakkari.antigravity.unlock.v1'


class KeychainError(RuntimeError):
    def __init__(self, status: int):
        self.status = status
        super().__init__(f'native_keychain_error:{status}')


class _API:
    def __init__(self):
        if sys.platform != 'darwin':
            raise KeychainError(-4)
        self.lib = C.CDLL('/System/Library/Frameworks/Security.framework/Security')
        self.cf = C.CDLL('/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation')
        p, u, b, i = C.c_void_p, C.c_uint32, C.c_ubyte, C.c_int32
        signatures = {
            'SecKeychainCreate': [C.c_char_p, u, p, b, p, C.POINTER(p)],
            'SecKeychainOpen': [C.c_char_p, C.POINTER(p)],
            'SecKeychainCopyDefault': [C.POINTER(p)],
            'SecKeychainUnlock': [p, u, p, b],
            'SecKeychainAddGenericPassword': [p,u,C.c_char_p,u,C.c_char_p,u,p,C.POINTER(p)],
            'SecKeychainFindGenericPassword': [p,u,C.c_char_p,u,C.c_char_p,C.POINTER(u),C.POINTER(p),C.POINTER(p)],
            'SecKeychainItemFreeContent': [p,p],
            'SecKeychainSetUserInteractionAllowed': [b],
        }
        for name, args in signatures.items():
            fn = getattr(self.lib, name)
            fn.argtypes, fn.restype = args, i
        self.cf.CFRelease.argtypes = [p]
        self.cf.CFRelease.restype = None
        self.check(self.lib.SecKeychainSetUserInteractionAllowed(False))

    @staticmethod
    def check(status):
        if status:
            raise KeychainError(int(status))

    def open(self, path: Path | None):
        ref = C.c_void_p()
        rc = self.lib.SecKeychainOpen(os.fsencode(path), C.byref(ref)) if path else self.lib.SecKeychainCopyDefault(C.byref(ref))
        self.check(rc)
        return ref


class NativeKeychain:
    """Vault defaults to the user's native default keychain. Never copies Google auth."""
    def __init__(self, vault_path: Path | None = None):
        self.vault_path = vault_path

    def create(self, path: Path, password: bytes):
        if path.exists() or path.is_symlink():
            raise KeychainError(-25299)
        env = os.environ.copy()
        env['HOME'] = str(path.parents[2])
        # The password crosses a private pipe, never argv, environment or disk.
        proc = subprocess.run([sys.executable, str(Path(__file__).resolve()), 'create', str(path)],
                              input=password, env=env, capture_output=True, timeout=20, check=False)
        if proc.returncode:
            try:
                observed = json.loads(proc.stdout).get('os_status')
            except (ValueError, AttributeError):
                observed = None
            raise KeychainError(observed if type(observed) is int else -4)
        path.chmod(0o600)

    def store(self, account_id: UUID, password: bytes):
        api, account = _API(), str(account_id).encode()
        ref = api.open(self.vault_path)
        try:
            api.check(api.lib.SecKeychainAddGenericPassword(ref, len(SERVICE), SERVICE,
                       len(account), account, len(password), password, None))
        finally:
            api.cf.CFRelease(ref)

    def load(self, account_id: UUID) -> bytes:
        api, account = _API(), str(account_id).encode()
        ref, length, data = api.open(self.vault_path), C.c_uint32(), C.c_void_p()
        try:
            api.check(api.lib.SecKeychainFindGenericPassword(ref, len(SERVICE), SERVICE,
                      len(account), account, C.byref(length), C.byref(data), None))
            try:
                return C.string_at(data, length.value)
            finally:
                api.lib.SecKeychainItemFreeContent(None, data)
        finally:
            api.cf.CFRelease(ref)

    def unlock(self, path: Path, password: bytes):
        api = _API()
        ref = api.open(path)
        try:
            api.check(api.lib.SecKeychainUnlock(ref, len(password), password, True))
        finally:
            api.cf.CFRelease(ref)


def _main() -> int:
    # Only explicit creation in a private HOME is supported by this helper.
    if len(sys.argv) != 3 or sys.argv[1] != 'create':
        return 2
    path = Path(sys.argv[2])
    if path.exists() or path.is_symlink() or path.parents[2] != Path(os.environ.get('HOME', '')):
        return 2
    password = sys.stdin.buffer.read(129)
    if not 32 <= len(password) <= 128:
        return 2
    api, ref = _API(), C.c_void_p()
    try:
        api.check(api.lib.SecKeychainCreate(os.fsencode(path), len(password), password, False, None, C.byref(ref)))
        return 0
    except KeychainError as exc:
        print(json.dumps({'code': 'native_keychain_error', 'os_status': exc.status}))
        return 1
    finally:
        if ref:
            api.cf.CFRelease(ref)


if __name__ == '__main__':
    sys.exit(_main())
