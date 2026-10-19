"""NotebookLM 1회 로그인 창(ADR 0045). 설치된 Chrome + 전용 프로필 + 루프백 CDP.

사람이 보이는 Chrome 창에서 Google 로그인을 직접 한 번 한다. 따까리는 창 안에서 아무것도 클릭·입력하지 않는다.
CDP 는 브라우저 수준 websocket 으로 두 가지만 읽는다: 열린 탭의 URL 목록(Target.getTargets)과 쿠키(Storage.getCookies).
페이지에 붙지 않고 스크립트를 실행하지 않는다. 로그인이 끝나면 쿠키를 세션 파일에 저장하고 창을 닫는다.

보호 신호(CAPTCHA·보안 확인 등)가 뜨면 상태를 protection 으로 바꾸고 쿠키 추출을 하지 않는다.
사람이 창에서 직접 마치면 이어지고, 마치지 못하면 타임아웃으로 끝난다. 자동으로 넘기지 않는다.
CDP 포트는 127.0.0.1 에만 바인딩하고, Chrome 이 알려 준 주소가 루프백이 아니면 중단한다.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import psutil

from ttakkari.config import get_settings
from ttakkari.notebooklm import errors, metrics, redact, session

log = logging.getLogger(__name__)

LOGIN_URL = "https://notebooklm.google.com/"
APP_HOSTS = ("notebooklm.google.com", "notebook.google.com")
POLL_SECONDS = 2.0
STABLE_POLLS = 2  # 로그인 조건이 연속으로 이 횟수만큼 맞아야 저장한다(쿠키가 자리 잡을 시간)
_PROTECTION_HINTS = (
    "/signin/v2/challenge", "/signin/challenge", "/signin/rejected", "/sorry/", "captcha", "recaptcha",
    "speedbump", "/interstitial", "accountrecovery", "/signin/v2/disabled",
)
ACTIVE = frozenset({"starting", "waiting", "protection", "saving"})


@dataclass
class LoginState:
    """로그인 창 하나의 상태. 화면이 그대로 보여 준다."""

    account_id: str
    state: str = "idle"  # idle|starting|waiting|protection|saving|done|timeout|cancelled|failed
    message: str = ""
    started_at: float | None = None
    finished_at: float | None = None
    cookie_names: list[str] = field(default_factory=list)

    @property
    def active(self) -> bool:
        return self.state in ACTIVE

    def to_dict(self) -> dict[str, Any]:
        return {
            "state": self.state,
            "message": self.message,
            "active": self.active,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
        }


def find_chrome() -> Path | None:
    s = get_settings()
    if s.notebooklm_chrome_bin:
        p = Path(s.notebooklm_chrome_bin).expanduser()
        return p if p.exists() else None
    mac = Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
    if mac.exists():
        return mac
    for name in ("google-chrome", "google-chrome-stable", "chrome", "chromium", "chromium-browser"):
        found = shutil.which(name)
        if found:
            return Path(found)
    return None


def profile_chrome_pids() -> list[int]:
    """전용 프로필을 쓰는 Chrome 프로세스(이전 실행이 남긴 창 포함)."""
    needle = f"--user-data-dir={get_settings().notebooklm_profile_dir}"
    found: list[int] = []
    for p in psutil.process_iter(["pid", "cmdline"]):
        try:
            cmd = p.info.get("cmdline") or []
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
        if cmd and "chrom" in (cmd[0] or "").lower() and any(needle in (c or "") for c in cmd):
            found.append(p.info["pid"])
    return found


def is_loopback_ws(ws_url: str) -> bool:
    parts = urlparse(ws_url)
    return parts.scheme == "ws" and parts.hostname in {"127.0.0.1", "::1", "localhost"}


def protection_signal(urls: list[str]) -> bool:
    for u in urls:
        low = u.lower()
        host = urlparse(low).hostname or ""
        if host.endswith(("google.com", "gstatic.com")) and any(h in low for h in _PROTECTION_HINTS):
            return True
    return False


def login_complete(urls: list[str], cookies: list[dict[str, Any]]) -> bool:
    """NotebookLM 앱 화면에 도착했고(Google 로그인·확인 화면이 아님) 필수 세션 쿠키가 있으면 로그인 완료."""
    on_app = any((urlparse(u).hostname or "") in APP_HOSTS for u in urls)
    on_accounts = any((urlparse(u).hostname or "") == "accounts.google.com" for u in urls)
    names = {c.get("name") for c in cookies if session.is_google(c.get("domain", ""))}
    return on_app and not on_accounts and all(n in names for n in session.REQUIRED_COOKIES)


class _Browser:
    """브라우저 수준 CDP websocket. 페이지에 붙지 않고 URL 목록과 쿠키만 읽는다."""

    def __init__(self, ws_url: str) -> None:
        self._ws_url = ws_url
        self._ws: Any = None
        self._id = 0

    async def open(self) -> None:
        import websockets

        self._ws = await websockets.connect(self._ws_url, max_size=16 * 1024 * 1024)

    async def close(self) -> None:
        if self._ws is not None:
            with contextlib.suppress(Exception):
                await self._ws.close()
            self._ws = None

    async def send(self, method: str, params: dict[str, Any] | None = None, timeout: float = 10) -> dict[str, Any]:
        self._id += 1
        call_id = self._id
        await self._ws.send(json.dumps({"id": call_id, "method": method, "params": params or {}}))

        async def wait() -> dict[str, Any]:
            while True:
                msg = json.loads(await self._ws.recv())
                if isinstance(msg, dict) and msg.get("id") == call_id:
                    if "error" in msg:
                        raise errors.Upstream(f"브라우저 명령이 실패했습니다: {method}")
                    return msg.get("result", {})

        return await asyncio.wait_for(wait(), timeout)

    async def page_urls(self) -> list[str]:
        res = await self.send("Target.getTargets")
        return [t.get("url", "") for t in res.get("targetInfos", []) if t.get("type") == "page"]

    async def cookies(self) -> list[dict[str, Any]]:
        return (await self.send("Storage.getCookies")).get("cookies", [])


async def _launch(chrome: Path, profile: Path) -> tuple[subprocess.Popen, str]:
    """보이는 Chrome 창을 전용 프로필로 띄우고 루프백 CDP 주소를 읽는다."""
    profile.mkdir(parents=True, exist_ok=True)
    profile.chmod(0o700)
    proc = await asyncio.to_thread(
        subprocess.Popen,
        [
            str(chrome),
            f"--user-data-dir={profile}",
            "--remote-debugging-port=0",
            "--remote-debugging-address=127.0.0.1",
            "--no-first-run",
            "--no-default-browser-check",
            LOGIN_URL,
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )
    metrics.incr("browser_launches")
    assert proc.stderr is not None

    def read() -> str:
        for line in proc.stderr:
            line = line.strip()
            if line.startswith("DevTools listening on "):
                return line.removeprefix("DevTools listening on ")
        return ""

    try:
        ws_url = await asyncio.wait_for(asyncio.to_thread(read), 20)
        if not is_loopback_ws(ws_url):
            raise errors.Upstream("Chrome의 로컬 제어 주소가 루프백이 아니라 중단했습니다.")
    except BaseException:
        await _stop(proc)
        raise
    return proc, ws_url


async def _stop(proc: subprocess.Popen) -> None:
    with contextlib.suppress(Exception):
        proc.terminate()
    try:
        await asyncio.to_thread(proc.wait, 10)
    except Exception:  # noqa: BLE001 -- 종료 대기가 길면 강제 종료
        with contextlib.suppress(Exception):
            proc.kill()
            await asyncio.to_thread(proc.wait, 10)
    finally:
        if proc.stderr is not None:
            with contextlib.suppress(Exception):
                proc.stderr.close()


async def _close_gracefully(browser: _Browser | None, proc: subprocess.Popen | None) -> None:
    if browser is not None:
        with contextlib.suppress(Exception):
            await browser.send("Browser.close", timeout=5)
        await browser.close()
    if proc is not None:
        with contextlib.suppress(Exception):
            await asyncio.to_thread(proc.wait, 8)
        if proc.poll() is None:
            await _stop(proc)
        elif proc.stderr is not None:
            with contextlib.suppress(Exception):
                proc.stderr.close()


class LoginManager:
    """계정별 로그인 창 수명주기. 같은 계정에 창은 하나만 연다(중복 실행 잠금)."""

    def __init__(self) -> None:
        self._states: dict[str, LoginState] = {}
        self._tasks: dict[str, asyncio.Task] = {}
        self._cancel: dict[str, asyncio.Event] = {}

    def state(self, account_id: str) -> LoginState:
        return self._states.get(account_id) or LoginState(account_id=account_id)

    async def start(self, account_id: str, on_done=None) -> LoginState:
        session.check_account(account_id)
        s = get_settings()
        if not s.notebooklm_enabled:
            raise errors.Disabled("NotebookLM 연결이 꺼져 있습니다.")
        if self.state(account_id).active:
            raise errors.Busy("로그인 창이 이미 열려 있습니다. 그 창에서 로그인을 마치거나 취소해 주세요.")
        if await asyncio.to_thread(profile_chrome_pids):
            raise errors.Busy("이전 NotebookLM 로그인 창이 남아 있습니다. 창을 닫거나 로그인 취소를 눌러 주세요.")
        chrome = await asyncio.to_thread(find_chrome)
        if chrome is None:
            raise errors.Upstream("설치된 Google Chrome을 찾지 못했습니다.")
        st = LoginState(account_id=account_id, state="starting", message="로그인 창을 여는 중입니다.", started_at=time.time())
        self._states[account_id] = st
        self._cancel[account_id] = asyncio.Event()
        task = asyncio.create_task(self._run(st, chrome, on_done))
        self._tasks[account_id] = task
        return st

    async def cancel(self, account_id: str) -> LoginState:
        st = self.state(account_id)
        ev = self._cancel.get(account_id)
        if st.active and ev is not None:
            ev.set()
            task = self._tasks.get(account_id)
            if task is not None:
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(asyncio.shield(task), 20)
            return self.state(account_id)
        # 이전 프로세스가 남긴 창: 전용 프로필을 쓰는 Chrome 만 종료한다.
        pids = await asyncio.to_thread(profile_chrome_pids)
        for pid in pids:
            with contextlib.suppress(psutil.Error):
                psutil.Process(pid).terminate()
        if pids:
            st = LoginState(account_id=account_id, state="cancelled", message="남아 있던 로그인 창을 종료했습니다.",
                            finished_at=time.time())
            self._states[account_id] = st
        return self.state(account_id)

    async def shutdown(self) -> None:
        for ev in self._cancel.values():
            ev.set()
        tasks = [t for t in self._tasks.values() if not t.done()]
        if tasks:
            await asyncio.wait(tasks, timeout=20)

    async def _run(self, st: LoginState, chrome: Path, on_done) -> None:
        s = get_settings()
        cancel = self._cancel[st.account_id]
        deadline = time.monotonic() + s.notebooklm_login_timeout_seconds
        proc: subprocess.Popen | None = None
        browser: _Browser | None = None
        stable = 0
        try:
            proc, ws_url = await _launch(chrome, s.notebooklm_profile_dir)
            browser = _Browser(ws_url)
            await browser.open()
            st.state, st.message = "waiting", "열린 Chrome 창에서 Google 로그인을 한 번 해 주세요. 마치면 창이 자동으로 닫힙니다."
            while True:
                if cancel.is_set():
                    st.state, st.message = "cancelled", "로그인을 취소하고 창을 닫았습니다."
                    return
                if time.monotonic() > deadline:
                    st.state, st.message = "timeout", "시간 안에 로그인이 끝나지 않아 창을 닫았습니다. 다시 시도해 주세요."
                    return
                if proc.poll() is not None:
                    st.state, st.message = "cancelled", "로그인 창이 닫혀 로그인을 마치지 못했습니다."
                    return
                urls = await browser.page_urls()
                if protection_signal(urls):
                    stable = 0
                    if st.state != "protection":
                        metrics.event("login_protection_signal")
                    st.state = "protection"
                    st.message = ("Google 보안 확인 화면이 떴습니다. 따까리는 이를 자동으로 넘기지 않습니다. "
                                  "창에서 직접 마쳐 주세요.")
                else:
                    if st.state == "protection":
                        st.state, st.message = "waiting", "열린 Chrome 창에서 Google 로그인을 마쳐 주세요."
                    cookies = await browser.cookies()
                    stable = stable + 1 if login_complete(urls, cookies) else 0
                    if stable >= STABLE_POLLS:
                        st.state, st.message = "saving", "로그인을 확인했습니다. 세션을 저장하는 중입니다."
                        st.cookie_names = await asyncio.to_thread(session.save_cookies, st.account_id, cookies)
                        st.state, st.message = "done", "로그인이 끝났습니다. 창을 닫았습니다."
                        return
                await asyncio.sleep(POLL_SECONDS)
        except asyncio.CancelledError:
            st.state, st.message = "cancelled", "로그인을 중단했습니다."
            raise
        except errors.NotebookLMError as e:
            st.state, st.message = "failed", redact.mask(e.message)
        except Exception as e:  # noqa: BLE001 -- 원문 대신 클래스 이름만 남긴다
            st.state, st.message = "failed", f"로그인 창 처리에 실패했습니다({type(e).__name__})."
            log.warning("notebooklm login failed: %s", redact.safe_error(e))
        finally:
            await _close_gracefully(browser, proc)
            st.finished_at = time.time()
            metrics.event("login_finished", state=st.state, seconds=int(st.finished_at - (st.started_at or st.finished_at)))
            if on_done is not None:
                with contextlib.suppress(Exception):
                    await on_done(st)


manager = LoginManager()
