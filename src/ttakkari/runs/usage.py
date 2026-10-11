"""계정별 사용량(5시간·주간·모델별 주간). ezwork fsw 의 ezChatUsage 와 같은 출처를 쓴다.

Claude: 구독 사용량 API(GET https://api.anthropic.com/api/oauth/usage, anthropic-beta: oauth-2025-04-20).
  토큰은 Claude Code 가 이미 둔 것을 읽기만 한다. macOS 키체인 서비스 이름은 기본 계정이
  `Claude Code-credentials`, CLAUDE_CONFIG_DIR 계정은 `Claude Code-credentials-<sha256(폴더)[:8]>`
  (2026-10-11 이 맥에서 등록 계정 3개로 대조). 다른 OS 는 `<폴더>/.credentials.json`.
  API 를 못 쓰면 따까리 실행 스트림의 `rate_limit_event` 로 받은 마지막 값을 쓴다.
Codex: `<CODEX_HOME>/sessions/**/*.jsonl` 의 마지막 `rate_limits`(primary=5시간, secondary=주간).

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
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx

from ttakkari.config import HOME

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
class Usage:
    windows: list[Window] = field(default_factory=list)
    source: str | None = None  # api | stream | session_log
    plan: str | None = None
    error: str | None = None
    updated_at: float | None = None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


_cache: dict[uuid.UUID, tuple[float, Usage]] = {}
_stream: dict[uuid.UUID, Usage] = {}


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
    if hit and not refresh and time.time() - hit[0] < TTL:
        return hit[1]
    if engine == "claude":
        u = await _fetch_claude(config_dir)
        if not u.windows and account_id in _stream:
            # API 를 못 쓰면 실행 스트림에서 받은 마지막 값으로 대신한다(오류 문구는 남긴다).
            s = _stream[account_id]
            u = Usage(windows=s.windows, source="stream", updated_at=s.updated_at, error=u.error)
    else:
        u = await asyncio.to_thread(codex_usage, config_dir)
    _cache[account_id] = (time.time(), u)
    return u


def clear_cache() -> None:
    _cache.clear()
    _stream.clear()
