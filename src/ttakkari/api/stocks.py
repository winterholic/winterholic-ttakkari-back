"""주식 투자: 토스증권 연결 상태·수동 확인·동기화(ADR 0044). 전부 읽기 전용이다."""

from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict
from sqlalchemy import func, select

from ttakkari.api.deps import Db
from ttakkari.models import TossInvestSyncRun
from ttakkari.orchestrator.runtime import OrchestratorSession
from ttakkari.security.auth import User
from ttakkari.stocks import tossinvest as svc


async def user_access(_: User) -> None:
    pass


async def agent_access(sid: OrchestratorSession, db: Db) -> None:
    from ttakkari.models import ChatSession

    if await db.get(ChatSession, sid) is None:
        raise HTTPException(403, "세션이 없습니다.")


class RunOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    kind: str
    status: str
    message: str | None
    logged_in: bool | None
    page_title: str | None
    api_paths: list[str]
    duration_ms: int | None
    request_source: str
    created_at: datetime
    updated_at: datetime


class ProviderOut(BaseModel):
    id: str
    name: str
    state: str
    checked_at: datetime | None
    detail: str


class StatusOut(BaseModel):
    state: str
    message: str
    logged_in: bool | None = None
    checked_at: datetime | None = None
    browser_available: bool = False
    profile_exists: bool = False
    check_interval_seconds: int = 0
    last_run: RunOut | None = None


class LoginWindowOut(BaseModel):
    state: str
    message: str


async def _latest(db: Db) -> TossInvestSyncRun | None:
    q = select(TossInvestSyncRun).order_by(TossInvestSyncRun.created_at.desc()).limit(1)
    return await db.scalar(q)


def _run_out(row: TossInvestSyncRun) -> RunOut:
    return RunOut(
        id=row.id,
        kind=row.kind,
        status=row.status,
        message=row.message,
        logged_in=row.logged_in,
        page_title=row.page_title,
        api_paths=list(row.api_paths or []),
        duration_ms=row.duration_ms,
        request_source=row.request_source,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


async def _status_view(db: Db) -> StatusOut:
    from ttakkari.config import get_settings

    s = get_settings()
    last = await _latest(db)
    if last is None:
        return StatusOut(
            state="never_checked",
            message="아직 연결을 확인하지 않았습니다.",
            browser_available=await _browser_ok(),
            profile_exists=svc.profile_has_files(),
            check_interval_seconds=s.tossinvest_check_interval_seconds,
        )
    return StatusOut(
        state=last.status,
        message=last.message or "",
        logged_in=last.logged_in,
        checked_at=last.created_at,
        browser_available=await _browser_ok(),
        profile_exists=svc.profile_has_files(),
        check_interval_seconds=s.tossinvest_check_interval_seconds,
        last_run=_run_out(last),
    )


async def _browser_ok() -> bool:
    import asyncio

    return await asyncio.to_thread(svc.find_chrome) is not None


async def _guard_interval(db: Db) -> None:
    from ttakkari.config import get_settings

    interval = get_settings().tossinvest_check_interval_seconds
    last = await _latest(db)
    if last is None:
        return
    wait = (
        interval
        - (datetime.now(UTC) - max(last.created_at, last.updated_at)).total_seconds()
    )
    if wait > 0:
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            {
                "code": "rate_limited",
                "message": f"사람 수준 빈도를 지키려고 {int(wait) + 1}초 뒤에 다시 시도해 주세요.",
            },
            headers={"Retry-After": str(int(wait) + 1)},
        )


async def _reserve(db: Db, kind: str) -> TossInvestSyncRun:
    # 같은 DB의 다른 프로세스·세대도 예약 확인과 저장을 직렬화한다.
    await db.execute(select(func.pg_advisory_xact_lock(0x54544B, 44)))
    await _guard_interval(db)
    row = TossInvestSyncRun(
        kind=kind,
        status="running",
        message="연결을 확인하고 있습니다.",
        logged_in=None,
        page_title=None,
        api_paths=[],
        duration_ms=None,
        request_source="manual",
    )
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return row


async def _run_kind(db: Db, kind: str) -> RunOut:
    row = await _reserve(
        db, kind
    )  # 브라우저를 열기 전에 영속화; 재시작해도 간격이 남는다.
    started = time.monotonic()
    try:
        # 실행 시간을 최소 재시도 간격보다 짧게 제한한다. 예약만 남은 중단 작업도
        # 300초 뒤 재시도할 수 있으며, 정상 실행이 그때까지 이어지지 않는다.
        async with asyncio.timeout(180):
            if kind == "sync":
                st, summary = await svc.sync_snapshot()
            else:
                st = await svc.check_status()
                summary = None
    except Exception as e:
        # 예상 밖 실패도 기록하고 500으로 알린다.
        row.status = "failed"
        row.message = f"확인하지 못했습니다: {type(e).__name__}"
        row.duration_ms = int((time.monotonic() - started) * 1000)
        await db.commit()
        raise HTTPException(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            "토스증권 연결을 확인하지 못했습니다.",
        ) from e
    row.status = st.state
    row.message = st.message
    row.logged_in = st.logged_in
    row.page_title = (st.observation.title or "")[:200] if st.observation else None
    row.api_paths = (summary or {}).get("api_paths", [])
    row.duration_ms = int((time.monotonic() - started) * 1000)
    row.updated_at = datetime.now(UTC)
    await db.commit()
    await db.refresh(row)
    return _run_out(row)


router = APIRouter(
    prefix="/api/stocks", tags=["stocks"], dependencies=[Depends(user_access)]
)


@router.get("/overview", response_model=list[ProviderOut])
async def overview(db: Db) -> list[ProviderOut]:
    last = await _latest(db)
    return [
        ProviderOut(
            id="tossinvest",
            name="토스증권",
            state=last.status if last else "never_checked",
            checked_at=last.created_at if last else None,
            detail="/stocks/tossinvest",
        )
    ]


@router.get("/tossinvest/status", response_model=StatusOut)
async def tossinvest_status(db: Db) -> StatusOut:
    """마지막으로 확인한 상태를 본다. 브라우저를 띄우지 않는다."""
    return await _status_view(db)


@router.get("/tossinvest/runs", response_model=list[RunOut])
async def tossinvest_runs(db: Db, limit: int = 20) -> list[RunOut]:
    if not 1 <= limit <= 100:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY, "limit은 1~100이어야 합니다."
        )
    rows = list(
        await db.scalars(
            select(TossInvestSyncRun)
            .order_by(TossInvestSyncRun.created_at.desc())
            .limit(limit)
        )
    )
    return [_run_out(r) for r in rows]


@router.get("/tossinvest/login-help")
async def tossinvest_login_help() -> dict:
    return svc.login_help()


@router.post(
    "/tossinvest/login-window",
    response_model=LoginWindowOut,
    status_code=status.HTTP_202_ACCEPTED,
)
async def tossinvest_login_window() -> LoginWindowOut:
    """일반 Chrome으로 로그인 창을 연다. URL·경로 등 외부 입력을 받지 않고 고정 인자만 쓴다."""
    if await asyncio.to_thread(svc.login_chrome_pids):
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            {
                "code": "login_window_open",
                "message": "로그인 창이 이미 열려 있습니다. 로그인하고 창을 닫은 뒤 연결 확인을 눌러 주세요.",
            },
        )
    if await asyncio.to_thread(svc.headless_check_running):
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            {
                "code": "check_running",
                "message": "연결 확인이 실행 중입니다. 끝난 뒤에 다시 시도해 주세요.",
            },
        )
    st = await asyncio.to_thread(svc.open_login_window)
    if st.state == "failed":
        raise HTTPException(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            st.message or "로그인 창을 열지 못했습니다.",
        )
    return LoginWindowOut(state=st.state, message=st.message or "")


@router.post(
    "/tossinvest/check", response_model=RunOut, status_code=status.HTTP_202_ACCEPTED
)
async def tossinvest_check(db: Db) -> RunOut:
    """연결 확인 1회. 사람 수준 빈도 간격을 지킨다."""
    return await _run_kind(db, "check")


@router.post(
    "/tossinvest/sync", response_model=RunOut, status_code=status.HTTP_202_ACCEPTED
)
async def tossinvest_sync(db: Db) -> RunOut:
    """동기화 1회. 읽기만 하며 금액·수량은 저장하지 않는다."""
    return await _run_kind(db, "sync")


agent_router = APIRouter(
    prefix="/api/agent/stocks", tags=["stocks"], dependencies=[Depends(agent_access)]
)


@agent_router.get("/tossinvest/status", response_model=StatusOut)
async def agent_tossinvest_status(db: Db) -> StatusOut:
    return await _status_view(db)


@agent_router.get("/tossinvest/runs", response_model=list[RunOut])
async def agent_tossinvest_runs(db: Db, limit: int = 20) -> list[RunOut]:
    return await tossinvest_runs(db, limit)


@agent_router.get("/tossinvest/login-help")
async def agent_tossinvest_login_help() -> dict:
    return svc.login_help()
