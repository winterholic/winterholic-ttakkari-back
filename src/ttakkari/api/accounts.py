"""엔진 계정 관리(ADR 0006). 이름·엔진·계정 폴더·켜기·우선순위. 순서가 사용량 한도 폴백 순서다.

계정 추가·삭제는 개인 목록에서만 한다. 그중 회사 모드에도 쓸 계정을 고르면(pool=work) 회사 모드가 자기 순서·켜기·임시 순서를 따로 가진다."""

from __future__ import annotations

import asyncio
import logging
import shlex
import sys
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, HTTPException, Request, Response, status
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from ttakkari.api.deps import Db
from ttakkari.audit import record
from ttakkari.broker.policy import is_within
from ttakkari.config import get_settings
from ttakkari.models import AppSetting, EngineAccount, utcnow
from ttakkari.runs import profiles
from ttakkari.runs import usage as usage_mod
from ttakkari.runs.manager import manager
from ttakkari.security.auth import User, client_ip

router = APIRouter(prefix="/api/accounts", tags=["accounts"])
log = logging.getLogger(__name__)
MAX_ACCOUNTS = 20


def _clean_dir(v: str | None) -> str | None:
    if v is None or not v.strip():
        return None
    p = Path(v.strip()).expanduser()
    if not p.is_absolute():
        raise ValueError("계정 폴더는 절대 경로나 ~ 로 시작하는 경로여야 합니다.")
    if is_within(p.resolve(), get_settings().home):
        raise ValueError("ttakkari 데이터 폴더 안은 계정 폴더로 쓸 수 없습니다.")
    return str(p)


Pool = Literal["personal", "work"]


class AccountIn(BaseModel):
    name: str = Field(min_length=1, max_length=60)
    engine: Literal["claude", "codex", "antigravity", "muse"]
    pool: Pool = "personal"
    config_dir: str | None = Field(default=None, max_length=1024)
    enabled: bool = True
    max_used_percent: int | None = Field(default=None, ge=50, le=100)

    @field_validator("config_dir")
    @classmethod
    def _dir(cls, v: str | None) -> str | None:
        return _clean_dir(v)

    @field_validator("config_dir")
    @classmethod
    def _no_antigravity_account_folder(cls, value: str | None, info):
        if info.data.get("engine") == "antigravity" and value:
            raise ValueError("Antigravity uses the single logged-in agy profile; config_dir is not supported.")
        return value


class AccountPatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=60)
    # work 로 회사 모드에 넣고(회사 순서 맨 뒤, 회사 모드에서 켠 채로), personal 로 뺀다. 개인 모드 순서·켜기는 그대로다.
    pool: Pool | None = None
    config_dir: str | None = Field(default=None, max_length=1024)
    # 개인 모드 켜기
    enabled: bool | None = None
    # 회사 모드 켜기. 개인 모드 enabled 와 독립이다. 회사에 들어온 계정에만 쓸 수 있다.
    work_enabled: bool | None = None
    max_used_percent: int | None = Field(default=None, ge=50, le=100)

    @field_validator("config_dir")
    @classmethod
    def _dir(cls, v: str | None) -> str | None:
        return _clean_dir(v)


class ReorderIn(BaseModel):
    ids: list[uuid.UUID] = Field(min_length=1, max_length=MAX_ACCOUNTS)


class AccountOut(BaseModel):
    id: uuid.UUID
    name: str
    engine: str
    pool: str
    config_dir: str | None
    enabled: bool
    priority: int
    max_used_percent: int
    # 회사 모드의 켜기·순서(pool=work 일 때만 의미). 개인 모드의 enabled·priority 와 독립이다.
    work_enabled: bool
    work_priority: int
    # ok(로그인 확인) | unknown(확인 못 함) | missing(계정 폴더 없음)
    login: str
    login_label: str | None
    cooldown_seconds: int
    # 처음 한 번 Mac Studio 터미널에서 실행할 로그인 명령(브라우저 인증). 이미 로그인됐으면 None.
    login_command: str | None = None
    auth_state: str | None = None
    auth_error: str | None = None
    isolation_mode: str | None = None
    execution_enabled: bool = True


def accounts_dir() -> Path:
    return get_settings().home / "accounts"


def login_command(engine: str, config_dir: str | None) -> str:
    if engine == "claude":
        # 초기 프롬프트로 /login 을 넘기는 동작은 버전마다 다를 수 있어(확인 필요) claude 를 띄우고 /login 을 입력하게 안내한다.
        return f"CLAUDE_CONFIG_DIR={config_dir} claude" if config_dir else "claude"
    if engine == "antigravity":
        if config_dir:
            return shlex.join(["env", f"PYTHONPATH={Path(__file__).resolve().parents[2]}", sys.executable,
                               "-m", "ttakkari.antigravity_accounts", "login", config_dir])
        return "agy"
    if engine == "muse":
        from ttakkari.runs.profiles import Profile
        env = Profile(uuid.uuid4(), "", "muse", Path(config_dir) if config_dir else None).env()
        return shlex.join(["env", "-u", "META_API_KEY", "-u", "MUSE_AUTH_PATH", "-u", "MUSE_TRANSPORT_TRACE",
                           *[f"{key}={value}" for key, value in env.items()],
                           get_settings().muse_bin, "login"])
    return f"CODEX_HOME={config_dir} codex login" if config_dir else "codex login"


def _next_work_priority(rows: list[EngineAccount]) -> int:
    return max((r.work_priority for r in rows if r.pool == "work"), default=-1) + 1


def _out(a: EngineAccount) -> AccountOut:
    login, label = profiles.identity(a.engine, a.config_dir)
    if a.engine == "muse":
        # Authentication is observed even when usage/read has no quota snapshot.
        observed = usage_mod.cached(a.id)
        if observed is not None and observed.login:
            login = observed.login
    managed_status = {}
    if a.engine == 'antigravity' and a.config_dir:
        from ttakkari import antigravity_accounts as managed
        try:
            managed_status = managed.status(a.config_dir)
        except (ValueError, OSError):
            managed_status = {'auth_state': 'invalid_profile', 'last_error': 'invalid_profile'}
    return AccountOut(id=a.id, name=a.name, engine=a.engine, pool=a.pool, config_dir=a.config_dir, enabled=a.enabled,
                      priority=a.priority, max_used_percent=a.max_used_percent, work_enabled=a.work_enabled, work_priority=a.work_priority, login=login, login_label=label, cooldown_seconds=profiles.cooldown_remaining(a.id),
                      login_command=None if login == "ok" else login_command(a.engine, a.config_dir),
                      auth_state=managed_status.get('auth_state'), auth_error=managed_status.get('last_error'),
                      isolation_mode='managed_home_keychain' if managed_status else None,
                      execution_enabled=profiles.execution_ready(profiles._profile(a)))


async def _get(db: Db, account_id: uuid.UUID) -> EngineAccount:
    a = await db.get(EngineAccount, account_id)
    if a is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "계정이 없습니다.")
    return a


async def _commit(db: Db) -> None:
    try:
        await db.commit()
    except IntegrityError as e:
        await db.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, "같은 이름의 계정이 있습니다.") from e


@router.get("", response_model=list[AccountOut])
async def list_accounts(_: User, db: Db) -> list[AccountOut]:
    rows = await profiles.accounts(db)
    await db.commit()
    # Reuse the usage collector's official account/read observation and TTL.
    # This also covers opening Settings before the first collector pass.
    await asyncio.gather(*(usage_mod.usage_for(a.id, a.engine, a.config_dir)
                           for a in rows if a.engine == "muse"))
    return [_out(a) for a in rows]


@router.get("/qwen-flash-usage")
async def qwen_flash_usage(_: User, db: Db, response: Response) -> dict:
    """Standalone Flash Next server counters, with persisted daily/weekly windows."""
    from ttakkari.runs import qwen_flash_monitor
    response.headers["Cache-Control"] = "no-store"
    return await qwen_flash_monitor.snapshot_with_history(db)


@router.get("/qwen-usage")
async def qwen_usage(_: User, db: Db) -> dict:
    """Local Qwen tokens from operational desk HTTP calls; evaluation requests are excluded."""
    from ttakkari.runs import desk_qwen_server
    from ttakkari.runs import qwen_usage as usage
    return await usage.summary(db, server=desk_qwen_server.status())


@router.post("", response_model=AccountOut, status_code=201)
async def create_account(body: AccountIn, _: User, db: Db, request: Request) -> AccountOut:
    rows = await profiles.accounts(db)
    if body.engine == "antigravity":
        # Serialize the check across requests and backend generations. A second
        # generated config_dir does not isolate the official CLI's credentials.
        await db.execute(select(func.pg_advisory_xact_lock(0x54544B, 3)))
        rows = await profiles.accounts(db)
    if len(rows) >= MAX_ACCOUNTS:
        raise HTTPException(status.HTTP_409_CONFLICT, f"계정은 {MAX_ACCOUNTS}개까지입니다.")
    top = await db.scalar(select(func.max(EngineAccount.priority)))
    a = EngineAccount(id=uuid.uuid4(), name=body.name.strip(), engine=body.engine, config_dir=body.config_dir,
                      enabled=body.enabled, priority=(top if top is not None else -1) + 1, pool=body.pool,
                      work_enabled=body.enabled, work_priority=_next_work_priority(rows),
                      max_used_percent=body.max_used_percent if body.max_used_percent is not None else 100)
    if a.config_dir is None and any(r.engine == body.engine and r.config_dir is None for r in rows):
        # 같은 엔진의 기본 계정(~/.claude·~/.codex)이 이미 있으면, 새 계정의 로그인 보관 폴더는 따까리가 만든다.
        # 사용자는 폴더를 몰라도 된다. 작업공간과는 무관하고 로그인 정보만 따로 둔다.
        d = accounts_dir() / str(a.id)
        if a.engine == 'antigravity':
            from ttakkari import antigravity_accounts as managed
            a.config_dir = str(managed.allocate(accounts_dir().resolve(), a.id).home)
        else:
            d.mkdir(parents=True, exist_ok=True, mode=0o700)
            a.config_dir = str(d)
    db.add(a)
    record(db, action="account.create", target_type="account", detail={"name": a.name, "engine": a.engine},
           ip=client_ip(request))
    await _commit(db)
    return _out(a)


# /override·/usage 는 /{account_id} 라우트보다 먼저 등록해야 UUID 검증(422)에 먼저 걸리지 않는다.
class OverrideIn(BaseModel):
    ids: list[uuid.UUID] = Field(min_length=1, max_length=MAX_ACCOUNTS)
    until: datetime


class OverrideOut(BaseModel):
    active: bool
    ids: list[uuid.UUID] = Field(default_factory=list)
    until: datetime | None = None


@router.get("/override", response_model=OverrideOut)
async def get_override(_: User, db: Db, pool: Pool = "personal") -> OverrideOut:
    ov = await profiles.get_override(db, pool)
    if not ov:
        return OverrideOut(active=False)
    return OverrideOut(active=True, ids=ov["ids"], until=ov["until"])


@router.put("/override", response_model=OverrideOut)
async def put_override(body: OverrideIn, _: User, db: Db, request: Request, pool: Pool = "personal") -> OverrideOut:
    """정한 시각까지만 이 순서로 쓴다(pool 의 개인·회사 모드마다 따로). 지나면 저절로 기본 순서로 돌아간다."""
    until = body.until if body.until.tzinfo else body.until.replace(tzinfo=UTC)
    if until <= utcnow():
        raise HTTPException(status.HTTP_400_BAD_REQUEST, {"code": "bad_input", "message": "끝나는 시각이 이미 지났어요."})
    known = {a.id for a in await profiles.accounts(db) if pool == "personal" or a.pool == "work"}
    if not set(body.ids) <= known:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, {"code": "bad_input", "message": "없는 계정이 섞여 있어요."})
    row = await db.get(AppSetting, profiles.override_key(pool))
    value = {"ids": [str(i) for i in body.ids], "until": until.isoformat()}
    if row is None:
        db.add(AppSetting(key=profiles.override_key(pool), value=value))
    else:
        row.value = value
    record(db, action="account.override", detail={**value, "pool": pool}, ip=client_ip(request))
    await db.commit()
    return OverrideOut(active=True, ids=body.ids, until=until)


@router.delete("/override", status_code=204)
async def delete_override(_: User, db: Db, pool: Pool = "personal") -> None:
    row = await db.get(AppSetting, profiles.override_key(pool))
    if row is not None:
        await db.delete(row)
        await db.commit()


class UsageWindowOut(BaseModel):
    key: str
    label: str
    percent: int
    resets_at: int | None
    remaining_percent: float | None = None
    # Muse 만 current/stale/unavailable 을 구분한다. unavailable 은 자리표시자라 percent 는 의미가 없다.
    state: Literal["current", "stale", "unavailable"] = "current"


class ResetCreditsOut(BaseModel):
    available: int
    next_expires_at: int | None


class AccountUsageOut(BaseModel):
    id: uuid.UUID
    name: str
    engine: str
    pool: str
    enabled: bool
    rank: int
    max_used_percent: int
    remaining_to_limit_percent: int | None
    stale: bool
    # 회사 모드의 켜기·순위. 회사에 들어온 계정(pool=work)만 work_rank 가 있다.
    work_enabled: bool
    work_rank: int | None
    login: str
    cooldown_seconds: int
    windows: list[UsageWindowOut]
    reset_credits: ResetCreditsOut | None = None
    credits_balance: int | None = None
    credits_error: str | None = None
    source: str | None
    plan: str | None
    error: str | None
    updated_at: float | None
    execution_enabled: bool = True
    # 새 값을 읽는 조회가 아직 진행 중(요청 시간 초과로 마지막 값을 대신 준 경우 포함).
    refreshing: bool = False


# 느린 공급자(Muse·Antigravity 하위 프로세스) 하나가 응답 전체를 붙잡지 않게 요청당 기다리는 한도.
# 넘으면 마지막으로 본 값을 refreshing=True 로 돌려주고, 조회는 뒤에서 이어져 캐시를 채운다.
USAGE_REQUEST_TIMEOUT = 25.0
# 계정 하나를 직접 다시 재는 요청은 느린 경로까지 끝까지 기다린다: Muse(SDK 25초 + TUI 23초 + 관측 핑 45초), 토큰이 만료된 Claude(갱신 호출 60초 + 조회).
# 이보다 길면 504 로 끝내 화면이 무한히 기다리지 않게 한다.
REFRESH_TIMEOUT = {"muse": 100.0, "claude": 80.0}


def _muse_windows(u: usage_mod.Usage, now: float, current: bool) -> list[UsageWindowOut]:
    """Muse 는 5시간·주간 창을 항상 내보낸다. 창을 없애면 사용량이 0이라는 뜻으로 읽히기 때문이다.

    초기화 시각이 지난 창의 낡은 퍼센트는 현재 잔여량이 아니므로 값을 버리고 unavailable 로 둔다.
    아직 지나지 않은 창은 마지막 관측값을 current(최신 조회 성공) 또는 stale(조회 실패·불완전)로 표시한다."""
    state = "current" if current else "stale"
    seen = {w.key: w for w in u.windows}
    out = []
    for key in ("five_hour", "seven_day"):
        w = seen.get(key)
        if w is None:
            out.append(UsageWindowOut(key=key, label=usage_mod.label_of(key), percent=0, resets_at=None, state="unavailable"))
        elif w.resets_at and w.resets_at <= now:
            out.append(UsageWindowOut(key=key, label=w.label, percent=0, resets_at=w.resets_at, state="unavailable"))
        else:
            out.append(UsageWindowOut(**w.__dict__, state=state))
    out.extend(UsageWindowOut(**w.__dict__, state=state) for w in u.windows
               if w.key not in ("five_hour", "seven_day") and not (w.resets_at and w.resets_at <= now))
    return out


def _agy_windows(u: usage_mod.Usage, now: float, current: bool) -> list[UsageWindowOut]:
    """Antigravity 는 그룹·창마다 따로 초기화된다. 창을 숨기지 않고 창별 state 로 알린다.

    초기화 시각이 지난 창의 낡은 값은 현재 잔여량이 아니므로 값을 버리고 unavailable 로 둔다.
    아직 지나지 않은 창은 최신 조회 성공이면 current, 조회 실패·지연이면 stale 이다.
    reset_time 이 없는 창(공급자가 주지 않음)은 지났는지 알 수 없으니 관측값 그대로 둔다."""
    state = "current" if current else "stale"
    out = []
    for w in u.windows:
        if w.resets_at and w.resets_at <= now:
            out.append(UsageWindowOut(key=w.key, label=w.label, percent=0, resets_at=w.resets_at, remaining_percent=None,
                                      state="unavailable"))
        else:
            out.append(UsageWindowOut(**w.__dict__, state=state))
    return out


def _age_text(seconds: float) -> str:
    seconds = max(0, int(seconds))
    if seconds >= 86400:
        return f"{seconds // 86400}일 {seconds % 86400 // 3600}시간 전"
    return f"{seconds // 3600}시간 {seconds % 3600 // 60}분 전" if seconds >= 3600 else f"{max(1, seconds // 60)}분 전"


def _usage_out(i: int, a: EngineAccount, work_rank: dict[uuid.UUID, int], u: usage_mod.Usage, *, refreshing: bool = False,
               error: str | None = None) -> AccountUsageOut:
    login, _label = profiles.identity(a.engine, a.config_dir)
    now = time.time()
    is_muse = a.engine == "muse"
    # 비API 출처(fallback/세션로그), Muse 계정은 초기화 시각이 지났는데 한도까지 쓴 창을 제외한다.
    has_expired_exhausted = any(
        w.resets_at and w.resets_at <= now and w.percent >= a.max_used_percent
        for w in u.windows
    ) if is_muse or u.source != "api" else False
    muse_current = not is_muse or usage_mod.muse_quota_current(u, now)
    is_agy = a.engine == "antigravity"
    aged = u.updated_at is None or now - u.updated_at > get_settings().usage_stale_seconds
    agy_expired = [w for w in u.windows if w.resets_at and w.resets_at <= now] if is_agy else []
    stale = aged or has_expired_exhausted or not muse_current or bool(agy_expired) or (is_agy and bool(u.error))
    valid_windows = [
        w for w in u.windows
        if not ((is_muse or u.source != "api") and w.resets_at and w.resets_at <= now
                and w.percent >= a.max_used_percent)
    ]
    headroom = None if stale or not muse_current else min((max(0, a.max_used_percent - w.percent) for w in valid_windows
                                       if w.key in ("five_hour", "seven_day")
                                       and not (w.resets_at and w.resets_at <= now)), default=None)
    if is_muse:
        windows_out = _muse_windows(u, now, muse_current)
    elif is_agy:
        # 창별 state 는 그 창의 초기화 경과 여부와 조회 신선도만 본다(다른 창이 지났다고 이 창이 낡은 값이 되지 않는다).
        windows_out = _agy_windows(u, now, not aged and not u.error)
    else:
        # Claude·Codex: 최신 조회 성공이 아니거나(오래됨·오류) 초기화 시각이 지난 창은 현재 값이 아니다. 화면이 0% 나 낡은
        # 퍼센트를 현재 사용량으로 읽지 않게 창마다 상태를 알린다.
        windows_out = [UsageWindowOut(**w.__dict__, state="unavailable" if w.resets_at and w.resets_at <= now
                                      else "stale" if stale or u.error else "current") for w in valid_windows]
    err = u.error or error
    if aged and not err and u.windows and not is_muse:
        err = f"새 사용량을 읽지 못했어요 · 마지막 측정 {_age_text(now - u.updated_at) if u.updated_at else '시각 불명'}"
    if is_muse and not muse_current and not err:
        err = "Muse 최신 사용량 미확인 · 5시간·주간 창 모두 확인되지 않음"
    if is_muse and not muse_current and err and any(w.key == "five_hour" and w.resets_at and w.resets_at <= now for w in u.windows):
        # 초기화 뒤 쓰지 않은 창은 공급자가 생략한다. 0% 로 읽히지 않게 이유와 갱신 조건을 알린다.
        err += " · 5시간 창이 초기화된 뒤 쓰지 않아 값이 없어요. Muse 를 쓰면 갱신돼요"
    if (not valid_windows or (is_agy and len(agy_expired) == len(u.windows))) and u.windows and not err:
        err = f"{a.engine.capitalize()} 초기화 시각 경과 · 새 사용량 미관측"
    return AccountUsageOut(id=a.id, name=a.name, engine=a.engine, pool=a.pool, enabled=a.enabled, rank=i + 1,
                           max_used_percent=a.max_used_percent, remaining_to_limit_percent=headroom,
                           stale=stale,
                           work_enabled=a.work_enabled, work_rank=work_rank.get(a.id), login=u.login or login,
                           cooldown_seconds=profiles.cooldown_remaining(a.id),
                           windows=windows_out, source=u.source, plan=u.plan,
                           reset_credits=ResetCreditsOut(**u.reset_credits.__dict__) if u.reset_credits else None,
                           credits_balance=u.credits_balance, credits_error=u.credits_error,
                           error=err, updated_at=u.updated_at, refreshing=refreshing,
                           execution_enabled=profiles.execution_ready(profiles._profile(a)) and
                                             (a.engine != "muse" or get_settings().muse_enabled))


async def _ordered_usage_accounts(db: Db) -> tuple[list[tuple[int, EngineAccount]], dict[uuid.UUID, int]]:
    ordered = list(enumerate(await profiles.effective_order(db)))
    work_rank = {a.id: i + 1 for i, a in enumerate(await profiles.effective_order(db, "work"))}
    return ordered, work_rank


async def _live_usage(i: int, a: EngineAccount, work_rank: dict[uuid.UUID, int], refresh: bool) -> AccountUsageOut:
    try:
        u = await asyncio.wait_for(usage_mod.usage_for(a.id, a.engine, a.config_dir, refresh=refresh), USAGE_REQUEST_TIMEOUT)
    except TimeoutError:
        last = usage_mod.cached(a.id) or usage_mod.Usage()
        return _usage_out(i, a, work_rank, last, refreshing=True, error="새 사용량을 읽는 중이에요 · 마지막 관측값")
    return _usage_out(i, a, work_rank, u, refreshing=usage_mod.fetching(a.id))


@router.get("/usage", response_model=list[AccountUsageOut])
async def usage(_: User, db: Db, refresh: bool = False) -> list[AccountUsageOut]:
    """계정별 사용량. 개인 모드의 지금 적용되는 순서(임시 순서 반영), rank 는 그 순서의 번호, work_rank 는 회사 모드 순서의 번호. 계정마다 2분 캐시, refresh 면 새로.

    계정은 병렬로 읽고, 한 계정이 USAGE_REQUEST_TIMEOUT 을 넘기면 그 계정만 마지막 값으로 대신한다."""
    ordered, work_rank = await _ordered_usage_accounts(db)
    # Provider calls may take seconds. Return the connection before waiting;
    # refresh completion can itself need another connection to wake queued Runs.
    await db.commit()
    return list(await asyncio.gather(*(_live_usage(i, a, work_rank, refresh) for i, a in ordered)))


@router.get("/usage/cached", response_model=list[AccountUsageOut])
async def usage_cached(_: User, db: Db) -> list[AccountUsageOut]:
    """네트워크 없이 마지막으로 본 값만. 계정 목록·순서·켜기·쿨다운은 지금 값이고 사용량은 캐시 값(없으면 windows 가 비고 updated_at 이 null)이다.
    화면이 카드를 바로 그린 뒤 계정마다 /{id}/usage 로 새로 읽는다."""
    ordered, work_rank = await _ordered_usage_accounts(db)
    return [_usage_out(i, a, work_rank, usage_mod.cached(a.id) or usage_mod.Usage(), refreshing=usage_mod.fetching(a.id))
            for i, a in ordered]


@router.get("/{account_id}/usage", response_model=AccountUsageOut)
async def account_usage(account_id: uuid.UUID, _: User, db: Db, refresh: bool = False) -> AccountUsageOut:
    """계정 하나의 사용량. /usage 의 한 줄과 같은 모양이라 카드마다 따로 불러 준비된 것부터 보여 줄 수 있다."""
    ordered, work_rank = await _ordered_usage_accounts(db)
    await db.commit()
    for i, a in ordered:
        if a.id == account_id:
            out = await _live_usage(i, a, work_rank, refresh)
            if refresh and not out.error:
                prof = profiles._profile(a)
                if not profiles.usage_limit_until(prof):
                    profiles.clear_cooldown(a.id)
                    await manager.release_waiting()
            return out.model_copy(update={"cooldown_seconds": profiles.cooldown_remaining(a.id)})
    raise HTTPException(status.HTTP_404_NOT_FOUND, "계정을 찾을 수 없어요.")


@router.post("/{account_id}/usage/refresh", response_model=AccountUsageOut)
async def refresh_account_usage(account_id: uuid.UUID, _: User, db: Db, request: Request) -> AccountUsageOut:
    """계정 하나를 즉시 다시 측정하고, 측정 성공 시 한도 대기(쿨다운·한도 대기 Run)를 갱신·해제한다."""
    ordered, work_rank = await _ordered_usage_accounts(db)
    target = None
    for i, a in ordered:
        if a.id == account_id:
            target = (i, a)
            break
    if target is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "계정을 찾을 수 없어요.")

    i, a = target
    await db.commit()
    try:
        u = await asyncio.wait_for(
            usage_mod.usage_for(a.id, a.engine, a.config_dir, refresh=True),
            REFRESH_TIMEOUT.get(a.engine, 35.0),
        )
    except TimeoutError:
        record(db, action="account.usage_refresh", target_type="account", target_id=a.id,
               outcome="error", detail={"error": "timeout"}, ip=client_ip(request))
        await _commit(db)
        raise HTTPException(status.HTTP_504_GATEWAY_TIMEOUT,
                            f"사용량 측정 시간 초과({a.engine}): {REFRESH_TIMEOUT.get(a.engine, 35.0):.0f}초 안에 공급자 응답을 받지 못했어요.")
    except Exception as e:  # noqa: BLE001
        log.warning("usage refresh failed for account %s: %s", a.id, type(e).__name__)
        record(db, action="account.usage_refresh", target_type="account", target_id=a.id,
               outcome="error", detail={"error": type(e).__name__}, ip=client_ip(request))
        await _commit(db)
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, f"사용량 측정 실패: {type(e).__name__}")

    # 조회 실패와 미관측은 쿨다운 해제의 근거가 아니다.
    prof = profiles._profile(a)
    if not u.error and u.windows and not profiles.usage_limit_until(prof):
        profiles.clear_cooldown(a.id)
        await manager.release_waiting()

    record(db, action="account.usage_refresh", target_type="account", target_id=a.id,
           outcome="error" if u.error else "ok",
           detail={"error": u.error} if u.error else {"source": u.source, "plan": u.plan},
           ip=client_ip(request))
    await _commit(db)
    return _usage_out(i, a, work_rank, u, refreshing=False)


@router.patch("/{account_id}", response_model=AccountOut)
async def patch_account(account_id: uuid.UUID, body: AccountPatch, _: User, db: Db, request: Request) -> AccountOut:
    a = await _get(db, account_id)
    data = body.model_dump(exclude_unset=True)
    muse_profile_changed = a.engine == "muse" and "config_dir" in data and data["config_dir"] != a.config_dir
    if a.engine == "antigravity" and "config_dir" in data and data['config_dir'] != a.config_dir:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY,
                            "Antigravity managed profile paths cannot be reassigned or cleared.")
    if "name" in data and data["name"] is not None:
        data["name"] = data["name"].strip()
    for k in ("pool", "enabled", "work_enabled", "max_used_percent"):
        if k in data and data[k] is None:
            del data[k]
    if data.get("pool") == "work" and a.pool != "work":
        # 회사 모드에 들일 때는 회사 순서 맨 뒤에 켠 채로 놓는다. 개인 모드 순서·켜기는 건드리지 않는다.
        data["work_priority"] = _next_work_priority(await profiles.accounts(db))
        data.setdefault("work_enabled", True)
    if data.get("work_enabled") is not None and (data.get("pool") or a.pool) != "work":
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "회사 모드에 넣은 계정만 회사 모드 켜기를 바꿀 수 있어요.")
    for k, v in data.items():
        setattr(a, k, v)
    revived = bool(data.get("enabled") or data.get("work_enabled"))
    if revived:
        # 다시 켜면 한도 대기도 풀어 바로 쓰게 한다.
        profiles.clear_cooldown(a.id)
    record(db, action="account.update", target_type="account", target_id=a.id, detail=data, ip=client_ip(request))
    await _commit(db)
    if muse_profile_changed:
        usage_mod.forget(a.id)
    if revived:
        await manager.release_waiting()
    return _out(a)


@router.delete("/{account_id}", status_code=204)
async def delete_account(account_id: uuid.UUID, _: User, db: Db, request: Request) -> None:
    a = await _get(db, account_id)
    await db.delete(a)
    profiles.clear_cooldown(account_id)
    record(db, action="account.delete", target_type="account", target_id=account_id, detail={"name": a.name},
           ip=client_ip(request))
    await db.commit()


@router.post("/reorder", response_model=list[AccountOut])
async def reorder(body: ReorderIn, _: User, db: Db, pool: Pool = "personal") -> list[AccountOut]:
    """pool 모드의 기본 순서. 보낸 계정을 앞에 두고, 읽은 뒤 새로 생긴 계정은 기존 상대 순서로 뒤에 둔다.

    personal 은 모든 계정이 대상이고 priority 를, work 는 회사에 들어온 계정만 대상이고 work_priority 를 바꾼다."""
    everyone = await profiles.accounts(db)
    current = [a for a in everyone if pool == "personal" or a.pool == "work"]
    if pool == "work":
        current.sort(key=lambda a: (a.work_priority, a.priority))
    rows = {a.id: a for a in current}
    supplied = set(body.ids)
    unknown = supplied - set(rows)
    duplicate_count = len(body.ids) - len(supplied)
    if unknown or duplicate_count:
        log.warning("account reorder rejected: pool=%s received=%d duplicate=%d unknown=%d current=%d missing=%d",
                    pool, len(body.ids), duplicate_count, len(unknown), len(rows), len(set(rows) - supplied))
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "모든 계정 id 를 한 번씩 보내야 합니다.")
    ordered = [rows[aid] for aid in body.ids] + [a for a in current if a.id not in supplied]
    for i, account in enumerate(ordered):
        if pool == "work":
            account.work_priority = i
        else:
            account.priority = i
    await db.commit()
    return [_out(a) for a in ordered]


class CodexResetOut(BaseModel):
    outcome: str
    message: str


@router.post("/{account_id}/codex-reset", response_model=CodexResetOut)
async def codex_reset(account_id: uuid.UUID, _: User, db: Db, request: Request) -> CodexResetOut:
    """Codex rate-limit 재설정 크레딧을 한 개 쓴다(Orca 의 「지금 재설정」과 같은 동작). 되돌릴 수 없다."""
    a = await _get(db, account_id)
    if a.engine != "codex":
        raise HTTPException(status.HTTP_400_BAD_REQUEST, {"code": "bad_input", "message": "Codex 계정만 재설정할 수 있어요."})
    outcome, message = await usage_mod.codex_reset(a.config_dir)
    if outcome == "reset":
        # 한도가 풀렸으니 쉬던 계정을 바로 다시 쓴다.
        profiles.clear_cooldown(a.id)
        await manager.release_waiting()
    usage_mod.forget(a.id)
    record(db, action="account.codex_reset", target_type="account", target_id=a.id,
           outcome="ok" if outcome in ("reset", "nothing_to_reset") else "error", detail={"outcome": outcome},
           ip=client_ip(request))
    await db.commit()
    return CodexResetOut(outcome=outcome, message=message)


@router.post("/{account_id}/cooldown/clear", response_model=AccountOut)
async def clear(account_id: uuid.UUID, _: User, db: Db) -> AccountOut:
    a = await _get(db, account_id)
    profiles.clear_cooldown(a.id)
    # 한도 대기 중이던 Run 이 있으면 바로 다시 시도한다. 여전히 못 쓰면 다시 대기한다.
    await manager.release_waiting()
    return _out(a)


class AntigravityConfirmIn(BaseModel):
    # 사용자가 방금 전용 HOME 으로 로그인한 Google 계정. 따까리는 이 값을 토큰에서 읽지 않고 사용자 확인으로만 받는다.
    principal: str | None = Field(default=None, max_length=254)
    confirm: bool = False


@router.post("/{account_id}/antigravity/check", response_model=AccountOut)
async def antigravity_check(account_id: uuid.UUID, body: AntigravityConfirmIn, _: User, db: Db,
                            request: Request) -> AccountOut:
    """전용 HOME 으로 agy /usage 를 한 번 호출해 로그인 여부를 확인한다. confirm=true 면 사용자가 로그인 계정을 확인한 것으로 기록한다."""
    from ttakkari import antigravity_accounts as managed
    a = await _get(db, account_id)
    if a.engine != "antigravity" or not a.config_dir:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "전용 HOME 을 쓰는 Antigravity 추가 계정만 확인할 수 있습니다.")
    principal = (body.principal or "").strip() or None
    if body.confirm and not principal:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "로그인한 Google 계정을 입력해야 확인할 수 있습니다.")
    try:
        if managed.load(a.config_dir).account_id != a.id:
            raise managed.ProfileError("account_mismatch")
        await asyncio.to_thread(managed.check, a.config_dir, principal=principal, confirm_isolated_login=body.confirm)
    except (managed.ProfileError, OSError) as exc:
        code = str(exc) if isinstance(exc, managed.ProfileError) else "cli_error"
        record(db, action="account.antigravity_check", target_type="account", target_id=a.id,
               detail={"ok": False, "code": code}, ip=client_ip(request))
        await _commit(db)
        raise HTTPException(status.HTTP_409_CONFLICT, {"code": code, "message": f"Antigravity 확인 실패: {code}"}) from exc
    record(db, action="account.antigravity_check", target_type="account", target_id=a.id,
           detail={"ok": True, "confirmed": body.confirm}, ip=client_ip(request))
    await _commit(db)
    await manager.release_waiting()
    return _out(a)
