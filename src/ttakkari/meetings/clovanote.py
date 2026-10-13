"""클로바노트(clovanote.naver.com) 브라우저 어댑터(ADR 0021).

클로바노트는 공개 API 가 없다. 따까리는 사용자가 네이버에 로그인해 둔 전용 Chrome 프로필을 Playwright 로 창 없이 띄우고,
웹 앱이 쓰는 내부 REST API(clovanote-api.naver.com/v2)를 같은 쿠키·세션 헤더로 부른다. 화면 클릭 자동화는 하지 않는다.
아래는 2026-10-13 실제 계정으로 확인한 사실과, 그 과정에서 겪은 함정이다. 바꾸기 전에 끝까지 읽는다.

## 1. 왜 사용자의 평소 Chrome 프로필을 못 쓰나
- Chrome 136 부터 기본 사용자 폴더(~/Library/Application Support/Google/Chrome)는 --remote-debugging-port/-pipe 를 무시한다.
  Playwright 는 이 통로로 Chrome 을 조종하므로 기본 프로필은 자동화할 수 없다.
- macOS TCC 가 다른 앱(터미널·서버)에서 그 폴더를 읽는 것을 막는다(Operation not permitted). 프로필 복사도 안 된다.
- 그래서 전용 프로필 폴더(설정 clovanote_profile_dir, 기본 ~/.ttakkari/clovanote-chrome)를 쓴다.

## 2. 로그인은 사람이, 자동화가 아닌 "일반 Chrome" 으로 한다
- Playwright 로 띄운 Chrome 에서 네이버에 로그인하면 navigator.webdriver=true·자동화 표시줄 때문에 네이버가 봇으로 보고
  아이디 보호(본인인증)를 건다. 실제로 걸렸다.
- 그래서 로그인 창은 `open -na "Google Chrome" --args --user-data-dir=<프로필>` 로 띄운 평범한 Chrome 이다(open_login_window).
  자동화 플래그가 없어 추가 인증 없이 로그인됐다.
- **'로그인 상태 유지'를 반드시 체크해야 한다.** 안 하면 NID_AUT/NID_SES 가 세션 쿠키(is_persistent=0)라 Chrome 을 닫는 순간 풀리고,
  따까리는 매번 Chrome 을 새로 띄우므로 계속 로그인이 필요해진다. 체크하면 NID_AUT 가 약 1년짜리 영구 쿠키가 된다.
  (만료 전이라도 비밀번호 변경·다른 기기의 전체 로그아웃·네이버 보안 정책으로 끊길 수 있다. 그때 화면이 '재로그인 필요'를 띄운다.)
- 로그인 감시(_watch_login)는 프로필의 Cookies(SQLite)를 복사해 읽어, 로그인 창을 연 뒤 새로 생긴 NID_AUT 가 영구인지 본다.
  영구면 창을 정상 종료(SIGTERM, 쿠키를 디스크에 쓴다)시키고 자동화 쪽에서 확인한다. 세션 쿠키면 체크하라고 알린다.
- 비밀번호는 어디에도 저장하지 않는다. 자동 입력은 네이버 봇 탐지를 더 세게 부른다.

## 3. 자동화 쪽 Chrome 을 띄우는 법 (_launch)
- channel="chrome"(설치된 실제 Chrome). Playwright 번들 Chromium 은 키체인 항목이 달라 쿠키를 못 푼다.
- ignore_default_args 에 **--use-mock-keychain** 을 꼭 넣는다. Playwright 는 macOS 에서 기본으로 가짜 키체인을 쓰는데,
  일반 Chrome 이 실제 키체인("Chrome Safe Storage")으로 암호화한 쿠키를 가짜 키로 열면 복호화에 실패하고 **Chrome 이 그 쿠키를 지운다.**
  이걸 몰라 로그인 쿠키를 한 번 날렸다.
- --enable-automation 도 빼고 --disable-blink-features=AutomationControlled, 일반 Chrome 과 같은 User-Agent(HeadlessChrome 문자열 제거)를 준다.
- 한 프로필은 한 Chrome 만 열 수 있다. 로그인 창이 떠 있는 동안 자동화는 기다린다(state=login_window).

## 4. 세션 헤더
- 내부 API 는 쿠키만으로는 400 "empty session id" 를 준다. 웹 앱이 기기 등록·세션 생성을 하고 note-session-id·note-device-id 헤더를 붙인다.
- 그래서 워크스페이스 홈을 실제로 열고, 앱이 보내는 요청에서 note-* 헤더를 가로채(_sniff) 같은 컨텍스트의 APIRequestContext 로 재사용한다.
  열어 둔 페이지가 60초마다 핑을 보내 세션(만료 600초)을 살려 둔다. 세션 오류가 나면 페이지를 다시 열어 한 번 재시도한다.

## 5. 쓰는 API (모두 https://clovanote-api.naver.com, 응답은 {"code":0,"message":"success","contents":...})
- GET  /v2/user                                       계정·워크스페이스 목록(로그인 안 됐으면 401)
- GET  /v2/w/{ws}/workspace/user-info/quota/recognition  남은 변환 시간(초 단위 remain), .../quota/summary 남은 요약 횟수
- GET  /v2/w/{ws}/notes?category=ALL&sortKey=UPDATED-DATE&sortOrder=DESC&offset=&limit=   노트 목록(contents.noteInfoSetList[].noteInfo)
- GET  /v2/w/{ws}/notes/{noteId}                      노트 정보 + script.blockList[{speakerId,start,end,text}](ms). 화자 이름은 noteInfo.attendeeList
- 업로드(웹 앱과 같은 순서):
  POST /v2/w/{ws}/notes {noteName, recognitionLanguage}             → contents.noteInfo.noteId (noteStatus=INIT)
  PUT  /v2/w/{ws}/notes/{id}/info/recognition-language {recognitionLanguage}
  POST /v2/w/{ws}/notes/{id}/audio/file/single?uploadDate=&audioStartDate=   multipart: audio(파일, 이름은 URL 인코딩), urlEncodedFileName
  POST /v2/w/{ws}/notes/{id}/recognition                            → 전사 시작(409 면 이미 시작됨)
  GET  /v2/w/{ws}/notes/{id} 를 폴링: noteStatus INIT→RECOGNITION→DONE, noteInfo.recognitionStatus.{status,recognitionProgress 0~100}
- 형식: m4a, mp3, aac, amr, wav. 최대 300MB(environment/configs.maxRecordingSize). 무료 계정은 월 300분.
- 8초짜리 음성은 업로드부터 전사 완료까지 약 4초였다.

웹 앱이 바뀌면(경로·헤더 이름) 이 모듈만 고친다. 파이프라인은 ClovaNoteProvider(providers.py)와 작업 큐(clova_jobs.py)를 통해서만 이 모듈을 쓴다.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import shutil
import signal
import sqlite3
import subprocess
import tempfile
import time
import urllib.parse
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import psutil

from ttakkari.config import get_settings

log = logging.getLogger(__name__)

WEB = "https://clovanote.naver.com"
API = "https://clovanote-api.naver.com"
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/154.0.0.0 Safari/537.36")
LOGIN_URL = "https://nid.naver.com/nidlogin.login?mode=form&url="
AUDIO_FORMATS = ("m4a", "mp3", "aac", "amr", "wav")
MAX_AUDIO_BYTES = 300 * 1024 * 1024
LOGIN_WATCH_SECONDS = 20 * 60
CHROME_EPOCH_OFFSET_US = 11_644_473_600 * 1_000_000  # Chrome 쿠키 시각은 1601-01-01 기준 마이크로초


class ClovaError(Exception):
    """사용자에게 그대로 보여 줄 수 있는 오류."""


class LoginRequired(ClovaError):
    pass


class Busy(ClovaError):
    pass


@dataclass
class LoginState:
    """로그인 창(일반 Chrome) 상태. 화면이 그대로 보여 준다."""

    state: str = "idle"  # idle | waiting | session_only | done | timeout | failed
    message: str | None = None
    started_at: float | None = None
    finished_at: float | None = None


@dataclass
class Status:
    state: str  # ready | login_required | login_window | disabled | error
    message: str
    checked_at: float = field(default_factory=time.time)
    account: dict[str, Any] | None = None
    workspace: dict[str, Any] | None = None
    quota: dict[str, Any] | None = None


def _iso_now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def parse_time(v: str | None) -> datetime | None:
    if not v or v.startswith("0001-"):
        return None
    with contextlib.suppress(ValueError):
        return datetime.fromisoformat(v)
    return None


def _profile() -> Path:
    return get_settings().clovanote_profile


def login_chrome_pids() -> list[int]:
    """로그인용으로 띄운 일반 Chrome(자동화 통로 없는 것)의 메인 프로세스."""
    needle = f"--user-data-dir={_profile()}"
    out = []
    for p in psutil.process_iter(["pid", "cmdline"]):
        cmd = p.info.get("cmdline") or []
        if needle in cmd and not any(a.startswith("--type=") for a in cmd) \
                and not any(a.startswith("--remote-debugging") for a in cmd):
            out.append(p.info["pid"])
    return out


def read_login_cookie() -> dict[str, Any] | None:
    """프로필의 NID_AUT 쿠키 메타(값은 읽지 않는다). Chrome 이 열고 있어도 되게 복사본을 읽는다."""
    src = _profile() / "Default" / "Cookies"
    if not src.exists():
        return None
    with tempfile.TemporaryDirectory() as d:
        dst = Path(d) / "Cookies"
        shutil.copy2(src, dst)
        if (j := src.with_name("Cookies-journal")).exists():
            shutil.copy2(j, Path(d) / "Cookies-journal")
        con = sqlite3.connect(f"file:{dst}?mode=ro", uri=True)
        try:
            row = con.execute("select creation_utc, expires_utc, is_persistent from cookies "
                              "where name='NID_AUT' and host_key like '%naver.com' order by creation_utc desc limit 1").fetchone()
        finally:
            con.close()
    if not row:
        return None
    created, expires, persistent = row
    return {"created": (created - CHROME_EPOCH_OFFSET_US) / 1e6, "expires": (expires - CHROME_EPOCH_OFFSET_US) / 1e6 if expires else None,
            "persistent": bool(persistent)}


class ClovaNoteClient:
    """프로필 하나를 여는 Chrome 하나. 모든 호출은 lock 안에서 차례로 한다(한 프로필은 동시에 한 Chrome 만)."""

    def __init__(self) -> None:
        self.lock = asyncio.Lock()
        self._pw = None
        self._ctx = None
        self._page = None
        self._headers: dict[str, str] = {}
        self._ws: str | None = None
        self._idle_task: asyncio.Task | None = None
        self._last_used = 0.0
        self.login = LoginState()
        self._login_task: asyncio.Task | None = None
        self.status_cache: Status | None = None

    # ---- 브라우저 수명

    @property
    def running(self) -> bool:
        return self._ctx is not None

    async def _launch(self) -> None:
        if login_chrome_pids():
            raise Busy("로그인 창(Chrome)이 열려 있어요. 로그인을 마치면 자동으로 이어져요.")
        from playwright.async_api import async_playwright

        s = get_settings()
        prof = _profile()
        prof.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._pw = await async_playwright().start()
        try:
            self._ctx = await self._pw.chromium.launch_persistent_context(
                str(prof), channel="chrome", headless=s.clovanote_headless, user_agent=UA,
                # 주석 3 참고: mock 키체인을 쓰면 실제 키체인으로 암호화된 로그인 쿠키를 Chrome 이 지워 버린다.
                ignore_default_args=["--enable-automation", "--use-mock-keychain"],
                args=["--disable-blink-features=AutomationControlled"])
        except Exception:
            await self._pw.stop()
            self._pw = None
            raise
        self._page = self._ctx.pages[0] if self._ctx.pages else await self._ctx.new_page()
        self._headers = {}

    async def close(self) -> None:
        ctx, pw = self._ctx, self._pw
        self._ctx = self._page = self._pw = None
        self._headers = {}
        with contextlib.suppress(Exception):
            if ctx is not None:
                await ctx.close()
        with contextlib.suppress(Exception):
            if pw is not None:
                await pw.stop()

    def _touch(self) -> None:
        self._last_used = time.monotonic()
        if self._idle_task is None or self._idle_task.done():
            self._idle_task = asyncio.create_task(self._idle_closer())

    async def _idle_closer(self) -> None:
        idle = get_settings().clovanote_idle_seconds
        while self.running:
            await asyncio.sleep(max(5.0, idle / 5))
            if time.monotonic() - self._last_used >= idle and not self.lock.locked():
                async with self.lock:
                    if time.monotonic() - self._last_used >= idle:
                        await self.close()
                        log.info("clovanote browser closed (idle)")

    async def _sniff(self, workspace_id: str | None) -> None:
        """워크스페이스 홈을 열어 앱이 만든 세션 헤더를 가로챈다(주석 4)."""
        got: dict[str, str] = {}
        done = asyncio.Event()

        def on_request(r) -> None:
            if "clovanote-api.naver.com" in r.url and "note-session-id" in r.headers and not got:
                got.update({k: v for k, v in r.headers.items() if k.startswith("note-") and k != "note-request-id"})
                done.set()

        self._page.on("request", on_request)
        try:
            url = f"{WEB}/w/{workspace_id}/home" if workspace_id else f"{WEB}/"
            await self._page.goto(url, wait_until="domcontentloaded", timeout=45_000)
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(done.wait(), 20)
        finally:
            self._page.remove_listener("request", on_request)
        if not got:
            raise LoginRequired("클로바노트에 로그인돼 있지 않아요. 재로그인해 주세요.")
        self._headers = {**got, "referer": f"{WEB}/", "origin": WEB}

    async def _ensure(self) -> None:
        if not get_settings().clovanote_enabled:
            raise ClovaError("클로바노트 연동이 꺼져 있어요(TTAKKARI_CLOVANOTE_ENABLED).")
        if not self.running:
            await self._launch()
        if not self._headers:
            ws = self._ws or get_settings().clovanote_workspace_id
            await self._sniff(ws)
            if self._ws is None:
                user = await self._raw("GET", "/v2/user")
                wss = (user or {}).get("workspaces") or []
                want = get_settings().clovanote_workspace_id
                pick = next((w for w in wss if w.get("workspaceId") == want), None) or (wss[0] if wss else None)
                if pick is None:
                    raise ClovaError("클로바노트 워크스페이스를 찾지 못했어요.")
                self._ws = pick["workspaceId"]
                if ws != self._ws:
                    await self._sniff(self._ws)
        self._touch()

    async def _raw(self, method: str, path: str, *, json: Any = None, multipart: dict | None = None,
                   timeout: float = 30_000) -> Any:
        r = await self._ctx.request.fetch(API + path, method=method, headers=self._headers, data=json, multipart=multipart,
                                          timeout=timeout, fail_on_status_code=False)
        try:
            body = await r.json()
        except Exception:  # noqa: BLE001
            body = {"code": r.status, "message": (await r.text())[:200]}
        if r.status == 401:
            raise LoginRequired("클로바노트 로그인이 풀렸어요. 재로그인해 주세요.")
        if r.status >= 400 or (isinstance(body, dict) and body.get("code") not in (0, None)):
            msg = body.get("message") if isinstance(body, dict) else str(body)
            raise ClovaError(f"클로바노트 API 오류({r.status}): {msg}")
        return body.get("contents") if isinstance(body, dict) else body

    async def api(self, method: str, path: str, **kw: Any) -> Any:
        """워크스페이스 경로는 '{ws}' 자리표시로 받는다. 세션이 끊겼으면 다시 열어 한 번 재시도한다."""
        await self._ensure()
        try:
            return await self._raw(method, path.replace("{ws}", self._ws or ""), **kw)
        except ClovaError as e:
            if isinstance(e, LoginRequired) or "session" not in str(e).lower():
                raise
            self._headers = {}
            await self._ensure()
            return await self._raw(method, path.replace("{ws}", self._ws or ""), **kw)

    # ---- 상태

    async def check(self) -> Status:
        """로그인·워크스페이스·남은 시간. 화면 상단 연결 카드와 Provider 의 준비 여부가 이 값을 쓴다."""
        if not get_settings().clovanote_enabled:
            st = Status("disabled", "클로바노트 연동이 꺼져 있어요.")
        elif login_chrome_pids():
            st = Status("login_window", self.login.message or "로그인 창이 열려 있어요. 로그인을 마치면 자동으로 이어져요.")
        else:
            try:
                async with self.lock:
                    user = await self.api("GET", "/v2/user")
                    rec = await self.api("GET", "/v2/w/{ws}/workspace/user-info/quota/recognition")
                    summ = await self.api("GET", "/v2/w/{ws}/workspace/user-info/quota/summary")
                ws = next((w for w in user.get("workspaces", []) if w.get("workspaceId") == self._ws), {})
                remain = sum(int(q.get("remain") or 0) for q in rec.get("recognition", []))
                st = Status("ready", "연결됨", account={"name": user.get("userName"), "email": user.get("email")},
                            workspace={"id": self._ws, "name": ws.get("name")},
                            quota={"recognition_remain_minutes": remain // 60,
                                   "summary_remain": sum(int(q.get("remain") or 0) for q in summ.get("summary", [])),
                                   "refresh_at": rec.get("refreshDate")})
            except LoginRequired as e:
                await self.close()
                st = Status("login_required", str(e))
            except Busy as e:
                st = Status("login_window", str(e))
            except Exception as e:  # noqa: BLE001
                log.warning("clovanote status check failed: %s", e)
                await self.close()
                st = Status("error", f"클로바노트 확인 중 오류: {str(e)[:200]}")
        self.status_cache = st
        return st

    # ---- 로그인 창(주석 2)

    async def open_login_window(self) -> LoginState:
        if self.login.state == "waiting" and login_chrome_pids():
            return self.login
        async with self.lock:
            await self.close()  # 같은 프로필을 자동화가 쥐고 있으면 일반 Chrome 이 못 연다
        ws = self._ws or get_settings().clovanote_workspace_id
        target = f"{WEB}/w/{ws}/home" if ws else f"{WEB}/"
        url = LOGIN_URL + urllib.parse.quote(target, safe="")
        prof = _profile()
        prof.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            await asyncio.to_thread(subprocess.run, ["open", "-na", "Google Chrome", "--args", f"--user-data-dir={prof}",
                                                     "--no-first-run", url], check=True, timeout=20)
        except Exception as e:  # noqa: BLE001
            self.login = LoginState("failed", f"Chrome 을 띄우지 못했어요: {e}")
            return self.login
        self.login = LoginState("waiting", "Mac 화면에 뜬 Chrome 에서 네이버에 로그인해 주세요. '로그인 상태 유지'를 꼭 체크해 주세요.",
                                started_at=time.time())
        if self._login_task is None or self._login_task.done():
            self._login_task = asyncio.create_task(self._watch_login(self.login.started_at))
        return self.login

    async def _watch_login(self, started: float) -> None:
        deadline = time.monotonic() + LOGIN_WATCH_SECONDS
        try:
            while time.monotonic() < deadline:
                await asyncio.sleep(3)
                pids = login_chrome_pids()
                ck = await asyncio.to_thread(read_login_cookie)
                fresh = ck is not None and ck["created"] >= started - 5
                if fresh and ck["persistent"]:
                    await asyncio.sleep(3)  # 클로바노트 첫 진입(기기 등록)까지 기다린다
                    await _terminate(login_chrome_pids())
                    self.login = LoginState("done", "로그인했어요.", started_at=started, finished_at=time.time())
                    await self.check()
                    return
                if fresh and not ck["persistent"]:
                    self.login.state = "session_only"
                    self.login.message = ("'로그인 상태 유지'가 꺼진 채 로그인됐어요. 이대로면 창을 닫는 순간 풀려요. "
                                          "네이버에서 로그아웃한 뒤 '로그인 상태 유지'를 체크하고 다시 로그인해 주세요.")
                if not pids:
                    # 사용자가 창을 닫았다. 영구 쿠키가 없으면 실패로 둔다.
                    ok = ck is not None and ck["persistent"] and (ck["expires"] or 0) > time.time()
                    self.login = LoginState("done" if ok else "failed",
                                            "로그인했어요." if ok else "로그인하지 않고 창이 닫혔어요.", started_at=started,
                                            finished_at=time.time())
                    await self.check()
                    return
            self.login = LoginState("timeout", "로그인을 20분 동안 기다렸어요. 다시 시도해 주세요.", started_at=started, finished_at=time.time())
        except Exception as e:
            log.exception("login watch failed")
            self.login = LoginState("failed", f"로그인 확인 중 오류: {e}", started_at=started, finished_at=time.time())

    # ---- 노트

    async def list_notes(self, limit: int = 50, max_pages: int = 40) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for page in range(max_pages):
            c = await self.api("GET", f"/v2/w/{{ws}}/notes?category=ALL&sortKey=UPDATED-DATE&sortOrder=DESC&offset={page * limit}&limit={limit}")
            rows = [r.get("noteInfo") or r for r in (c or {}).get("noteInfoSetList", [])]
            out += rows
            if len(rows) < limit:
                break
        return out

    async def get_note(self, note_id: str) -> dict[str, Any]:
        return await self.api("GET", f"/v2/w/{{ws}}/notes/{_q(note_id)}")

    async def create_note(self, name: str, language: str = "ko") -> str:
        info = (await self.api("POST", "/v2/w/{ws}/notes", json={"noteName": name[:100], "recognitionLanguage": language}))["noteInfo"]
        await self.api("PUT", f"/v2/w/{{ws}}/notes/{_q(info['noteId'])}/info/recognition-language", json={"recognitionLanguage": language})
        return info["noteId"]

    async def upload_audio(self, note_id: str, path: Path, filename: str, mime: str) -> dict[str, Any]:
        name = urllib.parse.quote(filename)
        now = _iso_now()
        data = await asyncio.to_thread(path.read_bytes)
        return await self.api("POST", f"/v2/w/{{ws}}/notes/{_q(note_id)}/audio/file/single?uploadDate={_q(now)}&audioStartDate={_q(now)}",
                              multipart={"audio": {"name": name, "mimeType": mime or "application/octet-stream", "buffer": data},
                                         "urlEncodedFileName": name}, timeout=15 * 60_000)

    async def start_recognition(self, note_id: str) -> None:
        try:
            await self.api("POST", f"/v2/w/{{ws}}/notes/{_q(note_id)}/recognition")
        except ClovaError as e:
            if "409" not in str(e):  # 이미 시작됨
                raise

    async def trash_note(self, note_id: str) -> None:
        await self.api("DELETE", f"/v2/w/{{ws}}/notes/{_q(note_id)}")

    @property
    def workspace_id(self) -> str | None:
        return self._ws


def _q(v: str) -> str:
    return urllib.parse.quote(v, safe="")


async def _terminate(pids: list[int]) -> None:
    """정상 종료(SIGTERM)는 쿠키를 디스크에 쓴다. 15초 안에 안 꺼지면 남긴다(강제 종료는 쿠키를 잃을 수 있다)."""
    for pid in pids:
        with contextlib.suppress(ProcessLookupError):
            os.kill(pid, signal.SIGTERM)
    for _ in range(30):
        if not login_chrome_pids():
            return
        await asyncio.sleep(0.5)


# ---- 노트 해석 (네트워크 없음, 테스트 대상)

def note_state(info: dict[str, Any]) -> str:
    status = (info.get("noteStatus") or "").upper()
    rec = ((info.get("recognitionStatus") or {}).get("status") or "").upper()
    if status == "DONE":
        return "ready"
    if "FAIL" in status or "ERROR" in status or "FAIL" in rec or "ERROR" in rec:
        return "failed"
    if status in ("INIT", "UPLOAD", "RECOGNITION", "RECORD", "PROCESSING") or rec in ("INIT", "ONGOING", "PROCESSING"):
        return "processing"
    return "unknown"


def speakers_of(info: dict[str, Any]) -> dict[str, str]:
    return {a["attendeeId"]: a.get("attendeeName") or "참석자" for a in info.get("attendeeList") or [] if a.get("attendeeId")}


def segments_of(detail: dict[str, Any]) -> list[dict[str, Any]]:
    info = detail.get("noteInfo") or {}
    names = speakers_of(info)
    out = []
    for b in (detail.get("script") or {}).get("blockList") or []:
        text = (b.get("text") or "").strip()
        if not text:
            continue
        sid = b.get("speakerId") or ""
        label = names.get(sid) or (f"참석자 {b['sttLabel']}" if b.get("sttLabel") else "참석자")
        out.append({"speaker": label, "start_ms": int(b.get("start") or 0), "end_ms": int(b.get("end") or 0), "text": text})
    return out


def _clock(ms: int) -> str:
    s = ms // 1000
    return f"{s // 3600:02d}:{s // 60 % 60:02d}:{s % 60:02d}" if s >= 3600 else f"{s // 60:02d}:{s % 60:02d}"


def transcript_of(segments: list[dict[str, Any]]) -> str:
    """분석 Run 이 읽는 전사 텍스트. 화자·시각을 앞에 붙인다(분석은 이 텍스트만 안다)."""
    return "\n".join(f"[{_clock(s['start_ms'])}] {s['speaker']}: {s['text']}" for s in segments)


def summary_of(info: dict[str, Any]) -> str | None:
    parts = [info.get("oneLineSummary") or ""]
    for a in info.get("abstractSummary") or []:
        parts.append(a if isinstance(a, str) else str(a.get("text") or a.get("summary") or ""))
    text = "\n".join(p for p in parts if p).strip()
    return text or None


client = ClovaNoteClient()
