"""엔진 계정 관리(ADR 0006). 이름·엔진·계정 폴더·켜기·우선순위. 순서가 사용량 한도 폴백 순서다."""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, HTTPException, Request, status
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
from ttakkari.security.auth import User, client_ip

router = APIRouter(prefix="/api/accounts", tags=["accounts"])
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
    engine: Literal["claude", "codex"]
    pool: Pool = "personal"
    config_dir: str | None = Field(default=None, max_length=1024)
    enabled: bool = True

    @field_validator("config_dir")
    @classmethod
    def _dir(cls, v: str | None) -> str | None:
        return _clean_dir(v)


class AccountPatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=60)
    # 개인 ↔ 회사로 옮긴다. 옮기면 그 묶음의 맨 뒤 순위가 된다.
    pool: Pool | None = None
    config_dir: str | None = Field(default=None, max_length=1024)
    enabled: bool | None = None

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
    # ok(로그인 확인) | unknown(확인 못 함) | missing(계정 폴더 없음)
    login: str
    login_label: str | None
    cooldown_seconds: int
    # 처음 한 번 Mac Studio 터미널에서 실행할 로그인 명령(브라우저 인증). 이미 로그인됐으면 None.
    login_command: str | None = None


def accounts_dir() -> Path:
    return get_settings().home / "accounts"


def login_command(engine: str, config_dir: str | None) -> str:
    if engine == "claude":
        # 초기 프롬프트로 /login 을 넘기는 동작은 버전마다 다를 수 있어(확인 필요) claude 를 띄우고 /login 을 입력하게 안내한다.
        return f"CLAUDE_CONFIG_DIR={config_dir} claude" if config_dir else "claude"
    return f"CODEX_HOME={config_dir} codex login" if config_dir else "codex login"


def _out(a: EngineAccount) -> AccountOut:
    login, label = profiles.identity(a.engine, a.config_dir)
    return AccountOut(id=a.id, name=a.name, engine=a.engine, pool=a.pool, config_dir=a.config_dir, enabled=a.enabled,
                      priority=a.priority, login=login, login_label=label, cooldown_seconds=profiles.cooldown_remaining(a.id),
                      login_command=None if login == "ok" else login_command(a.engine, a.config_dir))


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
    return [_out(a) for a in await profiles.accounts(db)]


@router.post("", response_model=AccountOut, status_code=201)
async def create_account(body: AccountIn, _: User, db: Db, request: Request) -> AccountOut:
    rows = await profiles.accounts(db)
    if len(rows) >= MAX_ACCOUNTS:
        raise HTTPException(status.HTTP_409_CONFLICT, f"계정은 {MAX_ACCOUNTS}개까지입니다.")
    top = await db.scalar(select(func.max(EngineAccount.priority)).where(EngineAccount.pool == body.pool))
    a = EngineAccount(id=uuid.uuid4(), name=body.name.strip(), engine=body.engine, config_dir=body.config_dir,
                      enabled=body.enabled, priority=(top if top is not None else -1) + 1, pool=body.pool)
    if a.config_dir is None and any(r.engine == body.engine and r.config_dir is None for r in rows):
        # 같은 엔진의 기본 계정(~/.claude·~/.codex)이 이미 있으면, 새 계정의 로그인 보관 폴더는 따까리가 만든다.
        # 사용자는 폴더를 몰라도 된다. 작업공간과는 무관하고 로그인 정보만 따로 둔다.
        d = accounts_dir() / str(a.id)
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
    """정한 시각까지만 이 순서로 쓴다. 지나면 저절로 기본 순서로 돌아간다."""
    until = body.until if body.until.tzinfo else body.until.replace(tzinfo=UTC)
    if until <= utcnow():
        raise HTTPException(status.HTTP_400_BAD_REQUEST, {"code": "bad_input", "message": "끝나는 시각이 이미 지났어요."})
    known = {a.id for a in await profiles.accounts(db) if a.pool == pool}
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


class AccountUsageOut(BaseModel):
    id: uuid.UUID
    name: str
    engine: str
    pool: str
    enabled: bool
    rank: int
    login: str
    cooldown_seconds: int
    windows: list[UsageWindowOut]
    source: str | None
    plan: str | None
    error: str | None
    updated_at: float | None


@router.get("/usage", response_model=list[AccountUsageOut])
async def usage(_: User, db: Db, refresh: bool = False) -> list[AccountUsageOut]:
    """계정별 사용량. 개인 → 회사 묶음 순, 각 묶음 안은 지금 적용되는 순서(임시 순서 반영). 계정마다 2분 캐시, refresh 면 새로."""
    ordered = [(i, a) for pool in profiles.POOLS for i, a in enumerate(await profiles.effective_order(db, pool))]

    async def one(i: int, a: EngineAccount) -> AccountUsageOut:
        login, _label = profiles.identity(a.engine, a.config_dir)
        u = await usage_mod.usage_for(a.id, a.engine, a.config_dir, refresh=refresh)
        return AccountUsageOut(id=a.id, name=a.name, engine=a.engine, pool=a.pool, enabled=a.enabled, rank=i + 1, login=login,
                               cooldown_seconds=profiles.cooldown_remaining(a.id),
                               windows=[UsageWindowOut(**w.__dict__) for w in u.windows], source=u.source, plan=u.plan,
                               error=u.error, updated_at=u.updated_at)

    return list(await asyncio.gather(*(one(i, a) for i, a in ordered)))


@router.patch("/{account_id}", response_model=AccountOut)
async def patch_account(account_id: uuid.UUID, body: AccountPatch, _: User, db: Db, request: Request) -> AccountOut:
    a = await _get(db, account_id)
    data = body.model_dump(exclude_unset=True)
    if "name" in data and data["name"] is not None:
        data["name"] = data["name"].strip()
    if data.get("pool") and data["pool"] != a.pool:
        top = await db.scalar(select(func.max(EngineAccount.priority)).where(EngineAccount.pool == data["pool"]))
        data["priority"] = (top if top is not None else -1) + 1
    elif "pool" in data:
        data.pop("pool")
    for k, v in data.items():
        setattr(a, k, v)
    if data.get("enabled"):
        # 다시 켜면 한도 대기도 풀어 바로 쓰게 한다.
        profiles.clear_cooldown(a.id)
    record(db, action="account.update", target_type="account", target_id=a.id, detail=data, ip=client_ip(request))
    await _commit(db)
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
    """한 묶음(개인·회사) 안의 순서. 그 묶음의 모든 계정 id 를 한 번씩 보낸다."""
    rows = {a.id: a for a in await profiles.accounts(db) if a.pool == pool}
    if set(body.ids) != set(rows) or len(body.ids) != len(rows):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "그 묶음의 모든 계정 id 를 한 번씩 보내야 합니다.")
    for i, aid in enumerate(body.ids):
        rows[aid].priority = i
    await db.commit()
    return [_out(rows[aid]) for aid in body.ids]


@router.post("/{account_id}/cooldown/clear", response_model=AccountOut)
async def clear(account_id: uuid.UUID, _: User, db: Db) -> AccountOut:
    a = await _get(db, account_id)
    profiles.clear_cooldown(a.id)
    return _out(a)
