"""토스증권(tossinvest.com) 읽기 전용 브라우저 어댑터(ADR 0044).

공개 API가 없고 계정 영역은 로그인 세션이 있어야 보인다. 클로바노트 선례(ADR 0021)의
교훈을 재사용한다: 평소 Chrome 프로필은 자동화에 쓸 수 없고(Chrome 136+ 기본 프로필
자동화 차단, macOS TCC 읽기 차단), 로그인은 자동화 플래그 없는 일반 Chrome 창으로
사람이 한 번 하며, 브라우저 조종은 Playwright 같은 별도 의존성 없이 설치된 Chrome +
CDP(websockets·psutil·httpx는 이미 앱 의존성)로 한다.

읽기 전용 강제:
- HTTP 재호출은 GET만, 호스트는 www/api.tossinvest.com·corp 문서 API만.
- 주문·매수·매도·이체·출금·입금·설정·인증 변경 냄새가 나는 경로는 거부한다.
- CDP Runtime.evaluate는 아래 고정 스니펫만 실행한다(문자열 합성·클릭·입력 없음).
- 자동 새로고침·스케줄이 없다. 수동 트리거 + 서버 최소 간격이 사람 수준 빈도를 강제한다.

쿠키·세션 값은 어디에도 기록·반환하지 않는다. 상태·스냅샷에는 불리언·개수·제목만 둔다.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import shutil
import sqlite3
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import psutil

from ttakkari.config import get_settings

log = logging.getLogger(__name__)

TOSS_HOME = "https://www.tossinvest.com/"
ALLOWED_HOSTS = frozenset(
    {
        "www.tossinvest.com",
        "api.tossinvest.com",
        "corp.tossinvest.com",
        "docs-api.tossinvest.com",
    }
)
# 계정 상태를 바꿀 수 있는 경로 힌트. 하나라도 들어간 URL은 읽기라도 호출하지 않는다.
BLOCKED_PATH_HINTS = frozenset(
    {
        "order",
        "trade",
        "buy",
        "sell",
        "transfer",
        "withdraw",
        "deposit",
        "remit",
        "exchange",
        "settings",
        "password",
        "certif",
        "otp",
        "2fa",
        "agree",
        "consent",
        "register",
        "unregister",
        "cancel",
        "revoke",
        "주문",
        "매수",
        "매도",
        "이체",
        "출금",
    }
)
LOGIN_WINDOW_SECONDS = 20 * 60


class TossError(Exception):
    """사용자에게 그대로 보여 줄 수 있는 오류."""


class LoginRequired(TossError):
    pass


class Busy(TossError):
    pass


@dataclass
class LoginState:
    """로그인 창(일반 Chrome) 상태. 화면이 그대로 보여 준다."""

    state: str = "idle"  # idle | waiting | done | timeout | failed
    message: str | None = None
    started_at: float | None = None
    finished_at: float | None = None


@dataclass
class PageObservation:
    """연결 확인 1회가 브라우저에서 읽은 것. 값(금액·수량)은 담지 않는다."""

    url: str = TOSS_HOME
    title: str | None = None
    logged_in: bool | None = None
    has_login_cta: bool | None = None
    state: str = "unknown"
    state_message: str = ""
    api_calls: list[dict[str, Any]] = field(default_factory=list)
    load_ms: int | None = None


@dataclass
class Status:
    state: str  # ready | login_required | login_window | unknown | disabled | error
    message: str
    logged_in: bool | None = None
    checked_at: float = field(default_factory=time.time)
    observation: PageObservation | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "state": self.state,
            "message": self.message,
            "logged_in": self.logged_in,
            "checked_at": datetime.fromtimestamp(self.checked_at, UTC).isoformat(),
            "observation": {
                "url": self.observation.url,
                "title": self.observation.title,
                "logged_in": self.observation.logged_in,
                "has_login_cta": self.observation.has_login_cta,
                "api_call_count": len(self.observation.api_calls),
                "api_calls": self.observation.api_calls,
                "load_ms": self.observation.load_ms,
            }
            if self.observation
            else None,
        }


# CDP Runtime.evaluate 고정 스니펫. 읽기만 하며 클릭·입력·이동을 하지 않는다.
LOGIN_SNIFF_JS = (
    "(() => {"
    " const body = document.body ? document.body.innerText.slice(0, 4000) : '';"
    " const links = document.body ? document.body.innerText.slice(0, 12000) : '';"
    " const hasLogin = /로그인/.test(links);"
    " const hasAccount = /(내 주식|보유|계좌|자산|관심)/.test(links);"
    " return JSON.stringify({title: document.title || null,"
    "  url: location.href,"
    "  hasLogin: hasLogin, hasAccount: hasAccount,"
    "  sample: body.slice(0, 300)});"
    "})()"
)


def assert_read_only(method: str, url: str) -> None:
    """외부 호출 직전 검사. GET이 아니거나 허용 밖이면 호출하지 않는다."""
    try:
        host = urlparse(url).hostname or ""
    except ValueError as e:
        raise TossError(f"읽을 수 없는 주소입니다: {e}") from e
    if method.upper() != "GET":
        raise TossError(f"읽기 전용이라 {method.upper()} 요청을 보내지 않습니다.")
    if host not in ALLOWED_HOSTS:
        raise TossError(f"허용한 토스증권 주소가 아니라 열지 않습니다: {host}")
    lowered = url.lower()
    for hint in BLOCKED_PATH_HINTS:
        if hint in lowered:
            raise TossError(f"계정 상태를 바꿀 수 있는 경로는 열지 않습니다: {hint}")


def _profile_dir() -> Path:
    return get_settings().tossinvest_profile_dir


def find_chrome() -> Path | None:
    """설정 우선, 없으면 설치된 Google Chrome을 찾는다."""
    s = get_settings()
    if s.tossinvest_chrome_bin:
        p = Path(s.tossinvest_chrome_bin).expanduser()
        return p if p.exists() else None
    mac = Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
    if mac.exists():
        return mac
    for name in (
        "google-chrome",
        "google-chrome-stable",
        "chrome",
        "chromium",
        "chromium-browser",
    ):
        found = shutil.which(name)
        if found:
            return Path(found)
    for p in (Path("/opt/homebrew/bin/google-chrome"), Path("/usr/bin/google-chrome")):
        if p.exists():
            return p
    return None


def profile_has_files() -> bool:
    """전용 프로필에 무엇이든 있으면(로그인 시도 흔적) True. 내용물은 읽지 않는다."""
    d = _profile_dir()
    try:
        return d.exists() and any(d.iterdir())
    except OSError:
        return False


def profile_cookie_hosts() -> list[str]:
    """전용 프로필 Cookies에서 토스증권 관련 호스트 이름만 본다. 값은 읽지 않는다."""
    db = _profile_dir() / "Cookies"
    if not db.exists():
        return []
    tmp = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False) as f:
            tmp = f.name
        shutil.copyfile(db, tmp)
        con = sqlite3.connect(f"file:{tmp}?mode=ro", uri=True)
        try:
            rows = con.execute("SELECT DISTINCT host_key FROM cookies").fetchall()
        finally:
            con.close()
        return sorted({r[0] for r in rows if "tossinvest" in r[0]})
    except (OSError, sqlite3.Error):
        return []
    finally:
        if tmp:
            with contextlib.suppress(OSError):
                Path(tmp).unlink()


def open_login_window() -> LoginState:
    """자동화 플래그 없는 일반 Chrome으로 프로필을 열어 사람이 로그인하게 한다."""
    st = LoginState(state="waiting", started_at=time.time())
    profile = _profile_dir()
    profile.mkdir(parents=True, exist_ok=True)
    try:
        # 고정 인자만: 자동화 플래그 없는 일반 Chrome 실행
        subprocess.Popen(
            [
                "open",
                "-na",
                "Google Chrome",
                "--args",
                f"--user-data-dir={profile}",
                TOSS_HOME,
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except OSError as e:
        st.state = "failed"
        st.message = f"일반 Chrome을 열지 못했습니다: {e}"
        st.finished_at = time.time()
        return st
    st.message = (
        "일반 Chrome 창으로 토스증권이 열렸습니다. 로그인하고 "
        "'로그인 상태 유지'를 확인한 뒤 닫아 주세요."
    )
    return st


def login_chrome_pids() -> list[int]:
    """로그인용으로 띄운 일반 Chrome의 프로세스(자동화 통로 없는 것)."""
    needle = f"--user-data-dir={_profile_dir()}"
    found: list[int] = []
    for p in psutil.process_iter(["pid", "cmdline"]):
        try:
            cmd = p.info.get("cmdline") or []
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
        if (
            any("Chrome" in (c or "") for c in cmd[:1])
            and any(needle in (c or "") for c in cmd)
            and not any("remote-debugging" in (c or "") for c in cmd)
        ):
            found.append(p.info["pid"])
    return found


class _Cdp:
    """읽기 전용 탐색에 필요한 최소 CDP 클라이언트. Page·Runtime·Network만 쓴다."""

    def __init__(self, ws_url: str) -> None:
        self._ws_url = ws_url
        self._ws: Any = None
        self._next_id = 0

    async def open(self) -> None:
        import websockets

        self._ws = await websockets.connect(self._ws_url, max_size=8 * 1024 * 1024)

    async def close(self) -> None:
        if self._ws is not None:
            with contextlib.suppress(Exception):
                await self._ws.close()
            self._ws = None

    async def _next(self, timeout: float) -> dict[str, Any]:
        assert self._ws is not None
        raw = await asyncio.wait_for(self._ws.recv(), timeout)
        try:
            msg = json.loads(raw)
        except ValueError:
            return {}
        return msg if isinstance(msg, dict) else {}

    async def send(
        self, method: str, params: dict[str, Any] | None = None, timeout: float = 30
    ) -> dict[str, Any]:
        """요청을 보내고 같은 id의 응답이 올 때까지 읽는다. 중간 이벤트는 버린다."""
        assert self._ws is not None
        self._next_id += 1
        call_id = self._next_id
        await self._ws.send(
            json.dumps({"id": call_id, "method": method, "params": params or {}})
        )
        deadline = time.monotonic() + timeout
        while True:
            msg = await self._next(max(0.1, deadline - time.monotonic()))
            if msg.get("id") == call_id:
                if "error" in msg:
                    raise TossError(f"브라우저 명령이 실패했습니다: {method}")
                return msg.get("result", {})
            if time.monotonic() > deadline:
                raise TossError(f"브라우저 응답이 늦습니다: {method}")

    async def navigate_and_sniff(
        self, url: str, timeout: float
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        """읽기 전용 이동. 로드 이벤트까지 관측된 외부 요청(경로만)을 함께 모은다."""
        assert_read_only("GET", url)
        await self.send("Page.enable")
        await self.send("Runtime.enable")
        await self.send("Network.enable")
        await self.send("Page.navigate", {"url": url})
        seen: list[dict[str, Any]] = []
        seen_keys: set[str] = set()
        deadline = time.monotonic() + timeout
        loaded = False
        while time.monotonic() < deadline and not loaded:
            try:
                msg = await self._next(1.0)
            except TimeoutError:
                continue
            if msg.get("method") == "Page.loadEventFired":
                loaded = True
            if msg.get("method") == "Network.requestWillBeSent":
                req = msg.get("params", {}).get("request", {})
                rurl = req.get("url", "")
                rmethod = req.get("method", "GET")
                # 페이지가 스스로 보내는 요청은 관찰만 한다. 재생은 GET·허용 주소만, 아래에서 따로 검사한다.
                try:
                    parts = urlparse(rurl)
                except ValueError:
                    continue
                if parts.hostname in ALLOWED_HOSTS and rmethod.upper() == "GET":
                    key = f"{parts.hostname}{parts.path}"
                    if key not in seen_keys:
                        seen_keys.add(key)
                        seen.append({"host": parts.hostname, "path": parts.path or "/"})
        result = await self.send(
            "Runtime.evaluate",
            {"expression": LOGIN_SNIFF_JS, "returnByValue": True},
            timeout=15,
        )
        value = (result.get("result") or {}).get("value") or "{}"
        try:
            sniff = json.loads(value)
        except ValueError:
            sniff = {}
        return sniff if isinstance(sniff, dict) else {}, seen


_browser_lock = asyncio.Lock()


async def _launch_headless(chrome: Path, profile: Path) -> tuple[subprocess.Popen, str]:
    """전용 프로필로 headless Chrome을 띄우고 CDP 주소를 돌려준다."""
    profile.mkdir(parents=True, exist_ok=True)
    # chrome 바이너리 고정, 인자는 아래 고정 목록만 쓴다.
    proc = await asyncio.to_thread(
        subprocess.Popen,
        [
            str(chrome),
            "--headless=new",
            "--disable-gpu",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-extensions",
            "--mute-audio",
            "--hide-scrollbars",
            "--window-size=1280,900",
            f"--user-data-dir={profile}",
            "--remote-debugging-port=0",
            "--remote-allow-origins=*",
            "about:blank",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )
    assert proc.stderr is not None
    ws_url = ""

    def _read() -> str:
        for _line in proc.stderr:
            line = _line.strip()
            if line.startswith("DevTools listening on "):
                return line.removeprefix("DevTools listening on ")
        return ""

    ws_url = await asyncio.to_thread(_read)
    if not ws_url:
        with contextlib.suppress(Exception):
            proc.terminate()
        raise TossError(
            "headless Chrome이 뜨지 않았습니다. 다른 Chrome이 프로필을 쓰고 있는지 확인해 주세요."
        )
    return proc, ws_url


async def _stop(proc: subprocess.Popen) -> None:
    with contextlib.suppress(Exception):
        proc.terminate()
    try:
        await asyncio.to_thread(proc.wait, 10)
    except Exception:  # noqa: BLE001 -- 종료 대기가 길어지면 강제 종료한다
        with contextlib.suppress(Exception):
            proc.kill()


def _decide_state(sniff: dict[str, Any]) -> tuple[str, bool | None, str]:
    has_login = bool(sniff.get("hasLogin"))
    has_account = bool(sniff.get("hasAccount"))
    if has_account and not has_login:
        return "ready", True, "저장된 로그인으로 토스증권을 열었습니다."
    if has_login and not has_account:
        return (
            "login_required",
            False,
            "로그인이 필요합니다. 일반 Chrome 창으로 로그인해 주세요.",
        )
    return (
        "unknown",
        None,
        "로그인 상태를 단정하지 못했습니다. 실제 세션으로 다시 확인이 필요합니다.",
    )


async def observe_home() -> PageObservation:
    """홈을 읽기 전용으로 열어 로그인 상태와 관측된 API 호출 목록을 본다."""
    s = get_settings()
    if not s.tossinvest_enabled:
        raise TossError("토스증권 연결이 꺼져 있습니다.")
    chrome = await asyncio.to_thread(find_chrome)
    if chrome is None:
        raise TossError("설치된 Google Chrome을 찾지 못했습니다.")
    if await asyncio.to_thread(login_chrome_pids):
        raise Busy("로그인 창이 열려 있습니다. 먼저 로그인 창을 닫아 주세요.")
    started = time.monotonic()
    obs = PageObservation()
    async with _browser_lock:  # 한 프로필은 한 Chrome만. 동시 확인은 차례로.
        proc: subprocess.Popen | None = None
        try:
            proc, ws_url = await _launch_headless(chrome, _profile_dir())
            cdp = _Cdp(ws_url)
            try:
                await cdp.open()
                try:
                    sniff, seen = await cdp.navigate_and_sniff(
                        TOSS_HOME, s.tossinvest_page_timeout_seconds
                    )
                finally:
                    await cdp.close()
            except (OSError, TossError):
                raise
            except Exception as e:
                raise TossError(
                    f"브라우저와 대화하지 못했습니다: {type(e).__name__}"
                ) from e
            obs.url = sniff.get("url") or TOSS_HOME
            obs.title = sniff.get("title")
            obs.has_login_cta = sniff.get("hasLogin")
            obs.api_calls = seen[:50]
            state, logged_in, message = _decide_state(sniff)
            obs.state = state
            obs.logged_in = logged_in
            obs.state_message = message
        finally:
            if proc is not None:
                await _stop(proc)
    obs.load_ms = int((time.monotonic() - started) * 1000)
    return obs


async def check_status() -> Status:
    """연결 확인 1회. 브라우저를 띄워 홈을 읽고 로그인 세션 유효 여부만 판단한다."""
    s = get_settings()
    if not s.tossinvest_enabled:
        return Status(state="disabled", message="토스증권 연결이 꺼져 있습니다.")
    try:
        obs = await observe_home()
    except Busy as e:
        return Status(state="login_window", message=str(e))
    except TossError as e:
        return Status(state="error", message=str(e))
    # observe_home이 sniff 원값으로 이미 판단한 state·message를 그대로 쓴다.
    return Status(
        state=obs.state,
        message=obs.state_message,
        logged_in=obs.logged_in,
        observation=obs,
    )


async def sync_snapshot() -> tuple[Status, dict[str, Any]]:
    """동기화 1회. check + 관측된 읽기 API 목록 요약. 금액·수량은 저장하지 않는다."""
    status = await check_status()
    summary = {
        "state": status.state,
        "logged_in": status.logged_in,
        "title": status.observation.title if status.observation else None,
        "api_paths": (
            [f"{c['host']}{c['path']}" for c in status.observation.api_calls]
            if status.observation
            else []
        ),
        "load_ms": status.observation.load_ms if status.observation else None,
    }
    return status, summary


def login_help() -> dict[str, Any]:
    """최초 1회 로그인을 사람이 하는 방법. 비밀값은 포함하지 않는다."""
    profile = _profile_dir()
    return {
        "profile_dir": str(profile),
        "profile_exists": profile_has_files(),
        "cookie_hosts": profile_cookie_hosts(),
        "steps": [
            "Mac Studio에서 일반 Chrome 창으로 토스증권을 엽니다.",
            "아래 명령을 터미널에 그대로 붙여 넣어 실행하세요. 평소 프로필이 아닌 전용 프로필로 열립니다.",
            "토스증권에 로그인하고 '로그인 상태 유지'를 꼭 확인한 뒤 창을 닫습니다.",
            "따까리 주식 투자 → 토스증권 연결에서 '연결 확인'을 눌러 상태를 확인합니다.",
        ],
        "open_command": [
            "open",
            "-na",
            "Google Chrome",
            "--args",
            f"--user-data-dir={profile}",
            TOSS_HOME,
        ],
        "note": (
            "평소 Chrome 프로필은 자동화에 쓸 수 없어(Chrome 136+ 차단·macOS TCC) "
            "전용 프로필을 씁니다. 비밀번호를 따까리에 저장하지 않습니다."
        ),
    }
