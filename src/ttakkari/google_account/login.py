"""Google 계정 1회 로그인 창(ADR 0045, 0051). 설치된 Chrome + 계정 전용 프로필 + 2단계 단기 루프백 CDP.

1단계(로그인): 사람이 보이는 Chrome 창에서 Google 로그인을 직접 한 번 한다. 원격 디버깅, 자동화 플래그, CDP 접속을 모두 쓰지 않는다.
따까리는 창 안에서 아무것도 클릭·입력하지 않는다.

2단계(쿠키 추출): 사용자가 창을 닫거나 '로그인 완료'를 누르면 1단계 창을 닫고, 같은 프로필로 headless Chrome을
--remote-debugging-port 와 루프백 주소(127.0.0.1)로 잠깐 띄워 Storage.getCookies 로 쿠키를 읽은 뒤 즉시 종료한다.
보호 신호(CAPTCHA·보안 확인 등)가 감지되거나 로그인이 완료되지 않았으면 세션을 저장하지 않고 안내한다.

로그인 창이 여는 주소와 완료 판정 호스트는 로그인을 시작한 기능(consumer)이 정한다. 같은 계정의 세션은 모든 기능이 공유한다.
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
from ttakkari.google_account import errors, metrics, redact, registry, session

log = logging.getLogger(__name__)

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
    feature: str | None = None  # 로그인을 시작한 기능(없으면 계정 관리 화면)
    cookie_names: list[str] = field(default_factory=list)

    @property
    def active(self) -> bool:
        return self.state in ACTIVE

    def to_dict(self) -> dict[str, Any]:
        return {
            "state": self.state,
            "message": self.message,
            "active": self.active,
            "feature": self.feature,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
        }


def find_chrome() -> Path | None:
    s = get_settings()
    if s.google_chrome_bin:
        p = Path(s.google_chrome_bin).expanduser()
        return p if p.exists() else None
    mac = Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
    if mac.exists():
        return mac
    for name in ("google-chrome", "google-chrome-stable", "chrome", "chromium", "chromium-browser"):
        found = shutil.which(name)
        if found:
            return Path(found)
    return None


def profile_chrome_pids(account_id: str = session.DEFAULT_ACCOUNT) -> list[int]:
    """그 계정의 전용 프로필을 쓰는 Chrome 프로세스(이전 실행이 남긴 창 포함). 옛 NotebookLM 경로로 뜬 창도 센다."""
    paths = {str(session.profile_dir(account_id))}
    legacy = session.legacy_profile_path()
    if legacy is not None and account_id == session.DEFAULT_ACCOUNT:
        paths.add(str(legacy))
    needles = {f"--user-data-dir={p}" for p in paths}
    found: list[int] = []
    for p in psutil.process_iter(["pid", "cmdline"]):
        try:
            cmd = p.info.get("cmdline") or []
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
        if cmd and "chrom" in (cmd[0] or "").lower() and any(n in (c or "") for n in needles for c in cmd):
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


def login_complete(urls: list[str], cookies: list[dict[str, Any]], target: registry.LoginTarget | None = None) -> bool:
    """대상 앱 화면에 도착했고(Google 로그인·확인 화면이 아님) 필수 세션 쿠키가 있으면 로그인 완료.

    target 이 없으면 등록된 어느 기능의 앱 화면이든 도착한 것으로 본다."""
    hosts = target.hosts if target is not None else registry.all_app_hosts()
    required = (*session.REQUIRED_COOKIES, *(target.required_cookies if target is not None else ()))
    on_app = any((urlparse(u).hostname or "") in hosts for u in urls)
    on_accounts = any((urlparse(u).hostname or "") == "accounts.google.com" for u in urls)
    names = {c.get("name") for c in cookies if session.is_google(c.get("domain", ""))}
    return on_app and not on_accounts and all(n in names for n in required)


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


async def _launch(chrome: Path, profile: Path, url: str) -> subprocess.Popen:
    """보이는 일반 Chrome 창을 전용 프로필로 띄운다(원격 디버깅·자동화 플래그 없음)."""
    profile.mkdir(parents=True, exist_ok=True)
    profile.chmod(0o700)
    proc = await asyncio.to_thread(
        subprocess.Popen,
        [
            str(chrome),
            f"--user-data-dir={profile}",
            "--no-first-run",
            "--no-default-browser-check",
            url,
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    metrics.incr("browser_launches")
    metrics.incr("browser_launches.visible")
    return proc


async def _launch_headless(chrome: Path, profile: Path, url: str) -> tuple[subprocess.Popen, str]:
    """쿠키 추출용 headless Chrome을 루프백 CDP로 잠깐 띄우고 주소를 읽는다."""
    profile.mkdir(parents=True, exist_ok=True)
    profile.chmod(0o700)
    proc = await asyncio.to_thread(
        subprocess.Popen,
        [
            str(chrome),
            f"--user-data-dir={profile}",
            "--headless=new",
            "--remote-debugging-port=0",
            "--remote-debugging-address=127.0.0.1",
            "--disable-gpu",
            "--no-first-run",
            "--no-default-browser-check",
            url,
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )
    metrics.incr("browser_launches")
    metrics.incr("browser_launches.headless")
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


async def _extract_session(
    chrome: Path, profile: Path, target: registry.LoginTarget,
) -> tuple[list[str], list[dict[str, Any]]]:
    """1단계 창이 종료된 뒤 headless Chrome을 잠깐 띄워 URL과 쿠키를 수집한다."""
    proc: subprocess.Popen | None = None
    browser: _Browser | None = None
    try:
        proc, ws_url = await _launch_headless(chrome, profile, target.url)
        browser = _Browser(ws_url)
        await browser.open()
        deadline = time.monotonic() + 5.0
        urls: list[str] = []
        cookies: list[dict[str, Any]] = []
        while time.monotonic() < deadline:
            urls = await browser.page_urls()
            cookies = await browser.cookies()
            if login_complete(urls, cookies, target) or protection_signal(urls):
                break
            await asyncio.sleep(0.5)
        return urls, cookies
    finally:
        await _close_gracefully(browser, proc)


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
    """계정별 로그인 창 수명주기. 같은 계정에 창은 하나만 연다(중복 실행 잠금). 계정이 다르면 동시에 열 수 있다."""

    def __init__(self) -> None:
        self._states: dict[str, LoginState] = {}
        self._tasks: dict[str, asyncio.Task] = {}
        self._cancel: dict[str, asyncio.Event] = {}
        self._complete: dict[str, asyncio.Event] = {}

    def state(self, account_id: str) -> LoginState:
        return self._states.get(account_id) or LoginState(account_id=account_id)

    async def start(self, account_id: str, on_done=None, feature: str | None = None) -> LoginState:
        session.check_account(account_id)
        s = get_settings()
        if not s.google_accounts_enabled:
            raise errors.Disabled("Google 계정 연결이 꺼져 있습니다.")
        target = registry.login_target(feature)
        if target.feature is not None and not registry.get(target.feature).enabled():
            raise errors.Disabled(f"{target.label} 연결이 꺼져 있습니다.")
        if self.state(account_id).active:
            raise errors.Busy("로그인 창이 이미 열려 있습니다. 그 창에서 로그인을 마치거나 취소해 주세요.")
        in_use = bool(await asyncio.to_thread(profile_chrome_pids, account_id))
        if in_use:
            raise errors.Busy("이전 Google 로그인 창이 남아 있습니다. 창을 닫거나 로그인 취소를 눌러 주세요.")
        await asyncio.to_thread(session.adopt_legacy_profile, account_id, in_use=False)
        chrome = await asyncio.to_thread(find_chrome)
        if chrome is None:
            raise errors.Upstream("설치된 Google Chrome을 찾지 못했습니다.")
        st = LoginState(account_id=account_id, state="starting", message="로그인 창을 여는 중입니다.",
                        started_at=time.time(), feature=target.feature)
        self._states[account_id] = st
        self._cancel[account_id] = asyncio.Event()
        self._complete[account_id] = asyncio.Event()
        metrics.incr("logins.started")
        task = asyncio.create_task(self._run(st, chrome, target, on_done))
        self._tasks[account_id] = task
        return st

    async def complete(self, account_id: str) -> LoginState:
        st = self.state(account_id)
        if not st.active:
            raise errors.Busy("진행 중인 로그인 창이 없습니다.")
        ev = self._complete.get(account_id)
        if ev is not None:
            ev.set()
        task = self._tasks.get(account_id)
        if task is not None:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(asyncio.shield(task), 20)
        return self.state(account_id)

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
        # 이전 프로세스가 남긴 창: 그 계정의 전용 프로필을 쓰는 Chrome 만 종료한다.
        pids = await asyncio.to_thread(profile_chrome_pids, account_id)
        for pid in pids:
            with contextlib.suppress(psutil.Error):
                psutil.Process(pid).terminate()
        if pids:
            st = LoginState(account_id=account_id, state="cancelled", message="남아 있던 로그인 창을 종료했습니다.",
                            finished_at=time.time())
            self._states[account_id] = st
        return self.state(account_id)

    def inflight(self) -> int:
        return sum(not t.done() for t in self._tasks.values())

    async def drain(self, timeout: float | None = 20.0) -> bool:
        """진행 중인 로그인 태스크의 완료를 기다린다. timeout 초과 시 shutdown() 후 False."""
        tasks = [t for t in self._tasks.values() if not t.done()]
        if not tasks:
            return True
        _done, unfinished = await asyncio.wait(tasks, timeout=timeout)
        if unfinished:
            await self.shutdown(timeout=min(2.0, timeout if timeout is not None else 2.0))
            return False
        return True

    async def shutdown(self, timeout: float = 20.0) -> None:
        for ev in self._cancel.values():
            ev.set()
        tasks = [t for t in self._tasks.values() if not t.done()]
        if tasks:
            for t in tasks:
                t.cancel()
            await asyncio.wait(tasks, timeout=timeout)

    async def _run(self, st: LoginState, chrome: Path, target: registry.LoginTarget, on_done) -> None:
        s = get_settings()
        cancel = self._cancel[st.account_id]
        complete = self._complete[st.account_id]
        deadline = time.monotonic() + s.google_login_timeout
        profile = session.profile_dir(st.account_id)
        proc: subprocess.Popen | None = None
        try:
            # 1단계: 보이는 일반 Chrome 창 띄우기 (원격 디버깅 없음)
            proc = await _launch(chrome, profile, target.url)
            st.state = "waiting"
            st.message = ("열린 Chrome 창에서 Google 로그인을 한 번 해 주세요. "
                          "마친 뒤 창을 닫거나 화면의 '로그인 완료'를 눌러 주세요.")
            while True:
                if cancel.is_set():
                    st.state, st.message = "cancelled", "로그인을 취소하고 창을 닫았습니다."
                    return
                if time.monotonic() > deadline:
                    st.state, st.message = "timeout", "시간 안에 로그인이 끝나지 않아 창을 닫았습니다. 다시 시도해 주세요."
                    return
                if complete.is_set() or proc.poll() is not None:
                    # 완료 신호(완료 버튼 또는 창 닫힘) -> 2단계 쿠키 수집으로 진행
                    break
                await asyncio.sleep(POLL_SECONDS)

            # 1단계 창 닫기 및 프로세스 종료 대기
            if proc.poll() is None:
                await _stop(proc)
            else:
                with contextlib.suppress(Exception):
                    await asyncio.to_thread(proc.wait, 5)

            # 잔여 프로세스가 프로필 락을 쥐고 있지 않도록 확인
            pids = await asyncio.to_thread(profile_chrome_pids, st.account_id)
            for pid in pids:
                with contextlib.suppress(psutil.Error):
                    psutil.Process(pid).terminate()
            if pids:
                await asyncio.sleep(0.5)

            # 2단계: headless Chrome으로 쿠키 추출
            st.state, st.message = "saving", "로그인 확인 후 세션을 저장하는 중입니다."
            urls, cookies = await _extract_session(chrome, profile, target)
            if login_complete(urls, cookies, target):
                st.cookie_names = await asyncio.to_thread(session.save_cookies, st.account_id, cookies)
                st.state, st.message = "done", "로그인이 끝났습니다. 세션을 저장했습니다."
            elif protection_signal(urls):
                st.state, st.message = "failed", (
                    "Google 보안 확인 화면이 감지되었습니다. 따까리는 이를 자동으로 넘기지 않습니다. "
                    "다시 로그인 창을 열어 보안 확인을 마쳐 주세요."
                )
            else:
                st.state, st.message = "cancelled", "로그인이 완료되지 않았습니다. 열린 창에서 Google 로그인을 마쳐 주세요."
        except asyncio.CancelledError:
            st.state, st.message = "cancelled", "로그인을 중단했습니다."
            raise
        except errors.GoogleAccountError as e:
            st.state, st.message = "failed", redact.mask(e.message)
        except Exception as e:  # noqa: BLE001 -- 원문 대신 클래스 이름만 남긴다
            st.state, st.message = "failed", f"로그인 창 처리에 실패했습니다({type(e).__name__})."
            log.warning("google account login failed: %s", redact.safe_error(e))
        finally:
            if proc is not None and proc.poll() is None:
                await _stop(proc)
            st.finished_at = time.time()
            metrics.incr(f"logins.{st.state}")
            metrics.event("login_finished", state=st.state, feature=st.feature,
                          seconds=int(st.finished_at - (st.started_at or st.finished_at)))
            if on_done is not None:
                with contextlib.suppress(Exception):
                    await on_done(st)


manager = LoginManager()
