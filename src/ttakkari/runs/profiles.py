"""엔진 계정(프로필) 우선순위와 사용량 한도 폴백(ADR 0006). 계정은 DB(engine_accounts)에 있고 설정 화면에서 고친다."""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
import uuid
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import Text, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from ttakkari.config import HOME, get_settings
from ttakkari.db import sessionmaker
from ttakkari.model_catalog import (
    ANTIGRAVITY_MODELS,
    ANTIGRAVITY_QUOTA_GROUPS,
    quota_group,
)
from ttakkari.models import AppSetting, ChatSession, EngineAccount, utcnow

log = logging.getLogger(__name__)

ENGINES = {"claude", "codex", "antigravity", "muse", "opencode", "copilot"}
# 엔진이 사용량·속도 한도로 거절할 때의 문구. 문구가 바뀌면 폴백 없이 그냥 실패한다(ADR 0006 한계).
# Claude: "You've hit your session|weekly|usage limit · resets 7:20pm (Asia/Seoul)", "Claude AI usage limit reached|<epoch>".
# Codex: "You've hit your usage limit. ... try again at Oct 12th, 2026 12:16 AM."
USAGE_LIMIT_RE = re.compile(
    r"usage limit|hit your (\w+ )?limit|reached your (\w+ )?limit|limit reached|rate.?limit|quota|too many requests|"
    r"\b429\b|credit balance|out of (extra )?(credits|usage)|overloaded",
    re.IGNORECASE,
)
PERMISSION_ERROR_RE = re.compile(
    r"permission denied|operation not permitted|access denied|authorization failed|forbidden|\[ttakkari-guard\]",
    re.IGNORECASE,
)

# 한도 걸린 계정이 쉬는 시각(unix 초). 재시작해도 초기화 전에 다시 두드리지 않게 app_settings 에도 적는다.
_cooldown: dict[uuid.UUID, float] = {}
_cooldown_changes: dict[uuid.UUID, float | None] = {}
COOLDOWN_KEY = "account_limits"


@dataclass(frozen=True)
class Profile:
    id: uuid.UUID
    name: str
    engine: str
    config_dir: Path | None = None
    max_used_percent: int = 100
    model: str | None = None
    pool: str = "personal"

    @property
    def limit_key(self) -> uuid.UUID:
        """Share provider-denied cooldowns only within the relevant quota group."""
        group = quota_group(self.model)
        return uuid.uuid5(self.id, group) if self.engine == "antigravity" and group else self.id

    @property
    def key(self) -> str:
        """sessions.engine_sessions 의 키. 이름을 바꿔도 대화가 이어지게 id 를 쓴다."""
        return str(self.id)

    def env(self, base_env: dict[str, str] | None = None) -> dict[str, str]:
        base_env = base_env or {}
        if self.engine == "muse":
            settings = get_settings()
            root = self.config_dir or settings.muse_profile_root or settings.home / "accounts/muse-default"
            return base_env | {f"XDG_{kind.upper()}_HOME": str(root / kind) for kind in ("config", "data", "cache")}
        if self.engine == "opencode":
            if self.config_dir:
                from ttakkari import opencode_accounts as managed_oc
                return managed_oc.execution_env(self.config_dir, base_env)
            root = get_settings().home / "accounts" / str(self.id) / "opencode"
            return base_env | {f"XDG_{kind.upper()}_HOME": str(root / kind) for kind in ("config", "data", "state", "cache")}
        if self.engine == "copilot":
            from ttakkari import copilot_accounts as managed_cp
            if self.config_dir:
                return managed_cp.execution_env(self.config_dir, base_env, is_system=False)
            return managed_cp.execution_env(None, base_env, is_system=True)
        if self.config_dir is None:
            return base_env.copy()
        if self.engine == "antigravity":
            from ttakkari import antigravity_accounts as managed
            try:
                p = managed.load(self.config_dir)
            except ValueError as exc:
                raise ValueError("Antigravity isolation: invalid_profile") from exc
            if p.account_id != self.id:
                raise ValueError("Antigravity isolation: account_mismatch")
            return managed.execution_env(p.home, base_env)
        return base_env | {"CLAUDE_CONFIG_DIR" if self.engine == "claude" else "CODEX_HOME": str(self.config_dir)}


def execution_ready(profile: Profile) -> bool:
    if profile.engine == "copilot":
        from ttakkari import copilot_accounts as managed_cp
        if profile.config_dir is None:
            return managed_cp.is_ready(None, is_system=True)
        try:
            return managed_cp.load(profile.config_dir).account_id == profile.id and managed_cp.is_ready(profile.config_dir)
        except (OSError, ValueError):
            return False
    if profile.engine == "opencode":
        if profile.config_dir is None:
            return True
        from ttakkari import opencode_accounts as managed_oc
        try:
            return managed_oc.load(profile.config_dir).account_id == profile.id and managed_oc.is_ready(profile.config_dir)
        except (OSError, ValueError):
            return False
    if profile.engine != "antigravity" or profile.config_dir is None:
        return True
    from ttakkari import antigravity_accounts as managed
    try:
        return managed.load(profile.config_dir).account_id == profile.id and managed.is_ready(profile.config_dir)
    except (OSError, ValueError):
        return False


def _profile(a: EngineAccount) -> Profile:
    return Profile(id=a.id, name=a.name, engine=a.engine, config_dir=Path(a.config_dir).expanduser() if a.config_dir else None,
                   max_used_percent=a.max_used_percent, pool=a.pool)


async def ensure_seeded(db: AsyncSession) -> None:
    """계정이 하나도 없으면 설정(TTAKKARI_ENGINE_PROFILES)으로 만든다. 기존 대화의 엔진 이름 키를 계정 id 로 옮긴다."""
    if await db.scalar(select(func.count()).select_from(EngineAccount)):
        return
    # 서로 다른 Run·API 요청이 빈 목록을 함께 읽어도 기본 계정과 세션 키 이전은 한 트랜잭션만 한다.
    await db.execute(select(func.pg_advisory_xact_lock(0x54544B, 1)))
    if await db.scalar(select(func.count()).select_from(EngineAccount)):
        return
    by_name: dict[str, EngineAccount] = {}
    for i, raw in enumerate(get_settings().engine_profiles):
        engine = raw.get("engine", "")
        name = (raw.get("name") or engine)[:60]
        if engine not in ENGINES or name in by_name:
            continue
        # 시드 시 지정된 보호 계정만 95%로 시작한다. 이후 값은 계정 DB와 비서관리에서 관리한다.
        cap = 95 if (engine, name.casefold()) in {("claude", "내 클로드"), ("codex", "daddy codex")} else 100
        acc = EngineAccount(name=name, engine=engine, config_dir=raw.get("config_dir"), enabled=True, priority=i,
                            max_used_percent=cap)
        db.add(acc)
        by_name[name] = acc
    await db.flush()
    for sess in await db.scalars(select(ChatSession).where(ChatSession.engine_sessions.cast(Text) != "{}")):
        sess.engine_sessions = {(str(by_name[k].id) if k in by_name else k): v for k, v in sess.engine_sessions.items()}
    await db.commit()


async def accounts(db: AsyncSession) -> list[EngineAccount]:
    await ensure_seeded(db)
    return list(await db.scalars(select(EngineAccount).order_by(EngineAccount.priority, EngineAccount.created_at)))


OVERRIDE_KEY = "account_priority_override"
WORK_OVERRIDE_KEY = "account_priority_override:work"


def override_key(pool: str) -> str:
    return WORK_OVERRIDE_KEY if pool == "work" else OVERRIDE_KEY


def is_on(a: EngineAccount, pool: str = "personal") -> bool:
    """그 모드에서 켜져 있는지. 개인 모드는 enabled, 회사 모드는 회사에 들어온 계정의 work_enabled 로 따로 센다."""
    return (a.pool == "work" and a.work_enabled) if pool == "work" else a.enabled


async def get_override(db: AsyncSession, pool: str = "personal") -> dict | None:
    """임시 우선순위 {ids, until(ISO)}. 개인·회사 모드가 따로 가진다. 기한이 지났으면 None."""
    row = await db.get(AppSetting, override_key(pool))
    if row is None or not row.value.get("ids"):
        return None
    try:
        until = datetime.fromisoformat(row.value["until"])
    except (KeyError, ValueError):
        return None
    return row.value if until > utcnow() else None


async def effective_order(db: AsyncSession, pool: str = "personal") -> list[EngineAccount]:
    """그 모드에서 지금 적용되는 순서. 임시 우선순위가 살아 있으면 그 순서가 먼저, 나머지는 기본 순서로 뒤에 붙는다.

    personal 은 모든 계정을 priority 순으로, work 는 회사에 들어온 계정만 work_priority 순으로 본다.
    """
    rows = await accounts(db)
    if pool == "work":
        rows = sorted((a for a in rows if a.pool == "work"), key=lambda a: (a.work_priority, a.priority))
    ov = await get_override(db, pool)
    if ov:
        rank = {aid: i for i, aid in enumerate(ov["ids"])}
        base = {a.id: i for i, a in enumerate(rows)}
        rows = sorted(rows, key=lambda a: (rank.get(str(a.id), len(rank)), base[a.id]))
    return rows


async def availability(db: AsyncSession, session_engine: str, pool: str = "personal", *,
                       model: str | None = None, include_muse: bool = False,
                       ) -> tuple[list[Profile], list[tuple[Profile, float]]]:
    """세션 엔진에 맞는 켜진 계정을 우선순위대로(임시 우선순위 반영). (쓸 수 있는 계정, [(한도 대기 중인 계정, 초기화 시각)]).

    auto 는 기본 claude·codex 만 본다. include_muse 면 켜진 Muse 계정도 후보에 넣는다(오케스트레이터 전용)."""
    from ttakkari import generation
    if generation.current:
        from ttakkari.runs import usage_store
        await load_cooldowns(db)
        await usage_store.load_latest(db, refresh=True)
    free: list[Profile] = []
    blocked: list[tuple[Profile, float]] = []
    muse_allowed = include_muse and get_settings().muse_enabled
    for a in await effective_order(db, pool):
        if not is_on(a, pool) or not (
            (session_engine == "auto" and (a.engine in {"claude", "codex"}
                                           or (muse_allowed and a.engine == "muse")))
            or a.engine == session_engine
        ):
            continue
        p = _profile(a)
        if not execution_ready(p):
            continue
        if p.engine in {"antigravity", "muse", "opencode", "copilot"}:
            p = replace(p, model=model or get_settings().worker_default_models[p.engine]["model"])
        if p.engine == "antigravity":
            from ttakkari.runs import agy_models
            await agy_models.ensure_known(p)
            if reason := agy_models.unsupported_reason(p):
                agy_models.note_excluded(p, reason)
                continue
        until = profile_limited_until(p)
        if until:
            blocked.append((p, until))
        else:
            free.append(p)
    return free, blocked


def usage_limit_until(profile: Profile, now: float | None = None) -> float | None:
    """마지막 사용량이 상한에 닿았으면 값이 낡아도 해당 창이 초기화될 때까지 쉰다."""
    from ttakkari.runs import usage as usage_mod

    now = time.time() if now is None else now
    u = usage_mod.cached(profile.id)
    # 측정 실패·지연은 한도 회복의 증거가 아니다. 배정 순위와 달리 상한 차단에는
    # stale 값도 쓴다. 새 측정·창 초기화·사용자가 상한을 올린 경우에만 풀린다.
    if u is None or not u.windows:
        return None
    group = quota_group(profile.model) if profile.engine == "antigravity" else None
    if profile.engine == "antigravity":
        capped = [w for w in u.windows if group and w.key in {f"{group}_5h", f"{group}_weekly"}
                  and not (w.resets_at and w.resets_at <= now)
                  and (w.remaining_percent is not None and w.remaining_percent <= 0)]
    else:
        capped = [w for w in u.windows if w.key in ("five_hour", "seven_day")
                  and not (w.resets_at and w.resets_at <= now) and w.percent >= profile.max_used_percent]
    if not capped:
        return None
    # 초기화 시각이 누락된 측정은 다음 짧은 수집 주기에서 해제 여부를 다시 판단한다.
    return max((float(w.resets_at) for w in capped if w.resets_at and w.resets_at > now), default=now + 90)


def limited_until(account_id: uuid.UUID, max_used_percent: int = 100) -> float | None:
    """한도로 쉬는 계정의 초기화 시각(unix 초). 쓸 수 있으면 None. 네트워크 없이 메모리만 본다.

    실행 오류로 건 cooldown 과 마지막 5시간·주간 사용량이 계정 상한에 닿은 경우를 함께 본다."""
    profile = Profile(id=account_id, name="", engine="", max_used_percent=max_used_percent)
    until = max(_cooldown.get(account_id, 0), usage_limit_until(profile) or 0)
    return until if until > time.time() else None


def profile_limited_until(profile: Profile) -> float | None:
    until = max(_cooldown.get(profile.id, 0), _cooldown.get(profile.limit_key, 0),
                usage_limit_until(profile) or 0)
    return until if until > time.time() else None


# 지금 Run 이 쓰고 있는 계정(run id → 계정 id). 매니저와 같은 프로세스만 바꾸므로 DB 에 두지 않는다
# (재시작하면 Run 도 함께 끝난다). 한도 대기 중인 Run 은 계정을 쓰지 않으니 넣지 않는다.
_in_use: dict[uuid.UUID, uuid.UUID] = {}


def release(run_id: uuid.UUID) -> None:
    _in_use.pop(run_id, None)


def running_counts() -> dict[uuid.UUID, int]:
    """계정 id → 그 계정으로 지금 돌고 있는 Run 수."""
    counts: dict[uuid.UUID, int] = {}
    for aid in _in_use.values():
        counts[aid] = counts.get(aid, 0) + 1
    return counts


async def running_counts_async() -> dict[uuid.UUID, int]:
    from ttakkari import generation
    if generation.current is None:
        return running_counts()
    from ttakkari.models import RuntimeLease
    async with sessionmaker()() as db:
        return dict((await db.execute(select(RuntimeLease.account_id, func.count()).where(
            RuntimeLease.account_id.is_not(None)).group_by(RuntimeLease.account_id))).all())


def _usage_headroom(profile: Profile, now: float | None = None) -> tuple[float, float] | None:
    """상한까지 남은 최소 퍼센트와 그 창의 초기화 시각. stale/null 사용량은 순위 판단에 쓰지 않는다."""
    from ttakkari.runs import usage as usage_mod

    now = time.time() if now is None else now
    u = usage_mod.cached(profile.id)
    if u is None or not u.windows or u.updated_at is None or now - u.updated_at > get_settings().usage_stale_seconds:
        return None
    if profile.engine == 'muse' and not usage_mod.muse_quota_current(u, now):
        return None
    # 조회 실패 뒤 유지한 마지막 관측값은 현재 잔여량이 아니다(차단 판단은 usage_limit_until 이 따로 유지한다).
    if profile.engine == "antigravity" and u.error:
        return None
    group = quota_group(profile.model) if profile.engine == "antigravity" else None
    keys = {f"{group}_5h", f"{group}_weekly"} if group else {"five_hour", "seven_day"}
    current = [w for w in u.windows if w.key in keys
               and not (w.resets_at and w.resets_at <= now)]
    if profile.engine == "antigravity":
        current = [w for w in current if group and w.remaining_percent is not None]
    if not current:
        return None
    def room(w):
        return max(0, w.remaining_percent) if group else max(0, profile.max_used_percent - w.percent)
    minimum = min(room(w) for w in current)
    limiting = [w for w in current if room(w) == minimum]
    reset = min((float(w.resets_at) for w in limiting if w.resets_at), default=float("inf"))
    return minimum, reset


# Muse 는 5시간 창이 초기화된 뒤 쓰지 않으면 공급자가 /usage 창을 생략한다(unavailable). 확정 잔여량이 없는
# 후보는 배정에서 밀리므로, 호출이 없으면 새 관측도 없는 교착이 생긴다. 낡은 값을 잔여량으로 쓰지 않는 대신
# 알 수 없는 Muse 에 한해 이 간격마다 한 번만 실제 작업을 맡겨 스트림 관측이 생기게 한다(탐색 배정).
# 예약 시각은 app_settings 한 줄(계정별)에 조건부 UPSERT 로 남긴다. 세대(프로세스)가 여럿이어도 간격당 한 번만
# 성공하고, 재시작해도 유지된다. _probe_last 는 마지막으로 안 예약 시각의 메모리 사본일 뿐이며 판정 권한은 DB 에 있다.
MUSE_PROBE_INTERVAL = 30 * 60.0
MUSE_PROBE_KEY = "muse_probe:{}"  # app_settings.key 는 60자 이하
_probe_last: dict[uuid.UUID, float] = {}


def reset_probes() -> None:
    _probe_last.clear()


def muse_probe_unconfirmed(profile: Profile, now: float | None = None) -> bool:
    """잔여량을 확정하지 못한 Muse 인가(간격과 무관). 알려진 한도 소진·쿨다운·로그인 없음은 제외한다."""
    from ttakkari.runs import usage as usage_mod

    if profile.engine != "muse":
        return False
    now = time.time() if now is None else now
    if _usage_headroom(profile, now) is not None or profile_limited_until(profile):
        return False
    u = usage_mod.cached(profile.id)
    return not (u is not None and u.login == "missing")


def muse_probe_eligible(profile: Profile, now: float | None = None) -> bool:
    """탐색 배정을 시도해 볼 만한가(메모리 사본 기준의 참고 판정). 실제 예약은 claim_muse_probe 가 DB 에서 정한다."""
    now = time.time() if now is None else now
    return muse_probe_unconfirmed(profile, now) and now - _probe_last.get(profile.id, 0.0) >= MUSE_PROBE_INTERVAL


_PROBE_CLAIM_SQL = text(
    "INSERT INTO app_settings (key, value, updated_at) VALUES (:key, CAST(:value AS jsonb), now()) "
    "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now() "
    "WHERE COALESCE((app_settings.value->>'at')::double precision, 0) <= :threshold "
    "RETURNING key")


async def claim_muse_probe(candidates: list[Profile], now: float | None = None) -> frozenset[uuid.UUID]:
    """후보 중 탐색 배정을 받을 Muse 계정 하나를 DB 에서 원자적으로 예약한다(없으면 빈 집합).

    조건부 UPSERT 한 문장이라 동시에 여러 세대·프로세스가 불러도 간격당 하나만 이긴다. 졌으면 DB 의 시각을
    메모리 사본에 반영한다. DB 오류에서는 탐색하지 않는다(탐색은 선택 사항이다)."""
    now = time.time() if now is None else now
    for p in candidates:
        if not muse_probe_eligible(p, now):
            continue
        key = MUSE_PROBE_KEY.format(p.id)
        try:
            async with sessionmaker()() as db:
                won = (await db.execute(_PROBE_CLAIM_SQL, {
                    "key": key, "value": json.dumps({"at": now}), "threshold": now - MUSE_PROBE_INTERVAL,
                })).first() is not None
                if not won:
                    row = await db.get(AppSetting, key)
                    at = (row.value or {}).get("at") if row else None
                    if isinstance(at, (int, float)):
                        _probe_last[p.id] = float(at)
                await db.commit()
        except Exception:
            log.warning("muse exploration claim failed", exc_info=True)
            continue
        if won:
            _probe_last[p.id] = now
            log.info("muse exploration assignment reserved: account=%s (quota unconfirmed; next in %ds)",
                     p.name, MUSE_PROBE_INTERVAL)
            return frozenset({p.id})
    return frozenset()


def choose(pool: list[Profile], run_id: uuid.UUID, *, idle_first: bool = True, prefer_engine: str | None = None,
           balance_usage: bool = False, counts_override: dict[uuid.UUID, int] | None = None,
           probe_ids: frozenset[uuid.UUID] | set[uuid.UUID] = frozenset()) -> Profile:
    """pool(우선순위 순, 비어 있지 않음)에서 이 Run 이 쓸 계정을 고르고 바로 쓰는 중으로 적는다.

    balance_usage이면 (probe_ids 로 예약된 Muse 의 탐색 배정) > 신선한 측정이 있는 후보 > Run 당 상한 여유 > 초기화 시각 > 모델의 엔진 선호 > 유휴 > 우선순위.
    Run 당 여유 = 상한 여유 / (다른 실행 수 + 1). 실제 소모량 예측이 아닌 배정 점수다.
    stale/null 후보끼리는 모델·유휴·우선순위로 고른다. 고정 엔진·회사 범위는 호출자가 pool로 제한한다.
    고르는 일과 적는 일 사이에 await 가 없다."""
    counts: dict[uuid.UUID, int] = {}
    for rid, aid in _in_use.items():
        if rid != run_id:
            counts[aid] = counts.get(aid, 0) + 1
    if counts_override is not None:
        counts = counts_override
    headroom = {(p.id, p.model): _usage_headroom(p) for p in pool} if balance_usage else {}
    # probe_ids: claim_muse_probe 가 DB 에서 예약에 성공한 Muse 계정. 예약 없이는 탐색하지 않는다.
    probes = {(p.id, p.model) for p in pool
              if balance_usage and p.engine == "muse" and p.id in probe_ids and headroom.get((p.id, p.model)) is None}
    def rank(ip: tuple[int, Profile]) -> tuple:
        index, profile = ip
        room = headroom.get((profile.id, profile.model))
        return (
            (profile.id, profile.model) not in probes,
            balance_usage and room is None,
            -room[0] / (counts.get(profile.id, 0) + 1) if room is not None else 0,
            room[1] if room is not None else 0,
            prefer_engine is not None and profile.engine != prefer_engine,
            idle_first and counts.get(profile.id, 0) > 0,
            index,
        )

    ranked = sorted(enumerate(pool), key=rank)
    prof = ranked[0][1]
    room = headroom.get((prof.id, prof.model))
    if (prof.id, prof.model) in probes:
        log.info("run %s muse exploration assignment: account=%s", run_id, prof.name)
    log.info("run %s account selection: account=%s balance_usage=%s usage_ranked=%s headroom=%s "
             "other_running=%s headroom_per_run=%s",
             run_id, prof.name, balance_usage, room is not None, room, counts.get(prof.id, 0),
             room[0] / (counts.get(prof.id, 0) + 1) if room is not None else None)
    _in_use[run_id] = prof.id
    return prof


async def choose_async(pool: list[Profile], run_id: uuid.UUID, **kwargs) -> Profile:
    from ttakkari import generation
    if generation.current:
        return await generation.current.choose_profile(pool, run_id, **kwargs)
    return choose(pool, run_id, **kwargs)


async def candidates(db: AsyncSession, session_engine: str, pool: str = "personal") -> list[Profile]:
    """쓸 수 있는 계정만(한도 대기 중인 계정은 제외)."""
    return (await availability(db, session_engine, pool))[0]


# Codex: "try again at Oct 12th, 2026 12:16 AM." (2026-10-11 실측). 시간대 표기가 없어 서버 현지 시각으로 읽는다.
# Claude: "resets 7:20pm (Asia/Seoul)", "resets 3pm (Asia/Seoul)", "resets Oct 14, 3pm (...)", "resets Mon 3pm (...)".
SESSION_RESET_RE = re.compile(
    r"resets?\s+(?:(?P<mon>[A-Z][a-z]{2,8})\s+(?P<day>\d{1,2}),?\s+|(?P<wd>Mon|Tue|Wed|Thu|Fri|Sat|Sun)[a-z]*\s+)?"
    r"(?P<h>\d{1,2})(?::(?P<m>\d{2}))?\s*(?P<ap>[ap]m)\s*(?:\((?P<tz>[^)]+)\))?",
    re.IGNORECASE,
)
RESET_AT_RE = re.compile(r"try again at ([A-Z][a-z]{2,8}) (\d{1,2})(?:st|nd|rd|th)?,? (\d{4}) (\d{1,2}):(\d{2}) ?([AP]M)", re.IGNORECASE)
# Antigravity/기타: "Resets in 4h21m", "resets in 45m", "resets in 2 hours"
DURATION_RESET_RE = re.compile(
    r"resets?\s+in\s+(?:(?P<h>\d+)\s*h(?:ours?)?)?\s*(?:(?P<m>\d+)\s*m(?:in(?:ute)?s?)?)?\s*(?:(?P<s>\d+)\s*s(?:ec(?:ond)?s?)?)?",
    re.IGNORECASE,
)
# 옛 Claude CLI: "Claude AI usage limit reached|1760000000"
EPOCH_RESET_RE = re.compile(r"limit reached\|(\d{9,11})")
MAX_RESET_SECONDS = 8 * 86400
RESET_BUFFER = 30
MONTHS = ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"]
WEEKDAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


def _clock_reset(m: re.Match[str]) -> float | None:
    """Claude 의 "resets ..." 문구에서 초기화 시각(unix 초)."""
    try:
        zone = ZoneInfo(m["tz"]) if m["tz"] else datetime.now().astimezone().tzinfo
        now = datetime.now(zone)
        hour = int(m["h"]) % 12 + (12 if m["ap"].lower() == "pm" else 0)
        when = now.replace(hour=hour, minute=int(m["m"] or 0), second=0, microsecond=0)
        if m["mon"]:
            when = when.replace(month=MONTHS.index(m["mon"][:3].lower()) + 1, day=int(m["day"]))
            if when <= now:
                when = when.replace(year=when.year + 1)
        elif m["wd"]:
            when += timedelta(days=(WEEKDAYS.index(m["wd"][:3].lower()) - now.weekday()) % 7)
            if when <= now:
                when += timedelta(days=7)
        elif when <= now:
            when += timedelta(days=1)
        return when.timestamp()
    except (ValueError, ZoneInfoNotFoundError):
        return None


def reset_epoch_from(*texts: str | None) -> float | None:
    """한도 메시지에서 초기화 시각(unix 초). 못 읽으면 None(기본 쉬는 시간을 쓴다)."""
    now = time.time()
    for t in texts:
        if not t:
            continue
        found: float | None = None
        if m := EPOCH_RESET_RE.search(t):
            found = float(m[1])
        elif m := DURATION_RESET_RE.search(t):
            h = int(m.group("h") or 0)
            mn = int(m.group("m") or 0)
            s = int(m.group("s") or 0)
            dur = h * 3600 + mn * 60 + s
            if dur > 0:
                found = now + dur
        elif m := SESSION_RESET_RE.search(t):
            found = _clock_reset(m)
        elif m := RESET_AT_RE.search(t):
            mon, day, year, hh, mm, ap = m.groups()
            try:
                # 메시지에 시간대가 없다. Codex 는 사용자 현지 시각으로 적는다(10-11 실측 문구가 KST 자정 근처) → 현지로 본다.
                found = datetime.strptime(f"{mon[:3]} {day} {year} {hh}:{mm} {ap.upper()}", "%b %d %Y %I:%M %p").astimezone().timestamp()
            except ValueError:
                found = None
        if found is not None and 0 < found - now < MAX_RESET_SECONDS:
            return found
    return None


def reset_text_from(*texts: str | None) -> str | None:
    """한도 메시지에서 CLI가 알려준 초기화 문구(예: 'Resets in 4h21m')를 뽑는다."""
    for t in texts:
        if not t:
            continue
        if m := DURATION_RESET_RE.search(t):
            matched = m.group(0).strip()
            if matched:
                return matched
        if m := SESSION_RESET_RE.search(t):
            return m.group(0).strip()
        if m := RESET_AT_RE.search(t):
            return m.group(0).strip()
    return None


def reset_seconds_from(*texts: str | None) -> int | None:
    """한도 메시지에서 초기화까지 남은 초(여유 포함). 못 읽으면 None."""
    at = reset_epoch_from(*texts)
    return None if at is None else int(at - time.time()) + RESET_BUFFER


def cool_down(account_id: uuid.UUID, seconds: int | None = None) -> None:
    _cooldown[account_id] = time.time() + (seconds or get_settings().profile_cooldown_seconds)
    _cooldown_changes[account_id] = _cooldown[account_id]
    _persist()


def cooldown_until(account_id: uuid.UUID) -> float | None:
    until = _cooldown.get(account_id, 0)
    return until if until > time.time() else None


def cooldown_remaining(account_id: uuid.UUID) -> int:
    return max(0, round(_cooldown.get(account_id, 0) - time.time()))


def clear_cooldown(account_id: uuid.UUID) -> None:
    for key in (account_id, *(uuid.uuid5(account_id, group) for group in ANTIGRAVITY_QUOTA_GROUPS)):
        if key == account_id or key in _cooldown:
            _cooldown.pop(key, None)
            _cooldown_changes[key] = None
    _persist()


def reset_cooldowns() -> None:
    _probe_last.clear()
    _cooldown.clear()
    _cooldown_changes.clear()


_persist_lock: asyncio.Lock | None = None
_persist_tasks: set[asyncio.Task] = set()


def _persist() -> None:
    try:
        task = asyncio.get_running_loop().create_task(_save_cooldowns())
    except RuntimeError:
        return
    _persist_tasks.add(task)
    task.add_done_callback(_persist_tasks.discard)


async def _save_cooldowns() -> None:
    global _persist_lock
    if _persist_lock is None:
        _persist_lock = asyncio.Lock()
    async with _persist_lock:
        from ttakkari import generation
        now = time.time()
        changes = dict(_cooldown_changes)
        snapshot = {str(k): v for k, v in _cooldown.items() if v > now}
        try:
            async with sessionmaker()() as db:
                if generation.current:
                    await db.execute(select(func.pg_advisory_xact_lock(0x54544B, 2)))
                row = await db.get(AppSetting, COOLDOWN_KEY)
                if generation.current:
                    snapshot = {k: v for k, v in ((row.value if row else {}).get("until") or {}).items() if v > now}
                    for aid, value in changes.items():
                        if value and value > now:
                            snapshot[str(aid)] = value
                        else:
                            snapshot.pop(str(aid), None)
                if row is None:
                    db.add(AppSetting(key=COOLDOWN_KEY, value={"until": snapshot}))
                else:
                    row.value = {"until": snapshot}
                await db.commit()
                for aid, value in changes.items():
                    if _cooldown_changes.get(aid) == value:
                        _cooldown_changes.pop(aid, None)
        except Exception:
            log.warning("account cooldown persist failed", exc_info=True)


async def flush_persist() -> None:
    for task in list(_persist_tasks):
        await asyncio.gather(task, return_exceptions=True)


async def load_cooldowns(db: AsyncSession) -> int:
    """재시작 뒤 한도 상태를 되살린다. 이미 지난 시각은 버린다."""
    row = await db.get(AppSetting, COOLDOWN_KEY)
    from ttakkari import generation
    if generation.current:
        _cooldown.clear()
    now = time.time()
    n = 0
    for raw, until in ((row.value if row else {}).get("until") or {}).items():
        try:
            aid = uuid.UUID(raw)
        except ValueError:
            continue
        if isinstance(until, (int, float)) and until > now and until > _cooldown.get(aid, 0):
            _cooldown[aid] = float(until)
            n += 1
    if generation.current:
        for aid, value in _cooldown_changes.items():
            if value:
                _cooldown[aid] = value
            else:
                _cooldown.pop(aid, None)
    return n


def is_usage_limit(*texts: str | None) -> bool:
    # File/tool names can contain 'quota' or '429'. An ordinary permission failure
    # must not replay side effects through another provider. Check separate lines
    # so an actual provider rejection after an earlier tool error is still detected.
    return any(USAGE_LIMIT_RE.search(line) and not PERMISSION_ERROR_RE.search(line)
               for text in texts if text for line in text.splitlines())


def identity(engine: str, config_dir: str | None) -> tuple[str, str | None]:
    """계정 로그인 상태를 짐작한다. (상태, 표시 이름). 토큰은 읽지 않는다.

    claude: <config_dir>/.claude.json 의 oauthAccount.emailAddress(기본은 ~/.claude.json).
    codex: <config_dir 또는 ~/.codex>/auth.json 존재 여부만(키체인 저장이면 알 수 없다).
    """
    base = Path(config_dir).expanduser() if config_dir else None
    if engine == "copilot":
        from ttakkari import copilot_accounts as managed_cp
        try:
            state = managed_cp.status(base, is_system=(base is None))
            st = state.get("auth_state")
            return ("ok" if st == "authenticated" else "missing" if st == "setup_required" else "unknown"), state.get("principal")
        except (ValueError, OSError):
            return "missing", None
    if engine == "opencode":
        if base:
            from ttakkari import opencode_accounts as managed_oc
            try:
                state = managed_oc.status(base)
                st = state.get("auth_state")
                return ("ok" if st == "authenticated" else "missing" if st == "setup_required" else "unknown"), state.get("principal")
            except (ValueError, OSError):
                return "missing", None
        return "unknown", None
    if engine == "antigravity":
        if base:
            from ttakkari import antigravity_accounts as managed
            try:
                state = managed.status(base)
                return ("ok" if state["auth_state"] == "ready" else "missing"), state.get("principal")
            except (ValueError, OSError):
                return "missing", None
        # The official CLI exposes no read-only login-status API. Do not inspect its credentials.
        return "unknown", None
    if engine == "muse":
        # SDK account/read supplies the actual lane; do not inspect auth.json.
        return "unknown", None
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
    if m.startswith(("github-copilot/", "opencode/")):
        return "opencode"
    if m.startswith("copilot/"):
        return "copilot"
    if m in ANTIGRAVITY_MODELS:
        return "antigravity"
    if m.startswith(("muse-wonder-", "muse-spark-")):
        return "muse"
    if m in CLAUDE_MODELS or m.startswith("claude"):
        return "claude"
    if m.startswith("gpt-oss-"):
        return "antigravity"
    if m.startswith(("gpt", "codex", "o1", "o3", "o4")):
        return "codex"
    if m.startswith("gemini-"):
        return "antigravity"
    if m.startswith("qwen"):
        return "qwen-local"
    return None
