"""쿠키·CSRF·세션 토큰 마스킹. 로그·예외·API 응답에 나가는 모든 문자열은 mask() 를 거친다(ADR 0045).

두 겹으로 막는다. (1) 세션 파일에서 읽은 실제 값을 등록해 문자열 그대로 치환한다.
(2) 값을 모르는 경우를 위해 쿠키 이름=값, 헤더, JSON 필드, 긴 토큰 모양을 패턴으로 치환한다.
"""

from __future__ import annotations

import re
import threading

MASK = "[masked]"
MAX_ERROR_CHARS = 300

_COOKIE_NAMES = (
    "SID|HSID|SSID|APISID|SAPISID|OSID|LSID|NID|SIDCC|AEC|SMSV|"
    r"__Secure-[0-9A-Za-z_]+|__Host-[0-9A-Za-z_]+|SNlM0e|FdrFJe|cfb2h|csrf[_-]?token|session[_-]?id|at"
)
_PATTERNS = [
    # Cookie: / Set-Cookie: 헤더 줄 전체
    re.compile(r"(?i)\b(set-)?cookie\s*:\s*[^\r\n]+"),
    re.compile(r"(?i)\bauthorization\s*:\s*[^\r\n]+"),
    # name=value 쿠키 조각, "name":"value" JSON 조각
    re.compile(rf"\b(?:{_COOKIE_NAMES})\s*=\s*[^;\s,'\"&]+"),
    re.compile(rf"(?i)[\"'](?:{_COOKIE_NAMES}|value)[\"']\s*:\s*[\"'][^\"']{{6,}}[\"']"),
    # at=<토큰> 형태 폼 필드와 URL 쿼리의 토큰
    re.compile(r"(?i)([?&](?:at|token|key|sid)=)[^&\s]+"),
    # 길고 무작위로 보이는 토큰(32자 이상 영숫자/-_/.)
    re.compile(r"(?<![0-9A-Za-z])[0-9A-Za-z_\-./+=]{40,}(?![0-9A-Za-z])"),
]
_lock = threading.Lock()
_secrets: set[str] = set()


def register_secrets(values: list[str] | set[str] | tuple[str, ...]) -> None:
    """세션에서 읽은 실제 값을 등록한다. 이 값이 어떤 문자열에 들어 있으면 그대로 치환한다."""
    with _lock:
        _secrets.update(v for v in values if isinstance(v, str) and len(v) >= 6)


def clear_secrets() -> None:
    with _lock:
        _secrets.clear()


def mask(text: object) -> str:
    s = str(text)
    with _lock:
        known = sorted(_secrets, key=len, reverse=True)
    for secret in known:
        if secret in s:
            s = s.replace(secret, MASK)
    for pat in _PATTERNS:
        if pat.groups:
            s = pat.sub(lambda m: (m.group(1) + MASK) if m.lastindex else MASK, s)
        else:
            s = pat.sub(MASK, s)
    return s


def safe_error(exc: BaseException, *, limit: int = MAX_ERROR_CHARS) -> str:
    """예외를 한 줄 요약으로. 클래스 이름과 마스킹·길이 제한한 메시지만 남기고 traceback·응답 원문은 버린다."""
    msg = mask(str(exc)).replace("\n", " ").strip()
    out = f"{type(exc).__name__}: {msg}" if msg else type(exc).__name__
    return out[:limit]
