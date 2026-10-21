"""계정별 사용량(5시간·주간·모델별 주간). ezwork fsw 의 ezChatUsage 와 같은 출처를 쓴다.

Claude: 구독 사용량 API(GET https://api.anthropic.com/api/oauth/usage, anthropic-beta: oauth-2025-04-20).
  토큰은 Claude Code 가 이미 둔 것을 읽기만 한다. macOS 키체인 서비스 이름은 기본 계정이
  `Claude Code-credentials`, CLAUDE_CONFIG_DIR 계정은 `Claude Code-credentials-<sha256(폴더)[:8]>`
  (2026-10-11 이 맥에서 등록 계정 3개로 대조). 다른 OS 는 `<폴더>/.credentials.json`.
  API 를 못 쓰면 따까리 실행 스트림의 `rate_limit_event` 로 받은 마지막 값을 쓴다.
Codex: ChatGPT 백엔드(GET https://chatgpt.com/backend-api/wham/usage, 토큰은 `<CODEX_HOME>/auth.json`).
  Orca(MIT, stablyai/orca `src/main/rate-limits/codex-backend-usage-client.ts`·`codex-reset-credit-client.ts`)에서
  확인한 규격이다. rate-limit 재설정 크레딧(`/wham/rate-limit-reset-credits`, `.../consume`)도 같은 출처.
  API 를 못 쓰면 `<CODEX_HOME>/sessions/**/*.jsonl` 의 마지막 `rate_limits`(primary=5시간, secondary=주간).

토큰 값은 이 모듈 밖으로 나가지 않는다. 화면에는 퍼센트와 초기화 시각만 준다.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import getpass
import hashlib
import json
import logging
import os
import re
import sys
import time
import uuid
from collections.abc import Callable
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx

from ttakkari.config import HOME, get_settings

log = logging.getLogger(__name__)
USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
KEYCHAIN = "Claude Code-credentials"
TTL = 120.0
TIMEOUT = 10.0


@dataclass
class Window:
    key: str  # five_hour | seven_day | seven_day_<model>
    label: str
    percent: int
    resets_at: int | None  # unix 초
    remaining_percent: float | None = None  # Antigravity 공식 remaining_fraction 을 보존한다.


@dataclass
class ResetCredits:
    """Codex rate-limit 재설정 크레딧. 한 번 쓰면 5시간·주간 한도가 바로 초기화된다."""

    available: int
    next_expires_at: int | None  # unix 초


@dataclass
class Usage:
    windows: list[Window] = field(default_factory=list)
    reset_credits: ResetCredits | None = None
    credits_balance: int | None = None  # Antigravity AI 크레딧 잔액. Codex 재설정 크레딧과 별개.
    credits_error: str | None = None
    source: str | None = None  # api | stream | session_log
    plan: str | None = None
    error: str | None = None
    updated_at: float | None = None
    login: str | None = None  # official read-only account state, when supplied by the provider

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def muse_quota_current(u: Usage, now: float) -> bool:
    """Both subscription windows must still be observed after a successful read.

    Historical windows remain visible and can still block exhausted accounts;
    a failed refresh or a reset without a new observation proves no spare quota.
    """
    current = {w.key for w in u.windows if w.resets_at and w.resets_at > now}
    return u.source == "muse_cli" and u.updated_at is not None and not u.error and {'five_hour', 'seven_day'} <= current


_cache: dict[uuid.UUID, tuple[float, Usage]] = {}
_stream: dict[uuid.UUID, Usage] = {}
# 새 측정값이 생길 때마다 부른다(runs/usage_store.py 가 DB 에 남긴다). 정하지 않으면 메모리에만 둔다.
_sink: Callable[[uuid.UUID, Usage], None] | None = None


def set_sink(fn: Callable[[uuid.UUID, Usage], None] | None) -> None:
    global _sink
    _sink = fn


def _emit(account_id: uuid.UUID, u: Usage) -> None:
    if _sink is not None and u.windows and u.source in ("api", "stream", "antigravity_cli", "muse_cli"):
        with contextlib.suppress(Exception):
            _sink(account_id, u)


def _validate_muse_window(u: Usage) -> Usage:
    if u.source != "muse_cli" or u.updated_at is None:
        return u
    valid = [w for w in u.windows if not (
        w.key == "five_hour" and w.resets_at and w.resets_at - u.updated_at > 5 * 3600 + 60
    )]
    if len(valid) == len(u.windows):
        return u
    # Older TUI parsing could move the reset minute forward by a whole day.
    # Preserve history, but never restore that impossible window as a live cap.
    log.warning("discarded Muse five-hour window beyond its observation horizon")
    return replace(u, windows=valid, error="Muse 5시간 초기화 시각 오류 · 새 사용량 확인 필요")


def seed(account_id: uuid.UUID, u: Usage, *, refresh: bool = False) -> None:
    """재시작 직후 DB 의 마지막 측정값으로 캐시를 채운다. 이미 값이 있으면 건드리지 않는다."""
    u = _validate_muse_window(u)
    prior = _cache.get(account_id)
    # 캐시 항목의 시각(prior[0])은 마지막으로 캐시를 쓴 때다. 실패한 조회도 그 시각을 갖는데 값의 updated_at 은 None 이라,
    # 값의 시각으로 비교하면 active 세대의 coordinator(load_latest(refresh=True))가 실패 결과를 며칠 전 DB 샘플로 되돌린다
    # (2026-10-19 x3: 401 이 이어지는데 화면은 20시간 전 "api 0%"를 오류 없이 보여 줬다).
    newer = refresh and prior is not None and (u.updated_at or 0) > prior[0]
    if u.updated_at and (prior is None or newer):
        _cache[account_id] = (u.updated_at, u)


def label_of(key: str) -> str:
    known = {"five_hour": "5시간", "seven_day": "주간"}
    if key in known:
        return known[key]
    if key.startswith("seven_day_"):
        m = key[len("seven_day_"):]
        return f"주간 {m[:1].upper()}{m[1:]}"
    return key.replace("_", " ")


def _ts(v: Any) -> int | None:
    if isinstance(v, (int, float)) and v > 0:
        return int(v)
    if isinstance(v, str) and v:
        with contextlib.suppress(ValueError):
            return int(datetime.fromisoformat(v).timestamp())
    return None


# ---- Antigravity CLI: 공식 /usage·/credits JSON 응답

AGY_TIMEOUT = 20.0


def windows_from_antigravity(body: dict[str, Any]) -> list[Window]:
    """`agy --print /usage --output-format json` 의 모델 그룹별 quota 를 읽는다.

    CLI JSON 은 공개 문서상 고정 API 계약은 아니므로 예상 필드가 없으면 빈 값으로 둔다.
    percent 는 기존 UI 호환을 위해 사용률, remaining_percent 는 원천 잔여율 보존용이다.
    """
    command = body.get("command") or {}
    data = command.get("data") or {}
    if body.get("status") != "SUCCESS" or command.get("name") != "usage":
        return []
    out: list[Window] = []
    for group in data.get("groups") or []:
        if not isinstance(group, dict) or not isinstance(group.get("name"), str):
            continue
        group_name = group["name"].strip()
        slug = re.sub(r"[^a-z0-9]+", "_", group_name.lower()).strip("_")
        if not slug:
            continue
        for bucket in group.get("buckets") or []:
            if not isinstance(bucket, dict):
                continue
            window = bucket.get("window")
            fraction = bucket.get("remaining_fraction")
            # 주간 한도가 찬 그룹의 5시간 버킷은 `disabled: true`, remaining_fraction 1, reset_time 없음으로 온다
            # (2026-10-18 실측). 한도가 적용되지 않는다는 뜻이라 "5시간 0% 사용"으로 보이면 안 된다.
            if bucket.get("disabled") is True:
                continue
            if window not in ("5h", "weekly") or not isinstance(fraction, (int, float)) or isinstance(fraction, bool) or not 0 <= fraction <= 1:
                continue
            remaining = float(fraction) * 100
            label = f"{group_name} · {'5시간' if window == '5h' else '주간'}"
            # 남은 양이 조금이라도 있으면 사용률을 100 으로 올림하지 않는다(한도 도달·차단 표시와 구분).
            percent = 100 if remaining <= 0 else min(99, round(100 - remaining))
            out.append(Window(f"agy_{slug}_{window}", label, percent, _ts(bucket.get("reset_time")), remaining))
    rank = {"5h": 0, "weekly": 1}
    return sorted(out, key=lambda w: rank.get(w.key.rsplit("_", 1)[-1], 2))


def credits_from_antigravity(body: dict[str, Any]) -> int | None:
    """`agy --print /credits --output-format json` 의 실제 AI 크레딧 잔액(0 포함)."""
    command = body.get("command") or {}
    value = (command.get("data") or {}).get("remaining_credits")
    if body.get("status") != "SUCCESS" or command.get("name") != "credits":
        return None
    if not isinstance(value, (int, float)) or isinstance(value, bool) or value < 0:
        return None
    return int(value)


class AgyCommandError(RuntimeError):
    """agy 명령이 0이 아닌 코드로 끝났다. detail 은 CLI 가 JSON 으로 준 오류 문구(짧게 자른 것)."""

    def __init__(self, message: str, detail: str = "") -> None:
        super().__init__(message)
        self.detail = detail


def _agy_error_detail(stdout: bytes) -> str:
    try:
        body = json.loads(stdout)
    except (UnicodeDecodeError, ValueError):
        return ""
    value = body.get("error") if isinstance(body, dict) else None
    return value[:200] if isinstance(value, str) else ""


async def _agy_command(command: str, config_dir: str | None = None) -> dict[str, Any]:
    env = os.environ.copy()
    env["AGY_CLI_DISABLE_AUTO_UPDATE"] = "true"
    cwd = None
    if config_dir:
        from ttakkari import antigravity_accounts as managed
        env = await asyncio.to_thread(managed.execution_env, config_dir, env)
        cwd = str(managed.probe_directory(config_dir))
    proc = await asyncio.create_subprocess_exec(
        get_settings().antigravity_bin, "--print", f"/{command}", "--output-format", "json", "--log-file", os.devnull,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, env=env, cwd=cwd,
    )
    try:
        stdout, _stderr = await asyncio.wait_for(proc.communicate(), AGY_TIMEOUT)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        raise TimeoutError("agy usage command timed out")
    if proc.returncode != 0:
        raise AgyCommandError(f"agy {command} exited {proc.returncode}", _agy_error_detail(stdout))
    try:
        body = json.loads(stdout)
    except (UnicodeDecodeError, ValueError) as e:
        raise ValueError(f"agy {command} returned invalid JSON") from e
    if not isinstance(body, dict):
        raise TypeError(f"agy {command} returned invalid JSON shape")
    return body


async def _fetch_antigravity(config_dir: str | None = None) -> Usage:
    try:
        commands = [_agy_command(cmd, config_dir) if config_dir else _agy_command(cmd) for cmd in ("usage", "credits")]
        usage_body, credits_body = await asyncio.gather(*commands, return_exceptions=True)
        if isinstance(usage_body, BaseException):
            return Usage(error=f"Antigravity quota 조회 실패({type(usage_body).__name__})")
        windows = windows_from_antigravity(usage_body)
        if not windows:
            response = str(usage_body.get("response") or "").lower()
            if usage_body.get("status") != "SUCCESS" and any(x in response for x in ("not logged in", "sign in", "login required", "not authenticated")):
                return Usage(error="Antigravity CLI 로그인 상태를 확인하지 못했어요")
            return Usage(error="Antigravity CLI quota 응답을 읽지 못했어요")
        credits = None if isinstance(credits_body, BaseException) else credits_from_antigravity(credits_body)
        credits_error = None
        if credits is None:
            # `no credits info found` 는 이 계정 플랜에 크레딧 정보가 없다는 공급자 답이다(2026-10-18 실측). 조회 실패와 다르다.
            no_info = isinstance(credits_body, AgyCommandError) and "no credits info" in credits_body.detail.lower()
            credits_error = "이 계정에는 Antigravity AI 크레딧 정보가 없어요" if no_info else "Antigravity 크레딧 조회 실패"
        return Usage(windows=windows, source="antigravity_cli", credits_balance=credits,
                     credits_error=credits_error, updated_at=time.time())
    except (OSError, RuntimeError, TimeoutError, TypeError, ValueError) as e:
        return Usage(error=f"Antigravity 조회 실패({type(e).__name__})")


# ---- Claude: 실행 스트림(rate_limit_event)

def windows_from_stream(info: dict[str, Any]) -> list[Window]:
    """claude stream-json `rate_limit_event.rate_limit_info.unifiedWindows`. utilization 은 0~1 비율."""
    out = []
    for key, w in (info.get("unifiedWindows") or {}).items():
        if not isinstance(w, dict) or "overage" in key or not isinstance(w.get("utilization"), (int, float)):
            continue
        out.append(Window(key, label_of(key), round(w["utilization"] * 100), _ts(w.get("resetsAt"))))
    return _ordered(out)


def remember_stream(account_id: uuid.UUID, windows: list[Window]) -> None:
    if windows:
        _stream[account_id] = Usage(windows=windows, source="stream", updated_at=time.time())
        _emit(account_id, _stream[account_id])


def remember_observation(account_id: uuid.UUID, body: dict[str, Any]) -> None:
    """Retain the provider's measurement time; replays never refresh old quota."""
    if body.get('source') != 'muse_cli' or not body.get('windows') or not body.get('updated_at'):
        return
    u = _validate_muse_window(Usage(**{**body, 'windows': [Window(**w) for w in body['windows']]}))
    prior = cached(account_id)
    if prior and (prior.updated_at or 0) >= u.updated_at:
        return
    _stream[account_id] = u
    _emit(account_id, u)


# ---- Claude: 구독 사용량 API

def windows_from_api(body: dict[str, Any]) -> list[Window]:
    """`limits[]`(kind: session·weekly_all·weekly_scoped, percent 0~100)를 먼저, 없으면 위쪽 키를 읽는다."""
    out: list[Window] = []
    for lim in body.get("limits") or []:
        if not isinstance(lim, dict) or not isinstance(lim.get("percent"), (int, float)):
            continue
        kind = lim.get("kind")
        if kind == "session":
            key = "five_hour"
        elif kind == "weekly_all":
            key = "seven_day"
        elif kind == "weekly_scoped":
            name = ((lim.get("scope") or {}).get("model") or {}).get("display_name")
            if not isinstance(name, str) or not name.strip():
                continue
            key = f"seven_day_{name.strip().lower()}"
        else:
            continue
        out.append(Window(key, label_of(key), round(lim["percent"]), _ts(lim.get("resets_at"))))
    if not out:
        for key in ("five_hour", "seven_day", "seven_day_opus", "seven_day_sonnet"):
            w = body.get(key)
            if isinstance(w, dict) and isinstance(w.get("utilization"), (int, float)):
                out.append(Window(key, label_of(key), round(w["utilization"]), _ts(w.get("resets_at"))))
    return _ordered(out)


def _ordered(ws: list[Window]) -> list[Window]:
    rank = {"five_hour": 0, "seven_day": 1}
    return sorted(ws, key=lambda w: (rank.get(w.key, 2), w.key))


def keychain_service(config_dir: str | None) -> str:
    if not config_dir:
        return KEYCHAIN
    return f"{KEYCHAIN}-{hashlib.sha256(config_dir.encode()).hexdigest()[:8]}"


async def _claude_oauth(config_dir: str | None) -> dict[str, Any] | None:
    """Claude Code 가 둔 claudeAiOauth 항목. 토큰 값은 이 모듈 밖으로 내보내지 않는다."""
    raw: str | None = None
    if sys.platform == "darwin":
        proc = await asyncio.create_subprocess_exec(
            "/usr/bin/security", "find-generic-password", "-s", keychain_service(config_dir), "-a", getpass.getuser(), "-w",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL, stdin=asyncio.subprocess.DEVNULL)
        try:
            out, _ = await asyncio.wait_for(proc.communicate(), TIMEOUT)
            raw = out.decode().strip() or None
        except TimeoutError:
            # 키체인이 허용 창을 띄우고 기다리는 경우다. 프로세스를 남기면 조회마다 하나씩 쌓인다.
            proc.kill()
            await proc.wait()
    if raw is None:
        f = (Path(config_dir) if config_dir else HOME / ".claude") / ".credentials.json"
        with contextlib.suppress(OSError):
            raw = f.read_text()
    if not raw:
        return None
    try:
        oauth = json.loads(raw).get("claudeAiOauth")
    except ValueError:
        return None
    tok = oauth.get("accessToken") if isinstance(oauth, dict) else None
    return oauth if isinstance(tok, str) and tok else None


def _expires_in(oauth: dict[str, Any], now: float | None = None) -> float | None:
    """access token 만료까지 남은 초(지났으면 음수). 만료 시각을 모르면 None."""
    exp = oauth.get("expiresAt")
    if isinstance(exp, bool) or not isinstance(exp, (int, float)) or exp <= 0:
        return None
    return exp / 1000 - (time.time() if now is None else now)


def _ago_text(seconds: float) -> str:
    seconds = max(0, int(seconds))
    return f"{seconds // 3600}시간 전" if seconds >= 3600 else f"{max(1, seconds // 60)}분 전"


# 사용량 API 는 access token(약 8시간)으로만 읽는다. 갱신은 Claude Code 가 그 계정으로 실행될 때 한다. 오래 쓰지 않은 계정은
# 토큰이 만료돼 401 이 이어지고, 따까리가 Run 을 보내지 않아 영영 낡은 값이 남았다(2026-10-19 x3, 약 20시간).
# 만료·401 이면 그 계정으로 아주 작은 claude 호출을 한 번 보내 CLI 가 스스로 토큰을 갱신하게 한다(우리가 refresh token 을 쓰거나
# 키체인에 쓰지 않는다). 거절·로그아웃 계정에 되풀이하지 않게 계정별 간격을 둔다.
LOGIN_REFRESH_GAP = 30 * 60.0
LOGIN_REFRESH_TIMEOUT = 60.0
_login_refresh_at: dict[str | None, float] = {}


async def _refresh_claude_login(config_dir: str | None) -> str | None:
    """CLI 로 토큰 갱신을 유도한다. 실제로 호출했으면 None, 건너뛰었거나 실패했으면 사유(짧은 문구)."""
    now = time.time()
    if now - _login_refresh_at.get(config_dir, 0.0) < LOGIN_REFRESH_GAP:
        return "최근에 갱신을 시도함"
    _login_refresh_at[config_dir] = now
    from ttakkari.runs.profiles import Profile
    env = Profile(uuid.uuid4(), "", "claude", Path(config_dir) if config_dir else None).env(os.environ.copy())
    proc = None
    try:
        proc = await asyncio.create_subprocess_exec(
            get_settings().claude_bin, "-p", "ok", "--model", "haiku", "--max-turns", "1", "--output-format", "json",
            "--no-session-persistence", "--disable-slash-commands", "--tools", "",
            cwd=os.path.realpath("/tmp"), env=env, stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        await asyncio.wait_for(proc.communicate(), LOGIN_REFRESH_TIMEOUT)
        return None if proc.returncode == 0 else f"claude 종료 코드 {proc.returncode}"
    except (OSError, TimeoutError) as e:
        return f"claude 실행 실패({type(e).__name__})"
    finally:
        if proc is not None and proc.returncode is None:
            proc.kill()
            await proc.wait()


def _api_error_text(r: httpx.Response) -> str:
    """사용량 API 오류 원문(공급자가 준 문구, 짧게)."""
    detail = ""
    with contextlib.suppress(ValueError, AttributeError, TypeError):
        body = r.json()
        err = body.get("error") if isinstance(body, dict) else None
        detail = (err.get("message") if isinstance(err, dict) else err) or ""
    text = f"사용량 API {r.status_code}" + (f": {str(detail)[:160]}" if detail else "")
    retry = r.headers.get("retry-after")
    return text + (f" · {retry}초 뒤 재시도 가능" if retry and retry.isdigit() else "")


async def _fetch_claude(config_dir: str | None) -> Usage:
    oauth = await _claude_oauth(config_dir)
    if not oauth:
        return Usage(error="로그인 토큰을 찾지 못했어요")
    note = ""
    left = _expires_in(oauth)
    if left is not None and left < 60:
        # 만료됐거나 곧 만료된다. 요청 전에 갱신해 401 을 한 번 덜 맞는다.
        why = await _refresh_claude_login(config_dir)
        fresh = await _claude_oauth(config_dir)
        if fresh:
            oauth = fresh
        left = _expires_in(oauth)
        if left is not None and left < 0:
            note = f" · 로그인 토큰이 {_ago_text(-left)} 만료됐고 갱신하지 못했어요" + (f"({why})" if why else "")
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT) as c:
            r = await c.get(USAGE_URL, headers={"Authorization": f"Bearer {oauth['accessToken']}",
                                                "anthropic-beta": "oauth-2025-04-20", "User-Agent": "ttakkari/0.1"})
            if r.status_code == 401 and not note:
                # 만료 시각이 아직 남았어도 서버가 거절했다(다른 곳에서 로그아웃·재로그인). 한 번 갱신을 유도하고 다시 읽는다.
                why = await _refresh_claude_login(config_dir)
                fresh = await _claude_oauth(config_dir)
                if fresh and fresh["accessToken"] != oauth["accessToken"]:
                    r = await c.get(USAGE_URL, headers={"Authorization": f"Bearer {fresh['accessToken']}",
                                                        "anthropic-beta": "oauth-2025-04-20", "User-Agent": "ttakkari/0.1"})
                elif why:
                    note = f" · 토큰 갱신을 건너뜀({why})"
        if r.status_code != 200:
            return Usage(error=_api_error_text(r) + note)
        windows = windows_from_api(r.json())
        if not windows:
            return Usage(error="사용량 API 응답에 5시간·주간 값이 없어요")
        return Usage(windows=windows, source="api", updated_at=time.time())
    except (httpx.HTTPError, ValueError) as e:
        return Usage(error=f"사용량 API 실패({type(e).__name__})" + note)


# ---- Codex: ChatGPT 백엔드(사용량·재설정 크레딧)

CODEX_API = "https://chatgpt.com/backend-api/wham"


def _codex_headers(config_dir: str | None) -> dict[str, str] | None:
    f = (Path(config_dir) if config_dir else HOME / ".codex") / "auth.json"
    try:
        tokens = json.loads(f.read_text()).get("tokens") or {}
    except (OSError, ValueError):
        return None
    tok = tokens.get("access_token")
    if not isinstance(tok, str) or not tok:
        return None
    h = {"Authorization": f"Bearer {tok}", "User-Agent": "codex-cli", "OpenAI-Beta": "codex-1", "originator": "Codex Desktop"}
    if tokens.get("account_id"):
        h["ChatGPT-Account-Id"] = tokens["account_id"]
    return h


class CodexApiError(Exception):
    """사용량 API 가 200 이 아니게 답했다. 토큰·응답 본문은 담지 않고 상태 코드와 오류 코드만 둔다."""

    def __init__(self, status: int, code: str = "") -> None:
        super().__init__(status, code)
        self.status, self.code = status, code


def _codex_error_code(r: httpx.Response) -> str:
    with contextlib.suppress(ValueError, AttributeError, TypeError):
        body = r.json()
        err = body.get("error") if isinstance(body, dict) else None
        code = (err.get("code") or err.get("type") if isinstance(err, dict) else err) or (body.get("code") if isinstance(body, dict) else "")
        return str(code)[:60] if isinstance(code, str) else ""
    return ""


async def _codex_get(config_dir: str | None, path: str) -> dict[str, Any] | None:
    """로그인 정보가 없으면 None, 200 이 아니면 CodexApiError."""
    h = _codex_headers(config_dir)
    if h is None:
        return None
    async with httpx.AsyncClient(timeout=TIMEOUT) as c:
        r = await c.get(f"{CODEX_API}/{path}", headers=h)
    if r.status_code != 200:
        raise CodexApiError(r.status_code, _codex_error_code(r))
    return r.json()


async def _codex_post(config_dir: str | None, path: str, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    h = _codex_headers(config_dir)
    if h is None:
        return 401, {}
    async with httpx.AsyncClient(timeout=30.0) as c:
        r = await c.post(f"{CODEX_API}/{path}", headers={**h, "Content-Type": "application/json"}, json=body)
    try:
        return r.status_code, r.json()
    except ValueError:
        return r.status_code, {}


def windows_from_codex(body: dict[str, Any]) -> list[Window]:
    rl = body.get("rate_limit") or {}
    out = []
    for part in ("primary_window", "secondary_window"):
        w = rl.get(part)
        if not isinstance(w, dict) or not isinstance(w.get("used_percent"), (int, float)):
            continue
        secs = w.get("limit_window_seconds") or 0
        key = "five_hour" if secs and secs <= 10 * 3600 else "seven_day"
        out.append(Window(key, label_of(key), round(w["used_percent"]), _ts(w.get("reset_at"))))
    return _ordered(out)


def credits_from(usage_body: dict[str, Any], credits_body: dict[str, Any] | None) -> ResetCredits | None:
    raw = (credits_body or usage_body.get("rate_limit_reset_credits")) or {}
    n = raw.get("available_count")
    if not isinstance(n, (int, float)):
        return None
    exp = sorted(t for c in raw.get("credits") or [] if (c or {}).get("status") == "available"
                 for t in [_ts(c.get("expires_at"))] if t)
    return ResetCredits(available=max(0, int(n)), next_expires_at=exp[0] if exp else None)


def _codex_token(config_dir: str | None) -> str | None:
    """갱신 여부 비교용 지문(토큰 원문은 밖으로 내보내지 않는다)."""
    h = _codex_headers(config_dir)
    return hashlib.sha256(h["Authorization"].encode()).hexdigest() if h else None


def _codex_token_expires_in(config_dir: str | None) -> float | None:
    """access token(JWT)의 exp 까지 남은 초. 읽을 수 없으면 None."""
    h = _codex_headers(config_dir)
    parts = h["Authorization"].removeprefix("Bearer ").split(".") if h else []
    if len(parts) != 3:
        return None
    with contextlib.suppress(ValueError, TypeError, AttributeError):
        payload = parts[1] + "=" * (-len(parts[1]) % 4)
        exp = json.loads(base64.urlsafe_b64decode(payload)).get("exp")
        if isinstance(exp, (int, float)):
            return exp - time.time()
    return None


# Claude 와 같다: access token 은 Codex CLI 가 실행될 때만 갱신된다. 오래 안 쓴 계정은 401 이 이어진다.
# 만료·401 이면 그 계정으로 아주 작은 codex 호출을 한 번 보내 CLI 가 스스로 갱신하게 한다. 우리가 refresh token 을 쓰거나
# auth.json 에 쓰지 않는다(refresh token 은 한 번만 쓸 수 있어 CLI 와 경합하면 로그인이 깨진다).
_codex_refresh_at: dict[str | None, float] = {}


async def _refresh_codex_login(config_dir: str | None) -> str | None:
    """CLI 로 토큰 갱신을 유도한다. 실제로 호출했으면 None, 건너뛰었거나 실패했으면 사유(짧은 문구)."""
    now = time.time()
    if now - _codex_refresh_at.get(config_dir, 0.0) < LOGIN_REFRESH_GAP:
        return "최근에 갱신을 시도함"
    _codex_refresh_at[config_dir] = now
    from ttakkari.runs.profiles import Profile
    env = Profile(uuid.uuid4(), "", "codex", Path(config_dir) if config_dir else None).env(os.environ.copy())
    proc = None
    try:
        proc = await asyncio.create_subprocess_exec(
            get_settings().codex_bin, "exec", "--skip-git-repo-check", "--ephemeral", "-s", "read-only", "ok",
            cwd=os.path.realpath("/tmp"), env=env, stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        await asyncio.wait_for(proc.communicate(), LOGIN_REFRESH_TIMEOUT)
        return None if proc.returncode == 0 else f"codex 종료 코드 {proc.returncode}"
    except (OSError, TimeoutError) as e:
        return f"codex 실행 실패({type(e).__name__})"
    finally:
        if proc is not None and proc.returncode is None:
            proc.kill()
            await proc.wait()


def _codex_failure_text(e: Exception, note: str = "") -> str:
    if isinstance(e, CodexApiError):
        what = f"사용량 API {e.status}" + (f"({e.code})" if e.code else "")
        if e.status in (401, 403):
            what += " · Codex 로그인이 만료됐거나 거절됐어요. 재로그인이 필요할 수 있어요"
        return what + note
    return f"사용량 API 실패({type(e).__name__})" + note


async def _fetch_codex(config_dir: str | None) -> Usage:
    """실패하면 windows 없는 Usage(error=사유). 호출부가 세션 로그로 대신하되 사유는 남긴다."""
    note = ""
    left = _codex_token_expires_in(config_dir)
    if left is not None and left < 60:
        why = await _refresh_codex_login(config_dir)
        left = _codex_token_expires_in(config_dir)
        if left is not None and left < 0:
            note = f" · 로그인 토큰이 {_ago_text(-left)} 만료됐고 갱신하지 못했어요" + (f"({why})" if why else "")
    try:
        try:
            body = await _codex_get(config_dir, "usage")
        except CodexApiError as e:
            if e.status != 401 or note:
                raise
            # 만료 시각이 남았어도 서버가 거절했다(다른 곳에서 로그아웃·재로그인). 한 번 갱신을 유도하고 다시 읽는다.
            before = _codex_token(config_dir)
            why = await _refresh_codex_login(config_dir)
            if _codex_token(config_dir) == before:
                note = f" · 토큰이 갱신되지 않았어요({why or 'codex 호출 뒤에도 같은 토큰'})"
                raise
            body = await _codex_get(config_dir, "usage")
        if body is None:
            return Usage(error="Codex 로그인 정보(auth.json)를 찾지 못했어요")
        if not isinstance(body.get("plan_type"), str):
            return Usage(error="사용량 API 응답 형식이 달라요" + note)
        credits = credits_from(body, None)
        if credits and credits.available > 0:
            # 사용량 응답엔 개수만 온다. 다음 만료 시각은 크레딧 목록에서 읽는다. 목록을 못 읽어도 사용량은 성공이다.
            with contextlib.suppress(httpx.HTTPError, ValueError, CodexApiError):
                credits = credits_from(body, await _codex_get(config_dir, "rate-limit-reset-credits")) or credits
        return Usage(windows=windows_from_codex(body), source="api", plan=body["plan_type"], reset_credits=credits,
                     updated_at=time.time())
    except (httpx.HTTPError, ValueError, CodexApiError) as e:
        return Usage(error=_codex_failure_text(e, note))


async def _codex_usage_or_log(config_dir: str | None) -> Usage:
    """API 값을 우선하고, 못 읽으면 세션 로그로 대신한다. 대신한 값이 낡았다면 API 실패 사유를 붙인다."""
    u = await _fetch_codex(config_dir)
    if u.windows:
        return u
    log = await asyncio.to_thread(codex_usage, config_dir)
    if not log.windows:
        return u if u.error else log
    if log.updated_at is None or time.time() - log.updated_at > get_settings().usage_stale_seconds:
        return replace(log, error=f"{u.error} · 세션 로그의 마지막 값" if u.error else log.error)
    return log


RESET_OUTCOME = {
    "reset": "재설정했어요. 한도가 바로 풀렸어요.",
    "nothing_to_reset": "지금은 재설정할 한도가 없어요(크레딧은 그대로예요).",
    "no_credit": "남은 재설정 크레딧이 없어요.",
    "already_redeemed": "이미 처리된 요청이에요.",
}


async def codex_reset(config_dir: str | None) -> tuple[str, str]:
    """rate-limit 재설정 크레딧 한 개를 쓴다. (결과 코드, 사람 말). 같은 요청이 두 번 쓰이지 않게 요청 id 를 준다."""
    status, body = await _codex_post(config_dir, "rate-limit-reset-credits/consume", {"redeem_request_id": str(uuid.uuid4())})
    if status == 401:
        return "not_signed_in", "Codex 로그인 정보를 찾지 못했어요."
    if status != 200:
        return "error", f"재설정 요청이 실패했어요(HTTP {status})."
    code = str(body.get("code") or "unknown")
    return code, RESET_OUTCOME.get(code, f"알 수 없는 결과: {code}")


# ---- Codex: 세션 로그

def _find_rate_limits(x: Any) -> dict | None:
    if isinstance(x, dict):
        if isinstance(x.get("rate_limits"), dict):
            return x["rate_limits"]
        for v in x.values():
            r = _find_rate_limits(v)
            if r:
                return r
    return None


def codex_usage(config_dir: str | None) -> Usage:
    root = (Path(config_dir) if config_dir else HOME / ".codex") / "sessions"
    files = sorted(root.rglob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)[:5] if root.is_dir() else []
    for f in files:
        last = None
        with contextlib.suppress(OSError):
            for line in f.open(encoding="utf-8", errors="replace"):
                if '"rate_limits"' not in line:
                    continue
                with contextlib.suppress(ValueError):
                    rl = _find_rate_limits(json.loads(line))
                    if rl and (rl.get("primary") or rl.get("secondary")):
                        last = rl
        if last:
            ws = []
            now = time.time()
            for part in ("primary", "secondary"):
                w = last.get(part)
                if not isinstance(w, dict) or not isinstance(w.get("used_percent"), (int, float)):
                    continue
                resets_at = _ts(w.get("resets_at"))
                if resets_at and resets_at <= now and round(w["used_percent"]) >= 100:
                    continue
                minutes = w.get("window_minutes") or 0
                key = "five_hour" if minutes and minutes <= 600 else "seven_day"
                ws.append(Window(key, label_of(key), round(w["used_percent"]), resets_at))
            if ws:
                return Usage(windows=_ordered(ws), source="session_log", plan=last.get("plan_type"),
                             updated_at=f.stat().st_mtime)
            return Usage(error="Codex 초기화 시각 경과 · 세션 로그 기준 사용량 만료", plan=last.get("plan_type"),
                         updated_at=f.stat().st_mtime)
    return Usage(error="아직 따까리에서 쓴 기록이 없어요")


# ---- 묶기

_agy_cache_roots: dict[uuid.UUID, str | None] = {}


# 같은 계정을 동시에 읽는 호출(수집기·화면·재시도)은 조회 하나를 같이 기다린다. 요청이 먼저 끝나도(시간 초과·탭 닫힘) 조회는 이어져 캐시를 채운다.
_inflight: dict[tuple[uuid.UUID, str, str | None], asyncio.Task[Usage]] = {}


def fetching(account_id: uuid.UUID) -> bool:
    """이 계정의 실제 조회가 지금 진행 중인가."""
    return any(k[0] == account_id for k in _inflight)


def _ready_value(account_id: uuid.UUID, engine: str, config_dir: str | None, refresh: bool) -> Usage | None:
    """네트워크 없이 바로 답할 수 있으면 그 값(격리 오류 또는 TTL 안의 캐시), 아니면 None."""
    # A non-default account must never inherit the host's quota, including a
    # cached value measured before its account path was changed.
    if engine == "antigravity" and config_dir:
        from ttakkari import antigravity_accounts as managed
        try:
            p = managed.load(config_dir)
            if p.account_id != account_id or not managed.is_ready(config_dir):
                raise ValueError("not ready")
        except (ValueError, OSError):
            return Usage(error="Antigravity isolation: profile is not ready; default quota was not queried")
    hit = _cache.get(account_id)
    if engine == "antigravity" and _agy_cache_roots.get(account_id) != config_dir:
        hit = None
    # 수집기가 주기마다 새로 읽어 두므로(runs/usage_collector.py) 화면이 그보다 자주 API 를 두드리지 않게 한다.
    ttl = max(TTL, get_settings().usage_poll_seconds)
    if hit and not refresh and time.time() - hit[0] < ttl:
        cached_u = hit[1]
        now = time.time()
        # 모든 창이 초기화 시각이 지난 경우 캐시 만료로 보고 새로 조회한다.
        if cached_u.windows and all(w.resets_at and w.resets_at <= now for w in cached_u.windows):
            return None
        # 한도에 닿았던 창(100% 이상)의 초기화 시각이 지난 경우 캐시 만료로 보고 새로 조회한다.
        if cached_u.windows and any(w.resets_at and w.resets_at <= now and w.percent >= 100 for w in cached_u.windows):
            return None
        # Antigravity 는 창마다 따로 초기화된다. 한 창이라도 지났으면 성공한 측정에 한해 새로 읽는다
        # (실패 뒤 유지한 값은 TTL 을 따른다. 아니면 공급자 장애 중에 화면마다 하위 프로세스를 띄운다).
        if (engine == "antigravity" and not cached_u.error
                and any(w.resets_at and w.resets_at <= now for w in cached_u.windows)):
            return None
        return cached_u
    return None


async def usage_for(account_id: uuid.UUID, engine: str, config_dir: str | None, *, refresh: bool = False) -> Usage:
    ready = _ready_value(account_id, engine, config_dir, refresh)
    if ready is not None:
        return ready
    key = (account_id, engine, config_dir)
    task = _inflight.get(key)
    if task is None:
        task = asyncio.ensure_future(_fetch_live_bounded(account_id, engine, config_dir))
        _inflight[key] = task

        def _done(t: asyncio.Task[Usage]) -> None:
            if _inflight.get(key) is t:
                del _inflight[key]
            if not t.cancelled() and t.exception():
                log.warning("usage fetch crashed: %s", type(t.exception()).__name__)

        task.add_done_callback(_done)
    return await asyncio.shield(task)


# 조회 하나가 영영 끝나지 않으면 _inflight 에 남아 그 계정의 이후 모든 조회(수집기·화면·새로고침)가 같은 작업을 기다린다.
# 엔진마다 하위 단계 시간 제한의 합보다 조금 길게 잡은 상한이다(Claude: 갱신 호출 60초 + 조회, Muse: SDK 25초 + TUI 23초).
FETCH_DEADLINE = {"claude": 100.0, "muse": 110.0, "antigravity": 50.0, "codex": 45.0}


async def _fetch_live_bounded(account_id: uuid.UUID, engine: str, config_dir: str | None) -> Usage:
    limit = FETCH_DEADLINE.get(engine, 60.0)
    try:
        return await asyncio.wait_for(_fetch_live(account_id, engine, config_dir), limit)
    except TimeoutError:
        # 실패도 캐시에 남겨 화면이 낡은 값을 현재 값처럼 보이지 않게 한다(_fetch_live 의 실패 경로와 같은 모양).
        failed = Usage(error=f"사용량 조회가 {limit:.0f}초 안에 끝나지 않았어요({engine})")
        _cache[account_id] = (time.time(), failed)
        return failed


async def _fetch_live(account_id: uuid.UUID, engine: str, config_dir: str | None) -> Usage:
    observed_now = True
    if engine == "muse":
        u = await _fetch_muse(config_dir)
        if MUSE_PING and not u.windows and u.login == "ok":
            # 공급자는 Muse 를 쓴 뒤에야 사용량을 준다. 안 쓰면 조회만으로는 값이 없고 그 계정이 계속 배제된다.
            pinged = await _muse_observation_ping(account_id, config_dir)
            if isinstance(pinged, Usage):
                u = pinged
            else:
                u = replace(u, error=f"{u.error} · {pinged}" if u.error else pinged)
        prior = cached(account_id)
        if u.login == 'missing':
            _stream.pop(account_id, None)
        elif not u.windows and u.login == 'ok' and prior and prior.source == 'muse_cli' and prior.windows:
            now = time.time()
            valid = [w for w in prior.windows if not (w.resets_at and w.resets_at <= now and w.percent >= 100)]
            if valid and len(valid) == len(prior.windows):
                u = replace(prior, login=u.login, error=u.error or 'Muse 새 사용량 미관측 · 마지막 관측값')
            elif valid:
                reset_msg = f"{u.error} · 초기화 시각 경과" if u.error else "Muse 초기화 시각 경과 · 새 사용량 미관측"
                u = replace(prior, windows=valid, login=u.login, error=reset_msg)
            else:
                reset_msg = f"{u.error} · 초기화 시각 경과" if u.error else "Muse 초기화 시각 경과 · 새 사용량 미관측"
                u = replace(u, plan=prior.plan, error=reset_msg)
            if account_id in _stream:
                s_valid = [w for w in _stream[account_id].windows if not (w.resets_at and w.resets_at <= now and w.percent >= 100)]
                if s_valid:
                    _stream[account_id] = replace(_stream[account_id], windows=s_valid)
                else:
                    _stream.pop(account_id, None)
            if len(valid) != len(prior.windows):
                _emit(account_id, u)
            observed_now = False
    elif engine == "antigravity":
        # 재시작 직후 DB 에서 채운 값은 계정 id 로 저장돼 있어 root 기록이 없어도 쓴다. 폴더가 바뀐 계정의 값은 쓰지 않는다.
        prior = cached(account_id) if _agy_cache_roots.get(account_id, config_dir) == config_dir else None
        u = await _fetch_antigravity(config_dir) if config_dir else await _fetch_antigravity()
        _agy_cache_roots[account_id] = config_dir
        if not u.windows and prior and prior.source == "antigravity_cli" and prior.windows:
            # 조회 실패가 마지막 관측값을 지우면 안 된다: 화면이 창을 잃고, 한도가 찬 그룹의 차단도 풀린다.
            # 값은 그대로 두되 오류를 붙여 낡은 값으로 표시한다. updated_at 은 실제 관측 시각을 유지한다.
            u = replace(prior, error=f"{u.error} · 마지막 관측값" if u.error else "Antigravity 새 사용량 미관측 · 마지막 관측값")
            observed_now = False
    elif engine == "claude":
        u = await _fetch_claude(config_dir)
        if not u.windows and account_id in _stream:
            # API 를 못 쓰면 실행 스트림에서 받은 마지막 값으로 대신한다(오류 문구는 남긴다).
            s = _stream[account_id]
            u = Usage(windows=s.windows, source="stream", updated_at=s.updated_at, error=u.error)
    else:
        u = await _codex_usage_or_log(config_dir)
    _cache[account_id] = (time.time(), u)
    if observed_now and u.source in ("api", "antigravity_cli", "muse_cli"):  # 스트림 값으로 대신한 경우는 remember_stream 이 이미 남겼다.
        _emit(account_id, u)
    return u


def muse_usage_from_probe(body: dict[str, Any]) -> Usage:
    """Only provider-observed subscription windows; absence never means zero."""
    account_state = (body.get("account") or {}).get("state")
    login = "ok" if account_state == "accountLogin" else "missing" if account_state == "loggedOut" else "unknown"
    snapshot = (body.get("usage") or {}).get("usage") or {}
    if not isinstance(snapshot, dict):
        return Usage(error="Muse 사용량 응답 형식 오류", login=login)
    observed = snapshot.get("observedAtMs")
    windows = []
    for window_name, key in (("window", "five_hour"), ("weekly", "seven_day")):
        window = snapshot.get(window_name) or {}
        if not isinstance(window, dict):
            return Usage(error="Muse 사용량 창 형식 오류", login=login)
        percent, resets = window.get("usedPercent"), window.get("resetsAtMs")
        if (not isinstance(percent, int) or isinstance(percent, bool) or percent < 0
                or not isinstance(resets, int) or isinstance(resets, bool) or resets <= 0):
            error = "Muse 구독 로그인이 필요해요" if login == "missing" else "Muse 로그인 확인 · SDK 구독 사용량 미관측. Muse 터미널의 /usage에서 확인할 수 있어요." if login == "ok" else "Muse 구독 사용량 미관측"
            return Usage(error=error, login=login)
        windows.append(Window(key, label_of(key), percent, resets // 1000))
    if not isinstance(observed, int) or isinstance(observed, bool) or observed <= 0:
        return Usage(error="Muse 사용량 관측 시각이 없어요", login=login)
    return _validate_muse_window(Usage(windows=windows, source="muse_cli", plan=snapshot.get("tier"), updated_at=observed / 1000, login=login))




async def _fetch_muse(config_dir: str | None) -> Usage:
    settings = get_settings()
    from ttakkari.runs.profiles import Profile
    env = os.environ.copy()
    env.update(Profile(uuid.uuid4(), "", "muse", Path(config_dir) if config_dir else None).env())
    probe = Path(__file__).resolve().parents[3] / ".muse-runtime/probe.mjs"
    proc = None
    try:
        proc = await asyncio.create_subprocess_exec(settings.muse_node_bin, str(probe), settings.muse_bin,
            "backend_usage_collector", str(settings.muse_runtime_root), "--usage-only", env=env,
            stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        stdout, _stderr = await asyncio.wait_for(proc.communicate(), 25)
        if proc.returncode not in (0, 2):
            return Usage(error=f"Muse SDK 조회 프로세스 종료({proc.returncode})")
        body = json.loads(stdout)
        if not isinstance(body, dict):
            return Usage(error="Muse 조회 응답 형식 오류")
        result = muse_usage_from_probe(body)
        if result.login == 'ok' and not result.windows:
            native = await _fetch_muse_native(settings.muse_bin, env)
            refreshed = muse_usage_from_probe(native)
            if refreshed.windows:
                return refreshed
            result.plan = native.get('plan')
            status = native.get('quota_status', 'unknown')
            result.error = ('Muse 로그인 확인 · /usage 구독 사용량 미관측: ' + status)
        return result
    except (OSError, ValueError, TimeoutError) as error:
        return Usage(error=f"Muse 조회 실패({type(error).__name__})")
    finally:
        if proc is not None and proc.returncode is None:
            proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), 4)
            except TimeoutError:
                proc.kill()
                await proc.wait()


# ---- Muse: 관측이 없을 때 짧은 핑 한 번으로 스트림 관측을 유도한다

MUSE_PING = True  # 시험은 공급자·DB 를 건드리지 않게 끈다(tests/conftest.py). 핑 시험만 켠다.
MUSE_PING_TIMEOUT = 45.0
MUSE_PING_PROMPT = "hi"
MUSE_PING_NOTE = "Reply with the single word ok. Do not use any tools."


def _muse_ping_wait_text(account_id: uuid.UUID) -> str:
    from ttakkari.runs import profiles
    last = profiles._probe_last.get(account_id)
    left = max(1, round((profiles.MUSE_PROBE_INTERVAL - (time.time() - last)) / 60)) if last else None
    return "핑은 계정당 30분에 한 번만 보내요" + (f"(다음 시도 약 {left}분 뒤)" if left else "")


async def _muse_observation_ping(account_id: uuid.UUID, config_dir: str | None) -> Usage | str:
    """사용량 관측이 없는 켜진 Muse 계정에 'hi' 한 번을 보내 공급자가 사용량을 내게 한다.

    성공하면 관측한 Usage, 건너뛰었거나 실패했으면 화면에 그대로 붙일 사유 문장을 돌려준다(실패를 여유 있음으로 읽지 않는다).
    계정당 30분에 한 번(탐색 배정과 같은 DB 예약을 공유해 세대가 여럿이어도 한 번)이고, 꺼진 계정·Run 이 쓰는 계정·알려진 한도는 제외한다."""
    from ttakkari.db import sessionmaker
    from ttakkari.models import EngineAccount
    from ttakkari.runs import profiles
    async with sessionmaker()() as db:
        row = await db.get(EngineAccount, account_id)
    if row is None or not (row.enabled or (row.pool == "work" and row.work_enabled)):
        return "꺼진 계정이라 핑을 보내지 않아요"
    if (await profiles.running_counts_async()).get(account_id, 0) > 0:
        return "이 계정으로 실행 중인 Run 이 있어 핑을 보내지 않아요(Run 이 끝나면 관측돼요)"
    profile = profiles.Profile(account_id, row.name, "muse", Path(config_dir) if config_dir else None,
                               max_used_percent=row.max_used_percent, model=get_settings().worker_default_models["muse"]["model"])
    if not profiles.muse_probe_unconfirmed(profile):
        return "이미 알려진 한도나 로그인 상태 때문에 핑을 보내지 않아요"
    if not await profiles.claim_muse_probe([profile]):
        return _muse_ping_wait_text(account_id)
    try:
        pinged = await _run_muse_ping(profile)
    except Exception as e:  # noqa: BLE001
        log.warning("muse observation ping failed: %s", type(e).__name__)
        return f"Muse 핑 실패({type(e).__name__}: {str(e)[:160]})"
    if isinstance(pinged, Usage) and pinged.windows:
        return pinged
    # 핑이 전달됐으면 공급자의 사용량 조회가 값을 줄 수 있다. 스트림에 관측이 없었어도 한 번 더 읽어 본다.
    reread = await _fetch_muse(config_dir)
    if reread.windows:
        return reread
    return pinged if isinstance(pinged, str) else "핑은 전달됐지만 공급자가 아직 구독 사용량을 주지 않았어요"


async def _run_muse_ping(profile: Any) -> Usage | str:
    """Run 과 같은 어댑터·보안 정책(도구 호출 가드 포함)으로 도구 없는 짧은 한 턴을 보낸다.

    스트림에서 사용량 관측을 받으면 그 Usage, 응답은 왔는데 관측이 없으면 창이 빈 Usage, 실패하면 사유 문장.
    답이 도착(turn/foregroundCompleted)하면 모델 호출은 끝난 것이라 남은 백그라운드 정리(수십 초)를 기다리지 않고 종료한다."""
    import shutil
    import tempfile

    from ttakkari.agents.base import LaunchSpec, ParseState
    from ttakkari.agents.muse import MuseAdapter
    cwd = Path(os.path.realpath(tempfile.mkdtemp(prefix="ttakkari-muse-ping-")))
    adapter = MuseAdapter()
    spec = LaunchSpec(cwd=cwd, prompt=MUSE_PING_PROMPT, engine_session_id=None, model=profile.model, effort=None,
                      outbox=cwd, write_roots=[], system_note=MUSE_PING_NOTE)
    proc = None
    state = ParseState()
    delivered = False

    async def pump(stream: asyncio.StreamReader) -> None:
        nonlocal delivered
        while line := await stream.readline():
            with contextlib.suppress(ValueError):
                obj = json.loads(line)
                if isinstance(obj, dict):
                    adapter.parse_line(obj, state, cwd)
                    if obj.get("method") in ("turn/foregroundCompleted", "turn/completed"):
                        delivered = True
                        if state.account_usage or obj.get("method") == "turn/completed":
                            return
                        # 정리 단계가 시작됐다. 사용량 알림이 곧 오는지 잠깐만 더 본다.
                        await asyncio.sleep(0.5)
                        return

    try:
        env = profile.env(os.environ.copy())
        cmd = adapter.build_command(spec, env)
        proc = await asyncio.create_subprocess_exec(*cmd, cwd=cwd, env=env, stdin=asyncio.subprocess.DEVNULL,
                                                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        try:
            await asyncio.wait_for(pump(proc.stdout), MUSE_PING_TIMEOUT)
        except TimeoutError:
            if not delivered:
                return f"Muse 핑이 {MUSE_PING_TIMEOUT:.0f}초 안에 답하지 않았어요"
        body = state.account_usage
        if body and body.get("windows") and body.get("updated_at"):
            return _validate_muse_window(Usage(**{**body, "windows": [Window(**w) for w in body["windows"]]}))
        if state.is_error or not delivered:
            return f"Muse 핑 실패({(state.error or '응답 없이 종료')[:160]})"
        return Usage(login="ok")
    finally:
        if proc is not None and proc.returncode is None:
            proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), 4)
            except TimeoutError:
                proc.kill()
                await proc.wait()
        adapter.cleanup(spec)
        shutil.rmtree(cwd, ignore_errors=True)


async def _fetch_muse_native(muse_bin: str, env: dict[str, str]) -> dict[str, Any]:
    """A fresh single-threaded helper owns the native terminal and its child."""
    proc = None
    try:
        proc = await asyncio.create_subprocess_exec(sys.executable, str(Path(__file__).with_name('muse_usage.py')),
            muse_bin, env=env, stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        stdout, _stderr = await asyncio.wait_for(proc.communicate(), 23)
        if proc.returncode != 0:
            return {'usage': {}, 'quota_status': f'helper_exit_{proc.returncode}'}
        body = json.loads(stdout)
        return body if isinstance(body, dict) else {'usage': {}, 'quota_status': 'invalid_response'}
    finally:
        if proc is not None and proc.returncode is None:
            proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), 4)
            except TimeoutError:
                proc.kill()
                await proc.wait()


def cached(account_id: uuid.UUID) -> Usage | None:
    """마지막으로 본 사용량(캐시·실행 스트림 중 더 최근 것). 네트워크도 인증 파일·키체인도 건드리지 않는다.

    TTL 이 지난 값도 준다(낡았는지는 updated_at 으로 알린다). 한 번도 못 봤으면 None."""
    hit = _cache.get(account_id)
    stream = _stream.get(account_id)
    # A completed failed refresh is newer evidence than an old stream snapshot. In
    # particular, do not resurrect expired Muse windows after _fetch_live clears them.
    if hit and not hit[1].windows and (not stream or hit[0] >= (stream.updated_at or 0)):
        return hit[1]
    if hit and stream and hit[0] >= (stream.updated_at or 0):
        have = {w.key for w in hit[1].windows}
        extra = [w for w in stream.windows if w.key not in have and not (w.resets_at and w.resets_at <= time.time())]
        if extra:
            return Usage(windows=_ordered(hit[1].windows + extra), source=hit[1].source, plan=hit[1].plan or stream.plan,
                         updated_at=hit[1].updated_at)
        return hit[1]
    fresh = [u for u in (hit[1] if hit else None, stream) if u is not None and u.windows]
    if fresh:
        best = max(fresh, key=lambda u: u.updated_at or 0)
        if best.source == "stream" and hit and hit[1].windows:
            # 스트림은 모델별 주간 창을 안 줄 때가 많다. 초기화 전인 것만 조회 결과에서 채운다(조금 더 오래된 값).
            have = {w.key for w in best.windows}
            extra = [w for w in hit[1].windows if w.key not in have and not (w.resets_at and w.resets_at <= time.time())]
            if extra:
                return Usage(windows=_ordered(best.windows + extra), source="stream", plan=best.plan or hit[1].plan,
                             updated_at=best.updated_at)
        return best
    return hit[1] if hit else None


def forget(account_id: uuid.UUID) -> None:
    _cache.pop(account_id, None)
    _stream.pop(account_id, None)


def clear_cache() -> None:
    _cache.clear()
    _stream.clear()


def exhausted_until(account_id: uuid.UUID) -> float | None:
    """마지막으로 본 사용량(네트워크 없이 캐시만)에서 5시간·주간 한도가 찬 계정의 초기화 시각(unix 초).

    모델별 주간 한도는 다른 모델을 쓸 수 있어 무시한다. 초기화 시각이 지났거나 모르면 None."""
    now = time.time()
    cached = _cache.get(account_id)
    until = 0.0
    for u in (cached[1] if cached else None, _stream.get(account_id)):
        for w in u.windows if u else []:
            if w.key in ("five_hour", "seven_day") and w.percent >= 100 and w.resets_at and w.resets_at > now:
                until = max(until, float(w.resets_at))
    return until or None
