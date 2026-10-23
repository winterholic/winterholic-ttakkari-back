"""비밀값 마스킹. 저장 전과 응답 직전에 같은 함수를 쓴다(ADR 0033)."""

from __future__ import annotations

import os
import re
from collections.abc import Iterable
from typing import Any

MASK = "[REDACTED]"
MAX_SCRUB_CHARS = 200_000
_SECRET_ENV_NAME = re.compile(r"secret|token|passw|pwd|api[_-]?key|access[_-]?key|private[_-]?key|credential|cookie|jwt", re.IGNORECASE)

# 키 이름 조각. `tokens`·`token_count`·`author` 같은 오탐은 뒤쪽 lookahead 로 뺀다.
_KEY = (
    r"[\w.\-]{0,60}(?:pass(?:word|wd)?|pwd|secret|token(?!s\b|_count|_limit|_usage|_budget)|api[_-]?key|apikey|access[_-]?key|"
    r"private[_-]?key|credential|cookie|session[_-]?id|jwt|signature|auth(?!or)|refresh)[\w.\-]{0,60}"
)

_PEM = re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?(?:-----END [A-Z ]*PRIVATE KEY-----|\Z)", re.DOTALL)
_URL_USERINFO = re.compile(r"(?i)(\b[a-z][a-z0-9+.\-]*://)[^/\s:@]*:[^/\s@]+@")
_HEADER_LINE = re.compile(
    r"(?im)^(\s*(?:proxy-)?authorization|\s*cookie|\s*set-cookie|\s*x-api-key|\s*x-auth-token|\s*x-ttk-[\w-]*token)(\s*[:=]\s*).*$"
)
_SCHEME = re.compile(r"(?i)\b(Bearer|Basic|Token)\s+[A-Za-z0-9._~+/=\-]{8,}")
_CLI_FLAG = re.compile(
    r"(?i)(--?(?:token|password|passwd|secret|api[-_]?key|access[-_]?key|auth|cookie)[\w\-]*)([ =])(?!-)[^\s]+"
)
# 키는 토큰 시작에서만 시도한다(lookbehind). 안 그러면 긴 단어열에서 시작 위치마다 끝까지 훑어 2차 시간이 된다.
_KV_QUOTED = re.compile(rf"""(?i)(?<![\w.\-])(?P<k>["']?{_KEY}["']?)(?P<sep>\s*[:=]\s*)(?P<q>["'])(?P<v>(?:\\.|(?!(?P=q))[^\\])*)(?:(?P=q)|\Z)""")
_KV_BARE = re.compile(rf"""(?i)(?<![\w.\-])(?P<k>["']?{_KEY}["']?)(?P<sep>\s*[:=]\s*)(?P<v>[^\s,;&"'}}\])]+)""")
_TOKEN_FORMATS = [
    re.compile(p)
    for p in (
        r"\bsk-ant-[A-Za-z0-9_\-]{10,}",
        r"\bsk-[A-Za-z0-9_\-]{16,}",
        r"\bgh[pousr]_[A-Za-z0-9]{20,}",
        r"\bgithub_pat_[A-Za-z0-9_]{20,}",
        r"\bxox[abprs]-[A-Za-z0-9\-]{10,}",
        r"\bAKIA[0-9A-Z]{16}\b",
        r"\bAIza[0-9A-Za-z_\-]{30,}",
        r"\beyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]*",
        r"\bnpm_[A-Za-z0-9]{30,}",
        r"\bglpat-[A-Za-z0-9_\-]{16,}",
        r"\bpypi-[A-Za-z0-9_\-]{20,}",
        r"\bttk_[A-Za-z0-9_\-]{16,}",
    )
]


def known_secrets() -> list[str]:
    """실행 중인 프로세스가 아는 비밀값. 설정 파일을 직접 열지 않고 이미 로드된 설정·환경에서만 모은다."""
    values: set[str] = set()
    for name, value in os.environ.items():
        if _SECRET_ENV_NAME.search(name) and len(value) >= 8:
            values.add(value)
    try:
        from ttakkari.config import get_settings

        for name, value in get_settings().model_dump().items():
            if isinstance(value, str) and len(value) >= 8 and _SECRET_ENV_NAME.search(name):
                values.add(value)
    except Exception:  # noqa: BLE001, S110 -- 마스킹은 설정 로드 실패와 무관하게 동작해야 한다
        pass
    return sorted(values, key=len, reverse=True)


def scrub(text: str, extra: Iterable[str] = ()) -> str:
    """문자열의 비밀값을 `[REDACTED]`로 바꾼다. 여러 번 적용해도 결과가 같다."""
    if not text:
        return text
    if len(text) > MAX_SCRUB_CHARS:
        text = text[:MAX_SCRUB_CHARS] + "…"
    for secret in sorted({*known_secrets(), *extra}, key=len, reverse=True):
        if len(secret) >= 6:
            text = text.replace(secret, MASK)
    text = _PEM.sub(MASK, text)
    text = _URL_USERINFO.sub(rf"\1{MASK}@", text)
    text = _HEADER_LINE.sub(rf"\1\2{MASK}", text)
    text = _SCHEME.sub(rf"\1 {MASK}", text)
    text = _CLI_FLAG.sub(rf"\1\2{MASK}", text)
    text = _KV_QUOTED.sub(lambda m: f"{m['k']}{m['sep']}{m['q']}{MASK}{m['q']}", text)
    text = _KV_BARE.sub(lambda m: m.group(0) if m["v"] == MASK or m["v"].startswith("[REDACTED") else f"{m['k']}{m['sep']}{MASK}", text)
    for pattern in _TOKEN_FORMATS:
        text = pattern.sub(MASK, text)
    return text


_PROSE_KV = re.compile(
    r"""(?i)(?<![\w.\-])(?P<k>[\w.\-]{0,40}(?:secret|passw(?:or)?d|pwd|token(?!s\b)|api[_-]?key|apikey|access[_-]?key"""
    r"""|private[_-]?key|credential)[\w.\-]{0,40})(?P<sep>["']?\s*[:=]\s*)(?P<q>["']?)(?P<v>[^\s"'`,;)\]}]{8,})(?P=q)"""
)
_PROSE_SCHEME = re.compile(r"(?i)\b(Bearer|Basic|Token)\s+([A-Za-z0-9._~+/=\-]{8,})")


def _secret_shaped(value: str) -> bool:
    """문서 속 예시 단어(Authentication 등)와 실제 비밀 값을 가른다: 숫자가 있거나 기호가 섞이거나 아주 길다."""
    has_digit = any(c.isdigit() for c in value)
    return len(value) >= 24 or (has_digit and any(c.isalpha() for c in value)) or (
        has_digit and bool(re.search(r"[._~+/=\-]", value)))


def scrub_prose(text: str, extra: Iterable[str] = ()) -> str:
    """사람이 쓴 문서 본문용 마스킹. 알려진 비밀 값·PEM·URL 자격·알려진 토큰 형식·비밀처럼 생긴 값만 가린다.

    scrub() 는 `token:`·`Basic Auth` 같은 산문까지 가려 문서를 못 읽게 만든다. 형식을 모르는 임의 문자열 비밀은
    못 잡는다는 한계가 있으며, 설정 파일 값은 이 함수가 아니라 구조 마스킹(harness_view.safe.mask_config)이 맡는다."""
    if not text:
        return text
    if len(text) > MAX_SCRUB_CHARS:
        text = text[:MAX_SCRUB_CHARS] + "…"
    for secret in sorted({*known_secrets(), *extra}, key=len, reverse=True):
        if len(secret) >= 6:
            text = text.replace(secret, MASK)
    text = _PEM.sub(MASK, text)
    text = _URL_USERINFO.sub(rf"\1{MASK}@", text)
    text = _HEADER_LINE.sub(rf"\1\2{MASK}", text)
    text = _PROSE_SCHEME.sub(lambda m: f"{m.group(1)} {MASK}" if _secret_shaped(m.group(2)) else m.group(0), text)
    text = _PROSE_KV.sub(
        lambda m: m.group(0) if m["v"].startswith("[REDACTED") or not _secret_shaped(m["v"])
        else f"{m['k']}{m['sep']}{m['q']}{MASK}{m['q']}", text)
    for pattern in _TOKEN_FORMATS:
        text = pattern.sub(MASK, text)
    return text


def scrub_value(value: Any, extra: Iterable[str] = ()) -> Any:
    """dict·list 안의 문자열까지 마스킹한다. 민감한 키 이름의 값은 통째로 가린다."""
    if isinstance(value, str):
        return scrub(value, extra)
    if isinstance(value, dict):
        out = {}
        for k, v in value.items():
            if _is_secret_name(str(k)):
                out[k] = MASK
            else:
                out[k] = scrub_value(v, extra)
        return out
    if isinstance(value, list | tuple):
        return [scrub_value(v, extra) for v in value]
    return value


def _is_secret_name(name: str) -> bool:
    return bool(re.fullmatch(_KEY, name, re.IGNORECASE)) and not re.search(r"tokens\b|token_(?:count|limit|usage|budget)|author", name, re.IGNORECASE)
