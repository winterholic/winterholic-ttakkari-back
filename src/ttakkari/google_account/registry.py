"""Google 계정을 쓰는 기능(consumer) 등록부(ADR 0051).

한 번 로그인한 Google 계정 세션은 여기에 등록된 모든 기능이 공유한다. 기능은 아래 Consumer 하나만 정의하면 된다.
- 어느 앱 화면에서 로그인하고(login_url, app_hosts), 세션에 어떤 쿠키 도메인이 필요한지(cookie_domains).
- 자기 상태를 어떻게 요약하는지(describe). 쿨다운·보호 신호(blocked) 같은 기능 고유 상태는 기능이 자기 저장소에 두고,
  계정 공통 모듈은 읽어서 보여 주기만 한다. 한 기능의 429 가 다른 기능을 막지 않는다.
- 세션이 바뀌었을 때(저장·삭제·재인증 필요) 자기 캐시를 어떻게 비우는지(hooks).
계정 세션 만료(reauth)는 계정 수준 상태라 모든 기능이 같이 본다.
"""

from __future__ import annotations

import contextlib
import importlib
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from urllib.parse import urlparse

from ttakkari.google_account.errors import Invalid

DEFAULT_ACCOUNT = "primary"
BASE_COOKIE_SUFFIXES = ("google.com", "googleusercontent.com")
# 기능이 하나도 등록되지 않은 환경에서 로그인 창이 여는 곳. 계정 관리 화면이라 특정 기능에 묶이지 않는다.
FALLBACK_LOGIN_URL = "https://myaccount.google.com/"
FALLBACK_APP_HOSTS = ("myaccount.google.com",)
_ID_RE = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")

AsyncHook = Callable[[str], Awaitable[None]]
AsyncDict = Callable[[str], Awaitable[dict]]


@dataclass(frozen=True)
class Consumer:
    id: str
    label: str
    description: str
    path: str  # 프런트 화면 경로(상태 카드에서 이동하는 곳)
    login_url: str  # 로그인 창이 여는 앱 화면. 이 호스트에 도착해야 로그인 완료로 본다.
    app_hosts: tuple[str, ...]
    cookie_domains: tuple[str, ...] = ()  # 기본 Google 도메인 외에 세션에 담아야 하는 쿠키 도메인 접미사
    required_cookies: tuple[str, ...] = ()  # 계정 필수 쿠키(SID 등) 외에 이 기능이 요구하는 쿠키 이름
    enabled: Callable[[], bool] = lambda: True
    # describe: {state, message, cooldown_until, retry_after, last_ok_at, ...} 기능별 상태 요약. 쿠키·토큰을 담지 않는다.
    describe: AsyncDict | None = None
    verify: AsyncDict | None = None  # 저장된 세션으로 기능 접속 확인 1회(Google 요청). 반환값은 기능 상태 요약
    resume: AsyncDict | None = None  # 기능의 blocked(보호 신호)를 사람이 확인한 뒤 푼다
    on_session_saved: AsyncHook | None = None  # 로그인으로 세션이 새로 저장됐다
    on_session_removed: AsyncHook | None = None  # 사용자가 세션을 지웠다
    on_reauth: AsyncHook | None = None  # 어느 기능이든 세션 만료를 감지했다

    def validate(self) -> None:
        if not _ID_RE.match(self.id):
            raise ValueError(f"consumer id 형식이 올바르지 않습니다: {self.id!r}")
        url = urlparse(self.login_url)
        if url.scheme != "https" or (url.hostname or "") not in self.app_hosts:
            raise ValueError("login_url 은 https 이고 app_hosts 안의 호스트여야 합니다.")
        for suffix in self.cookie_domains:
            if not suffix or suffix.startswith(".") or "/" in suffix:
                raise ValueError(f"cookie_domains 는 점으로 시작하지 않는 도메인 접미사여야 합니다: {suffix!r}")


@dataclass(frozen=True)
class LoginTarget:
    """로그인 창이 열 주소와 로그인 완료 판정 기준. 기능을 지정하지 않으면 첫 번째 등록 기능을 쓴다."""

    feature: str | None
    label: str
    url: str
    hosts: tuple[str, ...]
    required_cookies: tuple[str, ...] = ()


_consumers: dict[str, Consumer] = {}
_builtin_loaded = False


def _load_builtin() -> None:
    """내장 기능을 한 곳에서 등록한다. 새 기능을 붙이면 여기에 import 한 줄을 더한다."""
    global _builtin_loaded
    if _builtin_loaded:
        return
    _builtin_loaded = True
    try:
        importlib.import_module("ttakkari.notebooklm.consumer")  # import 시 register 한다
    except BaseException:
        _builtin_loaded = False  # 일부만 등록된 채로 굳지 않게 다음 호출에서 다시 시도한다
        raise


def register(consumer: Consumer) -> None:
    consumer.validate()
    _consumers[consumer.id] = consumer


def unregister(consumer_id: str) -> None:
    _consumers.pop(consumer_id, None)


def consumers() -> list[Consumer]:
    _load_builtin()
    return list(_consumers.values())


def get(consumer_id: str) -> Consumer:
    _load_builtin()
    c = _consumers.get(consumer_id)
    if c is None:
        raise Invalid("등록되지 않은 기능입니다.")
    return c


def cookie_suffixes() -> tuple[str, ...]:
    """세션에 담을 쿠키 도메인 접미사 = 기본 Google 도메인 + 등록된 기능들의 요구 도메인."""
    out = list(BASE_COOKIE_SUFFIXES)
    for c in consumers():
        out.extend(s for s in c.cookie_domains if s not in out)
    return tuple(out)


def login_target(feature: str | None) -> LoginTarget:
    if feature:
        c = get(feature)
        return LoginTarget(c.id, c.label, c.login_url, c.app_hosts, c.required_cookies)
    first = consumers()
    if first:
        c = first[0]
        return LoginTarget(c.id, c.label, c.login_url, c.app_hosts, c.required_cookies)
    return LoginTarget(None, "Google 계정", FALLBACK_LOGIN_URL, FALLBACK_APP_HOSTS)


def all_app_hosts() -> tuple[str, ...]:
    hosts: list[str] = list(FALLBACK_APP_HOSTS)
    for c in consumers():
        hosts.extend(h for h in c.app_hosts if h not in hosts)
    return tuple(hosts)


async def run_hook(consumer: Consumer, name: str, account_id: str) -> None:
    """기능 훅 하나가 실패해도 다른 기능과 계정 상태 갱신을 막지 않는다."""
    hook = getattr(consumer, name)
    if hook is None:
        return
    with contextlib.suppress(Exception):
        await hook(account_id)


async def fire(name: str, account_id: str) -> None:
    for c in consumers():
        await run_hook(c, name, account_id)
