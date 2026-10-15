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
import contextlib
import getpass
import hashlib
import json
import logging
import sys
import time
import uuid
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
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


@dataclass
class ResetCredits:
    """Codex rate-limit 재설정 크레딧. 한 번 쓰면 5시간·주간 한도가 바로 초기화된다."""

    available: int
    next_expires_at: int | None  # unix 초


@dataclass
class Usage:
    windows: list[Window] = field(default_factory=list)
    reset_credits: ResetCredits | None = None
    source: str | None = None  # api | stream | session_log
    plan: str | None = None
    error: str | None = None
    updated_at: float | None = None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


_cache: dict[uuid.UUID, tuple[float, Usage]] = {}
_stream: dict[uuid.UUID, Usage] = {}
# 새 측정값이 생길 때마다 부른다(runs/usage_store.py 가 DB 에 남긴다). 정하지 않으면 메모리에만 둔다.
_sink: Callable[[uuid.UUID, Usage], None] | None = None


def set_sink(fn: Callable[[uuid.UUID, Usage], None] | None) -> None:
    global _sink
    _sink = fn


def _emit(account_id: uuid.UUID, u: Usage) -> None:
    if _sink is not None and u.windows and u.source in ("api", "stream"):
        with contextlib.suppress(Exception):
            _sink(account_id, u)


def seed(account_id: uuid.UUID, u: Usage) -> None:
    """재시작 직후 DB 의 마지막 측정값으로 캐시를 채운다. 이미 값이 있으면 건드리지 않는다."""
    if account_id not in _cache and u.updated_at:
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


async def _claude_token(config_dir: str | None) -> str | None:
    raw: str | None = None
    if sys.platform == "darwin":
        proc = await asyncio.create_subprocess_exec(
            "/usr/bin/security", "find-generic-password", "-s", keychain_service(config_dir), "-a", getpass.getuser(), "-w",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL, stdin=asyncio.subprocess.DEVNULL)
        with contextlib.suppress(TimeoutError):
            out, _ = await asyncio.wait_for(proc.communicate(), TIMEOUT)
            raw = out.decode().strip() or None
    if raw is None:
        f = (Path(config_dir) if config_dir else HOME / ".claude") / ".credentials.json"
        with contextlib.suppress(OSError):
            raw = f.read_text()
    if not raw:
        return None
    try:
        tok = (json.loads(raw).get("claudeAiOauth") or {}).get("accessToken")
    except ValueError:
        return None
    return tok if isinstance(tok, str) and tok else None


async def _fetch_claude(config_dir: str | None) -> Usage:
    token = await _claude_token(config_dir)
    if not token:
        return Usage(error="로그인 토큰을 찾지 못했어요")
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT) as c:
            r = await c.get(USAGE_URL, headers={"Authorization": f"Bearer {token}", "anthropic-beta": "oauth-2025-04-20",
                                                "User-Agent": "ttakkari/0.1"})
        if r.status_code != 200:
            return Usage(error=f"사용량 API {r.status_code}")
        return Usage(windows=windows_from_api(r.json()), source="api", updated_at=time.time())
    except (httpx.HTTPError, ValueError) as e:
        return Usage(error=f"사용량 API 실패({type(e).__name__})")


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


async def _codex_get(config_dir: str | None, path: str) -> dict[str, Any] | None:
    h = _codex_headers(config_dir)
    if h is None:
        return None
    async with httpx.AsyncClient(timeout=TIMEOUT) as c:
        r = await c.get(f"{CODEX_API}/{path}", headers=h)
    return r.json() if r.status_code == 200 else None


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


async def _fetch_codex(config_dir: str | None) -> Usage | None:
    try:
        body = await _codex_get(config_dir, "usage")
        if not body or not isinstance(body.get("plan_type"), str):
            return None
        credits = credits_from(body, None)
        if credits and credits.available > 0:
            # 사용량 응답엔 개수만 온다. 다음 만료 시각은 크레딧 목록에서 읽는다.
            credits = credits_from(body, await _codex_get(config_dir, "rate-limit-reset-credits")) or credits
        return Usage(windows=windows_from_codex(body), source="api", plan=body["plan_type"], reset_credits=credits,
                     updated_at=time.time())
    except (httpx.HTTPError, ValueError):
        return None


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
            for part in ("primary", "secondary"):
                w = last.get(part)
                if not isinstance(w, dict) or not isinstance(w.get("used_percent"), (int, float)):
                    continue
                minutes = w.get("window_minutes") or 0
                key = "five_hour" if minutes and minutes <= 600 else "seven_day"
                ws.append(Window(key, label_of(key), round(w["used_percent"]), _ts(w.get("resets_at"))))
            return Usage(windows=_ordered(ws), source="session_log", plan=last.get("plan_type"),
                         updated_at=f.stat().st_mtime)
    return Usage(error="아직 따까리에서 쓴 기록이 없어요")


# ---- 묶기

async def usage_for(account_id: uuid.UUID, engine: str, config_dir: str | None, *, refresh: bool = False) -> Usage:
    hit = _cache.get(account_id)
    # 수집기가 주기마다 새로 읽어 두므로(runs/usage_collector.py) 화면이 그보다 자주 API 를 두드리지 않게 한다.
    ttl = max(TTL, get_settings().usage_poll_seconds)
    if hit and not refresh and time.time() - hit[0] < ttl:
        return hit[1]
    if engine == "antigravity":
        # No documented usage API in agy; unknown is different from zero usage or unlimited headroom.
        return Usage(error="Antigravity usage is not observable through the official CLI")
    if engine == "claude":
        u = await _fetch_claude(config_dir)
        if not u.windows and account_id in _stream:
            # API 를 못 쓰면 실행 스트림에서 받은 마지막 값으로 대신한다(오류 문구는 남긴다).
            s = _stream[account_id]
            u = Usage(windows=s.windows, source="stream", updated_at=s.updated_at, error=u.error)
    else:
        u = await _fetch_codex(config_dir) or await asyncio.to_thread(codex_usage, config_dir)
    _cache[account_id] = (time.time(), u)
    if u.source == "api":  # 스트림 값으로 대신한 경우는 remember_stream 이 이미 남겼다.
        _emit(account_id, u)
    return u


def cached(account_id: uuid.UUID) -> Usage | None:
    """마지막으로 본 사용량(캐시·실행 스트림 중 더 최근 것). 네트워크도 인증 파일·키체인도 건드리지 않는다.

    TTL 이 지난 값도 준다(낡았는지는 updated_at 으로 알린다). 한 번도 못 봤으면 None."""
    hit = _cache.get(account_id)
    fresh = [u for u in (hit[1] if hit else None, _stream.get(account_id)) if u is not None and u.windows]
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
