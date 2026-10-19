"""Google 계정 로그인 세션 파일 저장(ADR 0045, 0051).

쿠키는 Playwright 호환 storage_state.json 으로 <home>/google-accounts/<account_id>/ 에 둔다.
같은 디렉터리의 chrome-profile/ 은 그 계정 전용 Chrome 프로필이다. 계정마다 디렉터리가 달라 세션이 섞이지 않는다.
상류 공개 API(NotebookLM 의 from_storage(path=...) 등)가 이 파일을 읽는다.

보호는 파일 권한뿐이다. 디렉터리 0700, 파일 0600, 임시 파일에 쓴 뒤 원자적으로 교체한다.
따까리에는 이 파일을 암호화할 키 체계가 없다(기존 키체인 코드는 Antigravity 전용이고 열람 금지 대상이라 쓰지 않는다).
그러므로 같은 macOS 사용자 계정의 다른 프로세스는 읽을 수 있다. 암호화된 저장이라고 주장하지 않는다.

NotebookLM 단독 시절의 위치(<home>/notebooklm/accounts/<id>/storage_state.json, <home>/notebooklm-chrome)에 있던
세션과 프로필은 처음 쓰일 때 새 위치로 옮기고 옛 경로에는 심볼릭 링크를 남긴다. 무중단 배포 중 이전 세대가 같은 파일을 읽기 때문이다.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
from pathlib import Path
from typing import Any

from ttakkari.config import get_settings
from ttakkari.google_account import redact, registry
from ttakkari.google_account.errors import AuthRequired, Invalid

DEFAULT_ACCOUNT = registry.DEFAULT_ACCOUNT
_ACCOUNT_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")
STORAGE_NAME = "storage_state.json"
PROFILE_NAME = "chrome-profile"
# 상류가 세션으로 인정하는 최소 쿠키. 하나라도 없으면 로그인 완료로 보지 않는다.
REQUIRED_COOKIES = ("SID", "__Secure-1PSIDTS")


def check_account(account_id: str) -> str:
    if not _ACCOUNT_RE.match(account_id or ""):
        raise Invalid("계정 ID 형식이 올바르지 않습니다.")
    return account_id


def root_dir() -> Path:
    return get_settings().google_accounts_dir


def account_dir(account_id: str) -> Path:
    return root_dir() / check_account(account_id)


def storage_path(account_id: str) -> Path:
    return account_dir(account_id) / STORAGE_NAME


def profile_dir(account_id: str) -> Path:
    """그 계정 전용 Chrome 프로필. 예전 설정(notebooklm_profile)을 명시해 둔 환경은 기본 계정에 한해 그 경로를 유지한다."""
    explicit = get_settings().notebooklm_profile
    if explicit is not None and check_account(account_id) == DEFAULT_ACCOUNT:
        return Path(explicit).expanduser().resolve()
    return account_dir(account_id) / PROFILE_NAME


def known_account_ids() -> list[str]:
    """디스크에 디렉터리가 있는 계정 ID(형식이 맞는 것만)."""
    root = root_dir()
    if not root.is_dir():
        return []
    return sorted(p.name for p in root.iterdir() if p.is_dir() and _ACCOUNT_RE.match(p.name))


def _private_dir(path: Path) -> None:
    """path 와 <home>/google-accounts 까지의 상위를 0700 으로 만든다."""
    root = root_dir()
    path.mkdir(parents=True, exist_ok=True)
    for d in (path, *path.parents):
        if d == root.parent:
            break
        d.chmod(0o700)
        if d == root:
            break


# ------------------------------------------------------------------ 옛 NotebookLM 위치에서 이전

def legacy_storage_path(account_id: str) -> Path:
    return get_settings().notebooklm_dir / "accounts" / check_account(account_id) / STORAGE_NAME


def legacy_profile_path() -> Path | None:
    """옛 기본 프로필 경로. 설정으로 경로를 직접 지정한 환경은 옮길 것이 없다."""
    s = get_settings()
    return None if s.notebooklm_profile is not None else s.home / "notebooklm-chrome"


def adopt_legacy_session(account_id: str) -> bool:
    """옛 위치의 일반 파일이면 새 위치로 옮기고 옛 경로에는 새 파일을 가리키는 링크를 남긴다. 옮겼으면 True."""
    new = storage_path(account_id)
    old = legacy_storage_path(account_id)
    try:
        if new.is_file() or old.is_symlink() or not old.is_file():
            return False
        _private_dir(new.parent)
        os.replace(old, new)
        os.chmod(new, 0o600)
        os.symlink(new, old)
        return True
    except OSError:
        return False


def adopt_legacy_profile(account_id: str, *, in_use: bool) -> bool:
    """옛 기본 프로필 디렉터리를 새 위치로 옮긴다(기본 계정만). Chrome 이 쓰는 중이면 건드리지 않는다."""
    old = legacy_profile_path()
    if old is None or check_account(account_id) != DEFAULT_ACCOUNT or in_use:
        return False
    new = profile_dir(account_id)
    try:
        if old.is_symlink() or not old.is_dir() or new.exists():
            return False
        _private_dir(new.parent)
        os.rename(old, new)
        os.symlink(new, old)
        return True
    except OSError:
        return False


# ------------------------------------------------------------------ 쿠키 저장

def is_in_scope(domain: str, suffixes: tuple[str, ...] | None = None) -> bool:
    d = (domain or "").lstrip(".").lower()
    return any(d == s or d.endswith("." + s) for s in (suffixes or registry.cookie_suffixes()))


def is_google(domain: str) -> bool:
    return is_in_scope(domain)


def _to_storage_cookie(c: dict[str, Any]) -> dict[str, Any]:
    expires = c.get("expires")
    same = {"Strict": "Strict", "Lax": "Lax", "None": "None"}.get(str(c.get("sameSite") or ""), "Lax")
    return {
        "name": c["name"],
        "value": c["value"],
        "domain": c["domain"],
        "path": c.get("path") or "/",
        "expires": float(expires) if isinstance(expires, (int, float)) and expires > 0 else -1,
        "httpOnly": bool(c.get("httpOnly", False)),
        "secure": bool(c.get("secure", False)),
        "sameSite": same,
    }


def build_storage_state(cookies: list[dict[str, Any]]) -> dict[str, Any]:
    """CDP 쿠키 목록에서 등록된 기능들의 도메인 범위 쿠키만 골라 storage_state 로 만든다. 필수 쿠키가 없으면 AuthRequired."""
    suffixes = registry.cookie_suffixes()
    kept = [
        _to_storage_cookie(c)
        for c in cookies
        if isinstance(c, dict) and c.get("name") and isinstance(c.get("value"), str)
        and is_in_scope(c.get("domain", ""), suffixes)
    ]
    names = {c["name"] for c in kept}
    if not all(n in names for n in REQUIRED_COOKIES):
        raise AuthRequired("로그인 세션에 필요한 쿠키가 아직 없습니다.")
    return {"cookies": kept, "origins": []}


def save_cookies(account_id: str, cookies: list[dict[str, Any]]) -> list[str]:
    """쿠키를 원자적으로 교체 저장한다. 저장한 쿠키 이름 목록(값 아님)을 돌려준다."""
    state = build_storage_state(cookies)
    target = storage_path(account_id)
    _private_dir(target.parent)
    tmp = target.with_name(f".{STORAGE_NAME}.{os.getpid()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(state, f)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, target)
    finally:
        with contextlib.suppress(OSError):
            tmp.unlink()
    redact.register_secrets([c["value"] for c in state["cookies"]])
    return sorted({c["name"] for c in state["cookies"]})


def exists(account_id: str) -> bool:
    p = storage_path(account_id)
    if p.is_file():
        return True
    adopt_legacy_session(account_id)
    return p.is_file()


def _read(account_id: str) -> dict[str, Any]:
    try:
        data = json.loads(storage_path(account_id).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def cookie_names(account_id: str) -> list[str]:
    """저장된 쿠키 이름만. 값은 읽어도 반환하지 않는다."""
    return sorted({c.get("name", "") for c in _read(account_id).get("cookies", []) if isinstance(c, dict)} - {""})


def covers(account_id: str, suffixes: tuple[str, ...]) -> bool:
    """저장된 세션에 이 도메인 범위의 쿠키가 하나라도 있는가. 나중에 등록된 기능은 이전 로그인에서 범위가 빠졌을 수 있다."""
    return any(
        is_in_scope(c.get("domain", ""), suffixes)
        for c in _read(account_id).get("cookies", []) if isinstance(c, dict)
    )


def load_secrets(account_id: str) -> None:
    """세션 파일의 실제 값을 마스킹 등록한다(프로세스 기동 후 첫 사용 전에 호출)."""
    redact.register_secrets(
        [c.get("value", "") for c in _read(account_id).get("cookies", []) if isinstance(c, dict)])


def mode_ok(account_id: str) -> bool:
    """파일 0600 · 디렉터리 0700 인가. 상태 화면에서 권한이 풀렸는지 알리는 용도."""
    p = storage_path(account_id)
    try:
        return (p.stat().st_mode & 0o077) == 0 and (p.parent.stat().st_mode & 0o077) == 0
    except OSError:
        return False


def delete(account_id: str) -> None:
    """세션 파일(과 옛 위치의 링크·파일)을 지운다. Chrome 프로필은 두어 다시 로그인할 때 Google 쪽 로그인 상태를 이어 쓴다."""
    d = account_dir(account_id)
    for p in (storage_path(account_id), legacy_storage_path(account_id)):
        with contextlib.suppress(OSError):
            p.unlink()
    for leftover in d.glob(f".{STORAGE_NAME}.*.tmp") if d.is_dir() else ():
        with contextlib.suppress(OSError):
            leftover.unlink()
    with contextlib.suppress(OSError):
        d.rmdir()  # 프로필이 없을 때만 비어 있어 지워진다
