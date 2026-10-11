"""엔진 계정(프로필) 우선순위와 사용량 한도 폴백(ADR 0006). 계정은 DB(engine_accounts)에 있고 설정 화면에서 고친다."""

from __future__ import annotations

import json
import re
import time
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from sqlalchemy import Text, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ttakkari.config import HOME, get_settings
from ttakkari.models import AppSetting, ChatSession, EngineAccount, utcnow

ENGINES = {"claude", "codex"}
# 엔진이 사용량·속도 한도로 거절할 때의 문구. 문구가 바뀌면 폴백 없이 그냥 실패한다(ADR 0006 한계).
USAGE_LIMIT_RE = re.compile(
    r"usage limit|hit your (usage )?limit|limit reached|rate.?limit|quota|too many requests|\b429\b|"
    r"credit balance|out of credits|overloaded",
    re.IGNORECASE,
)

_cooldown: dict[uuid.UUID, float] = {}


@dataclass(frozen=True)
class Profile:
    id: uuid.UUID
    name: str
    engine: str
    config_dir: Path | None = None

    @property
    def key(self) -> str:
        """sessions.engine_sessions 의 키. 이름을 바꿔도 대화가 이어지게 id 를 쓴다."""
        return str(self.id)

    def env(self) -> dict[str, str]:
        if self.config_dir is None:
            return {}
        return {"CLAUDE_CONFIG_DIR" if self.engine == "claude" else "CODEX_HOME": str(self.config_dir)}


def _profile(a: EngineAccount) -> Profile:
    return Profile(id=a.id, name=a.name, engine=a.engine, config_dir=Path(a.config_dir).expanduser() if a.config_dir else None)


async def ensure_seeded(db: AsyncSession) -> None:
    """계정이 하나도 없으면 설정(TTAKKARI_ENGINE_PROFILES)으로 만든다. 기존 대화의 엔진 이름 키를 계정 id 로 옮긴다."""
    if await db.scalar(select(func.count()).select_from(EngineAccount)):
        return
    by_name: dict[str, EngineAccount] = {}
    for i, raw in enumerate(get_settings().engine_profiles):
        engine = raw.get("engine", "")
        name = (raw.get("name") or engine)[:60]
        if engine not in ENGINES or name in by_name:
            continue
        acc = EngineAccount(name=name, engine=engine, config_dir=raw.get("config_dir"), enabled=True, priority=i)
        db.add(acc)
        by_name[name] = acc
    await db.flush()
    for sess in await db.scalars(select(ChatSession).where(ChatSession.engine_sessions.cast(Text) != "{}")):
        sess.engine_sessions = {(str(by_name[k].id) if k in by_name else k): v for k, v in sess.engine_sessions.items()}
    await db.commit()


async def accounts(db: AsyncSession) -> list[EngineAccount]:
    await ensure_seeded(db)
    return list(await db.scalars(select(EngineAccount).order_by(EngineAccount.priority, EngineAccount.created_at)))


POOLS = ("personal", "work")


def override_key(pool: str) -> str:
    return f"account_priority_override:{pool}"


async def get_override(db: AsyncSession, pool: str = "personal") -> dict | None:
    """임시 우선순위 {ids, until(ISO)}. 계정 묶음(개인·회사)마다 따로. 기한이 지났으면 None."""
    row = await db.get(AppSetting, override_key(pool))
    if row is None or not row.value.get("ids"):
        return None
    try:
        until = datetime.fromisoformat(row.value["until"])
    except (KeyError, ValueError):
        return None
    return row.value if until > utcnow() else None


async def effective_order(db: AsyncSession, pool: str = "personal") -> list[EngineAccount]:
    """그 묶음에서 지금 적용되는 순서. 임시 우선순위가 살아 있으면 그 순서가 먼저, 나머지는 기본 순서로 뒤에 붙는다."""
    rows = [a for a in await accounts(db) if a.pool == pool]
    ov = await get_override(db, pool)
    if not ov:
        return rows
    rank = {aid: i for i, aid in enumerate(ov["ids"])}
    return sorted(rows, key=lambda a: (rank.get(str(a.id), len(rank)), a.priority))


async def candidates(db: AsyncSession, session_engine: str, pool: str = "personal") -> list[Profile]:
    """세션 엔진에 맞는 켜진 계정을 우선순위대로(임시 우선순위 반영). 쉬는 계정은 뒤로 보낸다(전부 쉬면 순서대로 다시 시도)."""
    picked = [_profile(a) for a in await effective_order(db, pool)
              if a.enabled and (session_engine == "auto" or a.engine == session_engine)]
    now = time.monotonic()
    return [p for p in picked if _cooldown.get(p.id, 0) <= now] + [p for p in picked if _cooldown.get(p.id, 0) > now]


# Codex: "try again at Oct 12th, 2026 12:16 AM." (2026-10-11 실측). 시간대 표기가 없어 서버 현지 시각으로 읽는다.
RESET_AT_RE = re.compile(r"try again at ([A-Z][a-z]{2,8}) (\d{1,2})(?:st|nd|rd|th)?,? (\d{4}) (\d{1,2}):(\d{2}) ?([AP]M)", re.IGNORECASE)


def reset_seconds_from(*texts: str | None) -> int | None:
    """한도 메시지에서 초기화 시각을 읽어 남은 초로. 못 읽으면 None(기본 쉬는 시간을 쓴다)."""
    for t in texts:
        m = RESET_AT_RE.search(t or "")
        if not m:
            continue
        mon, day, year, hh, mm, ap = m.groups()
        try:
            # 메시지에 시간대가 없다. Codex 는 사용자 현지 시각으로 적는다(10-11 실측 문구가 KST 자정 근처) → 현지로 본다.
            when = datetime.strptime(f"{mon[:3]} {day} {year} {hh}:{mm} {ap.upper()}", "%b %d %Y %I:%M %p").astimezone()
        except ValueError:
            continue
        left = when.timestamp() - time.time()
        if 0 < left < 8 * 86400:
            return int(left) + 30
    return None


def cool_down(account_id: uuid.UUID, seconds: int | None = None) -> None:
    _cooldown[account_id] = time.monotonic() + (seconds or get_settings().profile_cooldown_seconds)


def cooldown_remaining(account_id: uuid.UUID) -> int:
    return max(0, round(_cooldown.get(account_id, 0) - time.monotonic()))


def clear_cooldown(account_id: uuid.UUID) -> None:
    _cooldown.pop(account_id, None)


def reset_cooldowns() -> None:
    _cooldown.clear()


def is_usage_limit(*texts: str | None) -> bool:
    return any(t and USAGE_LIMIT_RE.search(t) for t in texts)


def identity(engine: str, config_dir: str | None) -> tuple[str, str | None]:
    """계정 로그인 상태를 짐작한다. (상태, 표시 이름). 토큰은 읽지 않는다.

    claude: <config_dir>/.claude.json 의 oauthAccount.emailAddress(기본은 ~/.claude.json).
    codex: <config_dir 또는 ~/.codex>/auth.json 존재 여부만(키체인 저장이면 알 수 없다).
    """
    base = Path(config_dir).expanduser() if config_dir else None
    if engine == "claude":
        f = (base / ".claude.json") if base else HOME / ".claude.json"
        try:
            email = (json.loads(f.read_text()).get("oauthAccount") or {}).get("emailAddress")
        except (OSError, ValueError):
            return ("missing" if base and not base.exists() else "unknown"), None
        return ("ok", email) if email else ("unknown", None)
    home = base or HOME / ".codex"
    if not home.exists():
        return "missing", None
    return ("ok", None) if (home / "auth.json").exists() else ("unknown", None)


CLAUDE_MODELS = {"opus", "sonnet", "haiku", "fable"}


def model_engine(model: str | None) -> str | None:
    """모델 이름이 어느 엔진 것인지. 모르면 None(첫 계정에만 넘긴다)."""
    if not model:
        return None
    m = model.lower()
    if m in CLAUDE_MODELS or m.startswith("claude"):
        return "claude"
    if m.startswith(("gpt", "codex", "o1", "o3", "o4")):
        return "codex"
    return None

