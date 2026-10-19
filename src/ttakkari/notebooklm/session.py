"""NotebookLM 로그인 세션 파일 저장(ADR 0045).

쿠키는 Playwright 호환 storage_state.json 으로 <home>/notebooklm/accounts/<account_id>/ 에 둔다.
상류 공개 API(`NotebookLMClient.from_storage(path=...)`)가 읽는 형식이다.

보호는 파일 권한뿐이다. 디렉터리 0700, 파일 0600, 임시 파일에 쓴 뒤 원자적으로 교체한다.
따까리에는 이 파일을 암호화할 키 체계가 없다(기존 키체인 코드는 Antigravity 전용이고 열람 금지 대상이라 쓰지 않는다).
그러므로 같은 macOS 사용자 계정의 다른 프로세스는 읽을 수 있다. 암호화된 저장이라고 주장하지 않는다.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import shutil
from pathlib import Path
from typing import Any

from ttakkari.config import get_settings
from ttakkari.notebooklm import redact
from ttakkari.notebooklm.errors import AuthRequired, Invalid

DEFAULT_ACCOUNT = "primary"
_ACCOUNT_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")
STORAGE_NAME = "storage_state.json"
# 상류가 세션으로 인정하는 최소 쿠키. 하나라도 없으면 로그인 완료로 보지 않는다.
REQUIRED_COOKIES = ("SID", "__Secure-1PSIDTS")
_GOOGLE_SUFFIXES = ("google.com", "googleusercontent.com")


def check_account(account_id: str) -> str:
    if not _ACCOUNT_RE.match(account_id or ""):
        raise Invalid("계정 ID 형식이 올바르지 않습니다.")
    return account_id


def account_dir(account_id: str) -> Path:
    return get_settings().notebooklm_dir / "accounts" / check_account(account_id)


def storage_path(account_id: str) -> Path:
    return account_dir(account_id) / STORAGE_NAME


def uploads_dir() -> Path:
    return get_settings().notebooklm_dir / "uploads"


def _private_dir(path: Path) -> None:
    """path 와 ~/.ttakkari/notebooklm 까지의 상위를 0700 으로 만든다."""
    root = get_settings().notebooklm_dir
    path.mkdir(parents=True, exist_ok=True)
    for d in (path, *path.parents):
        if d == root.parent:
            break
        with contextlib.suppress(OSError):
            d.chmod(0o700)
        if d == root:
            break


def is_google(domain: str) -> bool:
    d = (domain or "").lstrip(".").lower()
    return any(d == s or d.endswith("." + s) for s in _GOOGLE_SUFFIXES)


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
    """CDP 쿠키 목록에서 구글 도메인 쿠키만 골라 storage_state 로 만든다. 필수 쿠키가 없으면 AuthRequired."""
    kept = [
        _to_storage_cookie(c)
        for c in cookies
        if isinstance(c, dict) and c.get("name") and isinstance(c.get("value"), str) and is_google(c.get("domain", ""))
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
    return storage_path(account_id).is_file()


def cookie_names(account_id: str) -> list[str]:
    """저장된 쿠키 이름만. 값은 읽어도 반환하지 않는다."""
    try:
        data = json.loads(storage_path(account_id).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return sorted({c.get("name", "") for c in data.get("cookies", []) if isinstance(c, dict)} - {""})


def load_secrets(account_id: str) -> None:
    """세션 파일의 실제 값을 마스킹 등록한다(프로세스 기동 후 첫 사용 전에 호출)."""
    try:
        data = json.loads(storage_path(account_id).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    redact.register_secrets([c.get("value", "") for c in data.get("cookies", []) if isinstance(c, dict)])


def mode_ok(account_id: str) -> bool:
    """파일 0600 · 디렉터리 0700 인가. 상태 화면에서 권한이 풀렸는지 알리는 용도."""
    p = storage_path(account_id)
    try:
        return (p.stat().st_mode & 0o077) == 0 and (p.parent.stat().st_mode & 0o077) == 0
    except OSError:
        return False


def delete(account_id: str) -> None:
    shutil.rmtree(account_dir(account_id), ignore_errors=True)
