"""토스증권(tossinvest.com) 읽기 전용 브라우저 어댑터(ADR 0044).

공개 API가 없고 계정 영역은 로그인 세션이 있어야 보인다. 클로바노트 선례(ADR 0021)의
교훈을 재사용한다: 평소 Chrome 프로필은 자동화에 쓸 수 없고(Chrome 136+ 기본 프로필
자동화 차단, macOS TCC 읽기 차단), 로그인은 자동화 플래그 없는 일반 Chrome 창으로
사람이 한 번 하며, 브라우저 조종은 Playwright 같은 별도 의존성 없이 설치된 Chrome +
CDP(websockets·psutil·httpx는 이미 앱 의존성)로 한다.

읽기 전용 강제:
- 탐색 전 Fetch 가로채기를 걸어 허용 호스트의 HTTPS GET만 통과시킨다.
- 공개 HTML에서 확인한 정적 호스트는 스크립트·스타일·폰트·이미지 GET만 허용한다.
- 주문·매수·매도·이체·출금·입금·설정·인증 변경 냄새가 나는 경로는 거부한다.
- CDP Runtime.evaluate는 아래 고정 스니펫만 실행한다(문자열 합성·클릭·입력 없음).
- 자동 새로고침·스케줄이 없다. 수동 트리거 + 서버 최소 간격이 사람 수준 빈도를 강제한다.

실제 Chrome의 시작 시점 백그라운드 통신까지 통제되는지는 호스트 검증이 필요하다.

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
from urllib.parse import unquote, urlparse

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
# 공개 홈 HTML의 script/link에서 직접 확인한 호스트(2026-10-18).
STATIC_HOSTS = frozenset({"static.toss.im", "static.tossinvest.com"})
STATIC_TYPES = frozenset({"Script", "Stylesheet", "Font", "Image"})
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


def assert_read_only(
    method: str, url: str, *, resource_type: str | None = None
) -> None:
    """외부 호출 직전 검사. GET이 아니거나 허용 밖이면 호출하지 않는다."""
    try:
        parts = urlparse(url)
        host = parts.hostname or ""
        if (
            parts.scheme != "https"
            or parts.port not in (None, 443)
            or parts.username is not None
            or parts.password is not None
        ):
            raise TossError("HTTPS 기본 포트의 토스증권 주소만 읽습니다.")
    except ValueError as e:
        raise TossError(f"읽을 수 없는 주소입니다: {e}") from e
    if method.upper() != "GET":
        raise TossError(f"읽기 전용이라 {method.upper()} 요청을 보내지 않습니다.")
    is_static = host in STATIC_HOSTS and resource_type in STATIC_TYPES
    if host not in ALLOWED_HOSTS and not is_static:
        raise TossError(f"허용한 토스증권 주소가 아니라 열지 않습니다: {host}")
    if is_static:
        return
    lowered = url.lower()
    for _ in range(5):
        decoded = unquote(lowered)
        if decoded == lowered:
            break
        lowered = decoded
    if "%" in lowered:
        raise TossError("중첩 인코딩된 주소는 열지 않습니다.")
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


def headless_chrome_pids() -> list[int]:
    """같은 프로필로 headless 확인을 실행 중인 Chrome(자동화 통로 있는 것)."""
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
            and any("remote-debugging" in (c or "") for c in cmd)
        ):
            found.append(p.info["pid"])
    return found


def headless_check_running() -> bool:
    """같은 프로필로 headless 확인이 실행 중이면 True. 같은 프로세스의 lock과 타 프로세스를 함께 본다."""
    return _browser_lock.locked() or bool(headless_chrome_pids())


class _Cdp:
    """Browser websocket에서 page session을 분리하고 Fetch를 탐색 전에 건다."""

    def __init__(self, ws_url: str) -> None:
        self._ws_url = ws_url
        self._ws: Any = None
        self._next_id = 0
        self._pending: dict[int, asyncio.Future] = {}
        self._reader: asyncio.Task | None = None
        self._handlers: set[asyncio.Task] = set()
        self._loaded = asyncio.Event()
        self._error: TossError | None = None
        self._session: str | None = None
        self._seen: list[dict[str, Any]] = []
        self._seen_keys: set[str] = set()

    async def open(self) -> None:
        import websockets

        self._ws = await websockets.connect(self._ws_url, max_size=8 * 1024 * 1024)
        self._reader = asyncio.create_task(self._read_loop())

    async def close(self) -> None:
        for task in list(self._handlers):
            task.cancel()
        if self._handlers:
            await asyncio.gather(*self._handlers, return_exceptions=True)
        if self._reader is not None:
            self._reader.cancel()
            await asyncio.gather(self._reader, return_exceptions=True)
        if self._ws is not None:
            with contextlib.suppress(Exception):
                await self._ws.close()
            self._ws = None

    def _spawn(self, coroutine) -> None:
        task = asyncio.create_task(coroutine)
        self._handlers.add(task)
        task.add_done_callback(self._handlers.discard)

    def _fail(self) -> None:
        # 브라우저 응답/URL/쿠키 원문을 오류에 싣지 않는다.
        self._error = TossError(
            "브라우저 읽기 전용 통제를 유지하지 못해 확인을 중단했습니다."
        )
        self._loaded.set()
        for future in self._pending.values():
            if not future.done():
                future.set_exception(self._error)

    async def _read_loop(self) -> None:
        try:
            while True:
                msg = json.loads(await self._ws.recv())
                if not isinstance(msg, dict):
                    continue
                if "id" in msg:
                    future = self._pending.get(msg["id"])
                    if future is not None and not future.done():
                        future.set_result(msg)
                elif msg.get("method") == "Fetch.requestPaused":
                    self._spawn(self._intercept(msg))
                elif msg.get("method") == "Target.attachedToTarget":
                    params = msg.get("params", {})
                    if params.get("waitingForDebugger"):
                        self._spawn(self._configure_child(params["sessionId"]))
                elif (
                    msg.get("method") == "Page.loadEventFired"
                    and msg.get("sessionId") == self._session
                ):
                    self._loaded.set()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 -- 통제 실패는 원문을 노출하지 않고 중단한다.
            self._fail()

    async def send(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        timeout: float = 30,
        *,
        session: str | None = None,
    ) -> dict[str, Any]:
        assert self._ws is not None
        if self._error:
            raise self._error
        self._next_id += 1
        call_id = self._next_id
        future = asyncio.get_running_loop().create_future()
        self._pending[call_id] = future
        msg: dict[str, Any] = {"id": call_id, "method": method, "params": params or {}}
        if session:
            msg["sessionId"] = session
        try:
            await self._ws.send(json.dumps(msg))
            reply = await asyncio.wait_for(future, timeout)
            if "error" in reply:
                raise TossError(f"브라우저 명령이 실패했습니다: {method}")
            return reply.get("result", {})
        finally:
            self._pending.pop(call_id, None)

    async def _intercept(self, msg: dict[str, Any]) -> None:
        try:
            params = msg["params"]
            req = params["request"]
            args = {"requestId": params["requestId"]}
            try:
                assert_read_only(
                    req.get("method", ""),
                    req.get("url", ""),
                    resource_type=params.get("resourceType"),
                )
            except TossError:
                args["errorReason"] = "BlockedByClient"
                method = "Fetch.failRequest"
            else:
                method = "Fetch.continueRequest"
                parts = urlparse(req["url"])
                key = f"{parts.hostname}{parts.path}"
                if (
                    parts.hostname in ALLOWED_HOSTS
                    and key not in self._seen_keys
                    and len(self._seen) < 50
                ):
                    self._seen_keys.add(key)
                    self._seen.append(
                        {"host": parts.hostname, "path": parts.path or "/"}
                    )
            await self.send(method, args, session=msg.get("sessionId"))
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 -- 통제 실패는 원문을 노출하지 않고 중단한다.
            self._fail()

    async def _configure(self, session: str) -> None:
        await self.send(
            "Fetch.enable",
            {"patterns": [{"urlPattern": "*", "requestStage": "Request"}]},
            session=session,
        )
        await self.send("Network.enable", session=session)
        await self.send(
            "Network.setBypassServiceWorker", {"bypass": True}, session=session
        )
        await self.send(
            "Network.setBlockedURLs", {"urls": ["ws://*", "wss://*"]}, session=session
        )
        await self.send(
            "Target.setAutoAttach",
            {"autoAttach": True, "waitForDebuggerOnStart": True, "flatten": True},
            session=session,
        )

    async def _configure_child(self, session: str) -> None:
        try:
            await self._configure(session)
            await self.send("Runtime.runIfWaitingForDebugger", session=session)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 -- 통제 실패는 원문을 노출하지 않고 중단한다.
            self._fail()  # 통제 설정에 실패한 자식은 재개하지 않는다.

    async def navigate_and_sniff(
        self, url: str, timeout: float
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        assert_read_only("GET", url)
        targets = await self.send("Target.getTargets")
        page = next(
            (
                t
                for t in targets.get("targetInfos", [])
                if t.get("type") == "page" and t.get("url") == "about:blank"
            ),
            None,
        )
        if page is None:
            raise TossError("안전한 빈 브라우저 탭을 찾지 못했습니다.")
        attached = await self.send(
            "Target.attachToTarget", {"targetId": page["targetId"], "flatten": True}
        )
        self._session = attached["sessionId"]
        await self._configure(self._session)
        await self.send("Page.enable", session=self._session)
        await self.send("Runtime.enable", session=self._session)
        self._loaded.clear()
        await self.send("Page.navigate", {"url": url}, session=self._session)
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self._loaded.wait(), timeout)
        if self._error:
            raise self._error
        result = await self.send(
            "Runtime.evaluate",
            {"expression": LOGIN_SNIFF_JS, "returnByValue": True},
            timeout=15,
            session=self._session,
        )
        if self._handlers:
            await asyncio.gather(*list(self._handlers))
        if self._error:
            raise self._error
        value = (result.get("result") or {}).get("value") or "{}"
        try:
            sniff = json.loads(value)
        except ValueError:
            sniff = {}
        return sniff if isinstance(sniff, dict) else {}, self._seen


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
            "--disable-background-networking",
            "--disable-sync",
            "--disable-component-update",
            "--mute-audio",
            "--hide-scrollbars",
            "--window-size=1280,900",
            f"--user-data-dir={profile}",
            "--remote-debugging-port=0",
            "--remote-debugging-address=127.0.0.1",
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

    try:
        ws_url = await asyncio.wait_for(asyncio.to_thread(_read), 15)
        parts = urlparse(ws_url)
        if parts.scheme != "ws" or parts.hostname not in {"127.0.0.1", "::1"}:
            raise TossError("Chrome의 로컬 제어 주소를 확인하지 못했습니다.")
    except BaseException:
        await _stop(proc)
        raise
    return proc, ws_url


async def _stop(proc: subprocess.Popen) -> None:
    with contextlib.suppress(Exception):
        proc.terminate()
    try:
        await asyncio.to_thread(proc.wait, 10)
    except Exception:  # noqa: BLE001 -- 종료 대기가 길어지면 강제 종료한다
        with contextlib.suppress(Exception):
            proc.kill()
            await asyncio.to_thread(proc.wait, 10)
    finally:
        if proc.stderr is not None:
            proc.stderr.close()


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
